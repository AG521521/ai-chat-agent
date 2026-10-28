"""SQLite 持久化：任务、状态迁移、审核、记忆、审计、暂停开关。

设计约束：
* 每个动作一个事务（``with self.tx() as cur``），失败整笔回滚；
* 去重靠 ``UNIQUE(platform, contact_id, dedupe_key)``，不靠应用层判断；
* 状态迁移必须走 ``ALLOWED_TRANSITIONS``，非法迁移直接抛错；
* 发送相关的字段只由 mark_sent 写，避免中途被覆盖。

本模块只用标准库。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import dedupe as dedupe_mod
from .model import (
    TERMINAL_STATES,
    Decision,
    IncomingMessage,
    State,
    now_iso,
)

# ---------------------------------------------------------------- 状态机

ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.OBSERVED: {State.VALIDATING, State.CANCELLED, State.ERROR_HOLD, State.PAUSED},
    State.VALIDATING: {
        State.ANALYZING,
        State.CANCELLED,
        State.ERROR_HOLD,
        State.PAUSED,
    },
    State.ANALYZING: {
        State.DECIDING,
        State.WAITING_APPROVAL,
        State.ERROR_HOLD,
        State.PAUSED,
    },
    State.DECIDING: {
        State.PREPARING_REPLY,
        State.WAITING_APPROVAL,
        State.VERIFYING_QUOTE,
        State.ERROR_HOLD,
        State.CANCELLED,
        State.PAUSED,
    },
    State.WAITING_APPROVAL: {
        State.PREPARING_REPLY,
        State.READY_TO_SEND,
        State.CANCELLED,
        State.ERROR_HOLD,
        State.PAUSED,
    },
    State.PREPARING_REPLY: {
        State.VERIFYING_QUOTE,
        State.READY_TO_SEND,
        State.WAITING_APPROVAL,
        State.ERROR_HOLD,
        State.CANCELLED,
        State.PAUSED,
    },
    State.VERIFYING_QUOTE: {
        State.READY_TO_SEND,
        State.SENDING,  # 自动模式：引用由发送端现场校验，校验通过后直接发
        State.WAITING_APPROVAL,
        State.ERROR_HOLD,
        State.CANCELLED,
        State.PAUSED,
    },
    State.READY_TO_SEND: {
        State.SENDING,
        State.WAITING_APPROVAL,
        State.ERROR_HOLD,
        State.CANCELLED,
        State.PAUSED,
    },
    State.SENDING: {State.VERIFYING_RESULT, State.SEND_UNKNOWN, State.PAUSED},
    State.VERIFYING_RESULT: {
        State.DONE,
        State.SEND_UNKNOWN,
        State.FAILED_CONFIRMED,
        State.ERROR_HOLD,
        State.PAUSED,
    },
    # 状态不明时只能复核，不允许直接回到 SENDING / READY_TO_SEND 重发
    State.SEND_UNKNOWN: {State.DONE, State.FAILED_CONFIRMED, State.ERROR_HOLD, State.PAUSED},
    State.ERROR_HOLD: {
        State.WAITING_APPROVAL,
        State.OBSERVED,
        State.CANCELLED,
        State.PAUSED,
    },
    State.PAUSED: {State.OBSERVED, State.WAITING_APPROVAL, State.CANCELLED},
    State.DONE: set(),
    State.FAILED_CONFIRMED: set(),
    State.CANCELLED: set(),
}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    platform           TEXT NOT NULL,
    contact_id         TEXT NOT NULL,
    dedupe_key         TEXT NOT NULL,
    state              TEXT NOT NULL,
    content_type       TEXT NOT NULL DEFAULT 'text',
    text               TEXT NOT NULL DEFAULT '',
    observed_at        TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    contact_verified   INTEGER NOT NULL DEFAULT 0,
    allowlisted        INTEGER NOT NULL DEFAULT 0,
    ui_state_uncertain INTEGER NOT NULL DEFAULT 0,
    requires_quote     INTEGER NOT NULL DEFAULT 0,
    quote_target       TEXT,
    quote_target_verified INTEGER NOT NULL DEFAULT 0,
    evidence_level     TEXT NOT NULL DEFAULT 'ui_observed',
    source_message_id  TEXT,
    media_path         TEXT,
    media_summary      TEXT,
    risk_level         TEXT,
    confidence         REAL,
    requires_human     INTEGER NOT NULL DEFAULT 1,
    reason_codes       TEXT NOT NULL DEFAULT '[]',
    draft_text         TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    raw_json           TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform, contact_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    at         TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    task_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace  TEXT NOT NULL,
    platform   TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    role       TEXT,
    text       TEXT NOT NULL,
    at         TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'observed',
    confidence REAL NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory(namespace, kind, id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(platform, state);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    task_id TEXT,
    event   TEXT NOT NULL,
    detail  TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class ObserveResult:
    task_id: str
    created: bool
    dedupe_key: str
    duplicate_of: Optional[str] = None


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ---------------------------------------------------------- 基础
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            self._ensure_column(conn, "tasks", "desire_json", "TEXT")
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ---------------------------------------------------------- 观测 / 去重
    def observe(self, msg: IncomingMessage) -> ObserveResult:
        """登记一条观测到的消息。同一键重复观测只会返回既有任务。"""
        key = dedupe_mod.dedupe_key(msg)
        ts = now_iso()
        with self.tx() as cur:
            exists = cur.execute(
                "SELECT task_id FROM tasks WHERE platform=? AND contact_id=? AND dedupe_key=?",
                (msg.platform, msg.contact_id, key),
            ).fetchone()
            if exists:
                cur.execute(
                    "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                    (ts, exists["task_id"], "duplicate_observed", key),
                )
                return ObserveResult(exists["task_id"], False, key, exists["task_id"])

            cur.execute(
                """INSERT INTO tasks(
                    task_id, platform, contact_id, dedupe_key, state, content_type, text,
                    observed_at, created_at, updated_at, contact_verified, allowlisted,
                    ui_state_uncertain, requires_quote, quote_target, quote_target_verified,
                    evidence_level, source_message_id, media_path, media_summary, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    msg.task_id,
                    msg.platform,
                    msg.contact_id,
                    key,
                    State.OBSERVED.value,
                    msg.content_type,
                    msg.text or "",
                    msg.observed_at,
                    ts,
                    ts,
                    int(msg.contact_verified),
                    int(msg.is_allowlisted),
                    int(msg.ui_state_uncertain),
                    int(msg.requires_quote),
                    msg.quote_target,
                    int(msg.quote_target_verified),
                    msg.evidence_level.value,
                    msg.source_message_id,
                    msg.media_path,
                    msg.media_summary,
                    json.dumps(msg.raw_ref, ensure_ascii=False),
                ),
            )
            cur.execute(
                "INSERT INTO transitions(task_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                (msg.task_id, None, State.OBSERVED.value, ts, "observed"),
            )
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (ts, msg.task_id, "observed", json.dumps({"key": key}, ensure_ascii=False)),
            )
        return ObserveResult(msg.task_id, True, key)

    def find_duplicate_text(self, platform: str, contact_id: str, text: str) -> Optional[str]:
        """内容指纹层面的重复提示（非平台级确定性）。"""
        fp = dedupe_mod.content_fingerprint(text)
        if not fp:
            return None
        with self.read() as conn:
            rows = conn.execute(
                "SELECT task_id, text FROM tasks WHERE platform=? AND contact_id=? "
                "ORDER BY created_at DESC LIMIT 50",
                (platform, contact_id),
            ).fetchall()
        for row in rows:
            if dedupe_mod.content_fingerprint(row["text"]) == fp:
                return row["task_id"]
        return None

    # ---------------------------------------------------------- 状态机
    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def set_state(
        self,
        task_id: str,
        to_state: State,
        note: str = "",
        **fields: Any,
    ) -> State:
        """合法迁移才写库；重复设成同一状态视为幂等。"""
        ts = now_iso()
        with self.tx() as cur:
            row = cur.execute(
                "SELECT state FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"task not found: {task_id}")
            current = State(row["state"])
            if current is to_state:
                return current
            if to_state not in ALLOWED_TRANSITIONS[current]:
                raise StoreError(f"illegal transition {current.value} -> {to_state.value}")
            cur.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                (to_state.value, ts, task_id),
            )
            if fields:
                cols = ", ".join(f"{k}=?" for k in fields)
                cur.execute(
                    f"UPDATE tasks SET {cols} WHERE task_id=?",
                    (*fields.values(), task_id),
                )
            cur.execute(
                "INSERT INTO transitions(task_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                (task_id, current.value, to_state.value, ts, note),
            )
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (ts, task_id, "state", f"{current.value}->{to_state.value}"),
            )
            return to_state

    def apply_decision(self, task_id: str, decision: Decision, draft: str = "") -> None:
        """把决策写回任务：先更新分析字段，再迁移状态。"""
        ts = now_iso()
        with self.tx() as cur:
            row = cur.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise StoreError(f"task not found: {task_id}")
            current = State(row["state"])
            target = decision.state
            if target is not current:
                if target not in ALLOWED_TRANSITIONS[current]:
                    raise StoreError(f"illegal transition {current.value} -> {target.value}")
                cur.execute(
                    "INSERT INTO transitions(task_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                    (task_id, current.value, target.value, ts, ",".join(decision.reasons)[:300]),
                )
            cur.execute(
                """UPDATE tasks SET state=?, updated_at=?, risk_level=?, requires_human=?,
                   reason_codes=?, draft_text=COALESCE(NULLIF(?,''), draft_text)
                   WHERE task_id=?""",
                (
                    target.value,
                    ts,
                    decision.risk_level.value,
                    int(decision.requires_human),
                    json.dumps(decision.reasons, ensure_ascii=False),
                    draft,
                    task_id,
                ),
            )
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (ts, task_id, "decision", json.dumps(decision.to_dict(), ensure_ascii=False)),
            )

    def set_confidence(self, task_id: str, confidence: float) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE tasks SET confidence=? WHERE task_id=?", (confidence, task_id))

    def set_desire(self, task_id: str, data: dict[str, Any]) -> None:
        """保存「她想要什么」分析（推断，不是事实）。"""
        if not data:
            return
        with self.tx() as cur:
            cur.execute(
                "UPDATE tasks SET desire_json=?, updated_at=? WHERE task_id=?",
                (json.dumps(data, ensure_ascii=False), now_iso(), task_id),
            )

    def attach_media(
        self,
        task_id: str,
        summary: Optional[str] = None,
        quote_target: Optional[str] = None,
    ) -> None:
        """把适配器从界面取到的媒体证据写回任务。"""
        sets: list[str] = []
        vals: list[Any] = []
        if summary is not None:
            sets.append("media_summary=?")
            vals.append(summary[:800])
        if quote_target is not None:
            sets.append("quote_target=?")
            vals.append(quote_target[:400])
        if not sets:
            return
        ts = now_iso()
        sets.append("updated_at=?")
        vals.append(ts)
        vals.append(task_id)
        with self.tx() as cur:
            cur.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", tuple(vals))
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (ts, task_id, "attach_media", (summary or quote_target or "")[:300]),
            )

    def mark_sent(
        self,
        task_id: str,
        status: str,
        evidence: str,
        detail: str = "",
    ) -> State:
        """status: verified / unknown / failed。未知状态不许自动重发。"""
        target = {
            "verified": State.DONE,
            "unknown": State.SEND_UNKNOWN,
            "failed": State.FAILED_CONFIRMED,
        }.get(status)
        if target is None:
            raise StoreError(f"bad send status: {status}")
        task = self.get_task(task_id)
        if not task:
            raise StoreError(f"task not found: {task_id}")
        current = State(task["state"])
        if current is State.VERIFYING_QUOTE:
            # 自动模式允许从 VERIFYING_QUOTE 直接发（引用由发送端现场校验）
            self.set_state(task_id, State.SENDING, "adapter clicked send (auto quote)")
            current = State.SENDING
        if current is State.READY_TO_SEND:
            # 适配器已经点了发送，补上 SENDING 这一步再进结果态
            self.set_state(task_id, State.SENDING, "adapter clicked send")
            current = State.SENDING
        if current is State.SENDING:
            self.set_state(task_id, State.VERIFYING_RESULT, "adapter result")
        elif current is not State.VERIFYING_RESULT:
            raise StoreError(f"cannot mark send result from {current.value}")
        self.set_state(task_id, target, f"{status}/{evidence}")
        with self.tx() as cur:
            cur.execute(
                "UPDATE tasks SET evidence_level=?, last_error=? WHERE task_id=?",
                (evidence, detail[:500] if status != "verified" else None, task_id),
            )
        return target

    # ---------------------------------------------------------- 审核队列
    def enqueue_approval(self, task_id: str) -> None:
        ts = now_iso()
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO approvals(task_id, created_at, status) VALUES(?,?,'pending')",
                (task_id, ts),
            )

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                """SELECT a.task_id, a.created_at, t.platform, t.contact_id, t.content_type,
                          t.text, t.risk_level, t.reason_codes, t.draft_text, t.state
                   FROM approvals a JOIN tasks t ON t.task_id=a.task_id
                   WHERE a.status='pending' ORDER BY a.created_at"""
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["reason_codes"] = json.loads(item["reason_codes"] or "[]")
            except json.JSONDecodeError:
                item["reason_codes"] = []
            out.append(item)
        return out

    def decide_approval(
        self, task_id: str, approve: bool, by: str, note: str = ""
    ) -> dict[str, Any]:
        ts = now_iso()
        with self.tx() as cur:
            row = cur.execute(
                "SELECT status FROM approvals WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"approval not found: {task_id}")
            if row["status"] != "pending":
                raise StoreError(f"approval already {row['status']}")
            cur.execute(
                "UPDATE approvals SET status=?, decided_at=?, decided_by=?, note=? WHERE task_id=?",
                ("approved" if approve else "rejected", ts, by, note, task_id),
            )
            task = cur.execute(
                "SELECT state FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not task:
                raise StoreError(f"task not found: {task_id}")
            current = State(task["state"])
            if not approve:
                target = State.CANCELLED
            elif current is State.VERIFYING_QUOTE:
                # 人工在界面上确认了引用目标 —— 这一步本身就是"界面级验证"，
                # 记入 approvals 的 by/note，供事后追溯。
                cur.execute(
                    "UPDATE tasks SET quote_target_verified=1, updated_at=? WHERE task_id=?",
                    (ts, task_id),
                )
                target = State.READY_TO_SEND
            else:
                target = State.PREPARING_REPLY
            if target not in ALLOWED_TRANSITIONS[current]:
                raise StoreError(f"illegal transition {current.value} -> {target.value}")
            cur.execute(
                "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                (target.value, ts, task_id),
            )
            cur.execute(
                "INSERT INTO transitions(task_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                (task_id, current.value, target.value, ts, f"approval by {by}: {note}"[:300]),
            )
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (ts, task_id, "approval", "approved" if approve else "rejected"),
            )
        return {"task_id": task_id, "status": "approved" if approve else "rejected"}

    # ---------------------------------------------------------- 暂停开关
    def set_pause(self, scope: str, paused: bool, by: str = "cli") -> None:
        key = f"paused:{scope}"
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, "1" if paused else "0"),
            )
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (now_iso(), None, "pause", f"{scope}={paused} by {by}"),
            )

    def is_paused(self, platform: str) -> bool:
        keys = (f"paused:{platform}", "paused:all")
        with self.read() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM meta WHERE key IN ({','.join('?' * len(keys))})", keys
            ).fetchall()
        return any(r["value"] == "1" for r in rows)

    def meta_get(self, key: str, default: str = "") -> str:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---------------------------------------------------------- 记忆
    def add_turn(
        self,
        namespace: str,
        platform: str,
        contact_id: str,
        role: str,
        text: str,
        source: str = "observed",
        confidence: float = 1.0,
    ) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO memory(namespace, platform, contact_id, kind, role, text, at, source, confidence) "
                "VALUES(?,?,?,'turn',?,?,?,?,?)",
                (namespace, platform, contact_id, role, text, now_iso(), source, confidence),
            )

    def add_fact(
        self,
        namespace: str,
        platform: str,
        contact_id: str,
        text: str,
        source: str = "user",
        confidence: float = 1.0,
    ) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO memory(namespace, platform, contact_id, kind, role, text, at, source, confidence) "
                "VALUES(?,?,?,'fact',NULL,?,?,?,?)",
                (namespace, platform, contact_id, text, now_iso(), source, confidence),
            )

    def recent_turns(self, namespace: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT role, text, at FROM memory WHERE namespace=? AND kind='turn' "
                "ORDER BY id DESC LIMIT ?",
                (namespace, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def all_turns(self, namespace: str, limit: int = 5000) -> list[dict[str, Any]]:
        """取某个命名空间下的全部轮次（时间正序），用于确定性统计。"""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT role, text, at FROM memory WHERE namespace=? AND kind='turn' "
                "ORDER BY id ASC LIMIT ?",
                (namespace, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def facts(self, namespace: str, keyword: str = "", limit: int = 10) -> list[dict[str, Any]]:
        sql = "SELECT text, source, confidence, at FROM memory WHERE namespace=? AND kind='fact'"
        args: list[Any] = [namespace]
        if keyword:
            sql += " AND text LIKE ?"
            args.append(f"%{keyword}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def clear_memory(self, platform: str, contact_id: str) -> int:
        with self.tx() as cur:
            res = cur.execute(
                "DELETE FROM memory WHERE platform=? AND contact_id=? AND namespace LIKE ?",
                (platform, contact_id, f"%{platform}:{contact_id}%"),
            )
            n = res.rowcount
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (now_iso(), None, "memory_cleared", f"{platform}:{contact_id} rows={n}"),
            )
        return n

    # ---------------------------------------------------------- 其他
    def audit(self, event: str, task_id: str | None = None, detail: Any = None) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO audit(at, task_id, event, detail) VALUES(?,?,?,?)",
                (now_iso(), task_id, event, json.dumps(detail, ensure_ascii=False, default=str)),
            )

    def list_tasks(self, platform: str = "", state: str = "", limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT task_id, platform, contact_id, state, content_type, risk_level, " \
              "requires_human, created_at, substr(text,1,60) AS text_head FROM tasks WHERE 1=1"
        args: list[Any] = []
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        if state:
            sql += " AND state=?"
            args.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.read() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def auto_ready(self, platform: str = "", limit: int = 20,
                   max_risk: str = "R1", only_media: bool = True) -> list[dict[str, Any]]:
        """自动模式可发送的任务：风险不超过阈值，且已到 READY_TO_SEND / VERIFYING_QUOTE。"""
        allowed = ["R0"] if max_risk.upper() == "R0" else ["R0", "R1"]
        sql = (
            "SELECT task_id FROM tasks WHERE state IN ('READY_TO_SEND','VERIFYING_QUOTE') "
            f"AND risk_level IN ({','.join('?' * len(allowed))})"
        )
        args: list[Any] = list(allowed)
        if only_media:
            sql += " AND content_type != 'text'"
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        sql += " ORDER BY updated_at ASC LIMIT ?"
        args.append(limit)
        with self.read() as conn:
            ids = [r["task_id"] for r in conn.execute(sql, args)]
        out = []
        for tid in ids:
            task = self.get_task(tid)
            if task:
                out.append(task)
        return out

    def counts(self) -> dict[str, int]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
            ).fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status='pending'"
            ).fetchone()
        out = {r["state"]: r["n"] for r in rows}
        out["pending_approvals"] = pending["n"]
        return out
