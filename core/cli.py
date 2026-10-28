"""核心 CLI：唯一的入口。两个平台适配器都通过它读写状态。

用法（在 D:\\ai_chat_core 下）::

    python -m core.cli init
    echo {...} | python -m core.cli run
    python -m core.cli decide --task-id <id>
    python -m core.cli queue
    python -m core.cli approve --task-id <id> --by me
    python -m core.cli mark-sent --task-id <id> --status verified
    python -m core.cli pause --scope all
    python -m core.cli selftest

输入/输出统一为 JSON（stdout），错误写 stderr 并返回非 0。
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core import approval as approval_mod  # noqa: E402
from core import dedupe as dedupe_mod  # noqa: E402
from core import desire as desire_mod  # noqa: E402
from core import limits as limits_mod  # noqa: E402
from core import risk as risk_mod  # noqa: E402
from core import route as route_mod  # noqa: E402
from core import stats as stats_mod  # noqa: E402
from core import verify as verify_mod  # noqa: E402
from core.classify import Classifier, ClassifyError, Transport  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.model import (  # noqa: E402
    Analysis,
    Decision,
    IncomingMessage,
    RiskLevel,
    State,
)
from core.persona import PersonaStore  # noqa: E402
from core.store import ALLOWED_TRANSITIONS, Store, StoreError  # noqa: E402

# 正常分析路径上的状态前缀：只有在目标态无法直达时才逐级推进
_PREFIX = (State.VALIDATING, State.ANALYZING, State.DECIDING, State.PREPARING_REPLY)


def _advance_toward(store: Store, task_id: str, target: State) -> None:
    """把任务从当前状态推进到 target 的可达前驱（不直接写 target）。"""
    task = store.get_task(task_id)
    if not task:
        raise StoreError(f"task not found: {task_id}")
    cur = State(task["state"])
    if target is cur or target in ALLOWED_TRANSITIONS[cur]:
        return
    for nxt in _PREFIX:
        task = store.get_task(task_id)
        cur = State(task["state"])
        if target is cur or target in ALLOWED_TRANSITIONS[cur]:
            return
        if nxt in ALLOWED_TRANSITIONS[cur]:
            store.set_state(task_id, nxt, "auto")
    task = store.get_task(task_id)
    cur = State(task["state"])
    if target is not cur and target not in ALLOWED_TRANSITIONS[cur]:
        raise StoreError(f"unreachable {cur.value} -> {target.value}")

SETTINGS_DEFAULTS: dict[str, Any] = {
    "app": {"dry_run": True, "global_paused": False, "log_level": "INFO"},
    "reply": {
        "mode": "manual",
        "min_confidence": 0.9,
        "max_length": 120,
        "default_on_uncertain": "human_review",
        "allowlisted_contacts": {"wechat": [], "douyin": []},
    },
    "risk": {
        "require_human_for": ["R1", "R2", "R3"],
        "always_review_topics": [
            "major_relationship_decisions",
            "money",
            "privacy",
            "major_promises",
            "intense_conflict",
            "crisis",
        ],
    },
    "video": {
        "require_quote_verification": True,
        "if_quote_uncertain": "human_review",
        "if_only_thumbnail_available": "partial_understanding",
    },
    "memory": {"recent_turns": 20, "use_long_term": True, "isolate_by_contact": True, "link_groups": []},
    "recovery": {
        "max_analysis_retries": 2,
        "max_ui_retries": 2,
        "resend_if_status_unknown": False,
        "pause_on_login_challenge": True,
        "circuit_breaker_enabled": True,
        "circuit_breaker_threshold": 3,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0.8,
        "max_tokens": 400,
        "timeout_seconds": 30,
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key_file": "config/secrets.local.json",
    },
    "paths": {"db": "var/agent.db", "media": "var/media"},
}


# ------------------------------------------------------------------ 工具

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(base: Path) -> dict[str, Any]:
    """默认值 ← settings.json ← settings.local.json（本地覆盖，含真实联系人，不入库）。"""
    cfg: dict[str, Any] = {}
    for name in ("settings.json", "settings.local.json"):
        path = base / "config" / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"{name} 解析失败: {exc}")
        if not isinstance(data, dict):
            raise SystemExit(f"{name} 顶层必须是对象")
        cfg = deep_merge(cfg, data)
    return deep_merge(SETTINGS_DEFAULTS, cfg)


def resolve(base: Path, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else base / p


def db_path(base: Path, settings: dict[str, Any], override: Optional[str] = None) -> Path:
    return Path(override) if override else resolve(base, settings["paths"]["db"])


def emit(obj: Any, code: int = 0) -> int:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
    return code


def fail(msg: str, code: int = 1) -> int:
    sys.stderr.write(json.dumps({"error": msg}, ensure_ascii=False) + "\n")
    return code


def read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise SystemExit("需要从 stdin 传入 JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"stdin JSON 解析失败: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("stdin JSON 顶层必须是对象")
    return data


def norm_contact(value: str) -> str:
    """联系人名规范化：OCR/备注会在中文间插空格或加后缀，去空白后比对。"""
    return re.sub(r"\s+", "", value or "")


def is_allowlisted(settings: dict[str, Any], platform: str, contact_id: str) -> bool:
    """白名单匹配：全等，或按分隔符切词后与条目全等。

    兼容「小A」→「小A（白名单）」这类备注变化，但不会把
    「白名单粉丝」误判成「白名单」。
    """
    from core.naming import matches_alias

    table = settings.get("reply", {}).get("allowlisted_contacts", {}) or {}
    entries = table.get(platform) or []
    if not norm_contact(contact_id):
        return False
    return any(matches_alias(str(entry), contact_id) for entry in entries)


def task_to_message(task: dict[str, Any]) -> IncomingMessage:
    return IncomingMessage(
        platform=task["platform"],
        contact_id=task["contact_id"],
        content_type=task["content_type"],
        text=task["text"] or "",
        observed_at=task.get("observed_at"),
        source_message_id=task.get("source_message_id"),
        media_path=task.get("media_path"),
        media_summary=task.get("media_summary"),
        quote_target=task.get("quote_target"),
        quote_target_verified=bool(task.get("quote_target_verified")),
        contact_verified=bool(task.get("contact_verified")),
        is_allowlisted=bool(task.get("allowlisted")),
        ui_state_uncertain=bool(task.get("ui_state_uncertain")),
        requires_quote=bool(task.get("requires_quote")),
        evidence_level=task.get("evidence_level") or "ui_observed",
        task_id=task["task_id"],
    )


def offline_transport(payload: dict[str, Any], settings: dict[str, Any]) -> str:
    """离线占位模型：只在 selftest / 单测里用，绝不用于真实回复。"""
    user = ""
    for m in payload.get("messages", []):
        if m.get("role") == "user":
            user += m.get("content", "")
    risky = any(k in user for k in ("转账", "分手", "借钱", "身份证"))
    return json.dumps(
        {
            "reply_text": "[离线占位] 这条需要用真实模型重新生成。",
            "intent": "unknown",
            "risk_level": "R2" if risky else "R0",
            "confidence": 0.55 if risky else 0.95,
            "uncertain": False,
            "requires_human": bool(risky),
            "reason_codes": ["offline_stub"],
            "media_understanding": "unavailable",
            "suggested_reply_strategy": "warm_conversational",
        },
        ensure_ascii=False,
    )


# ------------------------------------------------------------------ 命令

def cmd_init(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    return emit({"ok": True, "db": str(store.path), "counts": store.counts()})


def _observe_messages(
    store: Store, settings: dict[str, Any], payload: dict[str, Any], allow_all: bool
) -> dict[str, Any]:
    platform = payload.get("platform", "")
    contact_id = str(payload.get("contact_id", ""))
    if platform not in ("wechat", "douyin"):
        raise SystemExit("platform 必须是 wechat 或 douyin")
    if not contact_id:
        raise SystemExit("contact_id 不能为空")

    allowlisted = True if allow_all else is_allowlisted(settings, platform, contact_id)
    contact_verified = bool(payload.get("contact_verified", False))

    created, duplicates = [], []
    for raw in payload.get("messages", []):
        msg = IncomingMessage(
            platform=platform,
            contact_id=contact_id,
            content_type=raw.get("content_type", "text"),
            text=raw.get("text", "") or "",
            observed_at=raw.get("observed_at"),
            source_message_id=raw.get("source_message_id"),
            media_path=raw.get("media_path"),
            media_summary=raw.get("media_summary"),
            media_sig=raw.get("media_sig"),
            quote_target=raw.get("quote_target"),
            quote_target_verified=bool(raw.get("quote_target_verified", False)),
            contact_verified=contact_verified,
            is_allowlisted=allowlisted,
            ui_state_uncertain=bool(raw.get("ui_state_uncertain", False)),
            requires_quote=bool(raw.get("requires_quote", False)),
            evidence_level=raw.get("evidence_level", "ui_observed"),
            raw_ref=raw.get("raw_ref", {}),
        )
        res = store.observe(msg)
        if res.created:
            created.append(
                {
                    "task_id": res.task_id,
                    "dedupe_key": res.dedupe_key,
                    "content_type": msg.content_type,
                    "evidence_level": msg.evidence_level.value,
                    "raw_ref": msg.raw_ref,
                }
            )
        else:
            duplicates.append({"task_id": res.task_id, "dedupe_key": res.dedupe_key})
    return {
        "platform": platform,
        "contact_id": contact_id,
        "allowlisted": allowlisted,
        "created": created,
        "duplicates": duplicates,
    }


def cmd_observe(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    payload = read_payload()
    return emit(_observe_messages(store, settings, payload, args.allow_all))


def _precheck(
    store: Store, settings: dict[str, Any], msg: IncomingMessage, task_id: str
) -> Optional[Any]:
    """花模型调用之前的硬拦截。

    顺序与 route() 一致，但**先于**模型：暂停、联系人不确定、非白名单、重复
    都不该消耗 API，也不该把无关内容送去模型。
    """
    if store.is_paused(msg.platform):
        return Decision(State.PAUSED, ["paused"], RiskLevel.R1, True, next_action="wait")
    if not msg.contact_verified:
        return Decision(
            State.ERROR_HOLD, ["contact_unverified"], RiskLevel.R3, True, next_action="human"
        )
    if not msg.is_allowlisted:
        return Decision(State.CANCELLED, ["not_allowlisted"], RiskLevel.R0, False, next_action="drop")
    mode_cfg = str(settings.get("reply", {}).get("mode", "manual")).lower()
    if (
        settings.get("reply", {}).get("only_media", False)
        and msg.content_type == "text"
        and mode_cfg != "manual"
    ):
        # 自动模式下文字不回，也不值得花模型调用
        return Decision(State.CANCELLED, ["text_skip"], RiskLevel.R0, False, next_action="drop")
    if msg.text:
        dup = store.find_duplicate_text(msg.platform, msg.contact_id, msg.text)
        if dup and dup != task_id:
            return Decision(
                State.CANCELLED, ["duplicate", dup], RiskLevel.R0, False, next_action="drop"
            )
    return None


def _notes_text(base: Path, contact_id: str) -> str:
    """读取**这个联系人**的关系笔记（按联系人隔离，绝不拿别人的画像乱套）。"""
    from core.naming import matches_alias

    path = base / "config" / "relation_notes.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return ""
    contacts = data.get("contacts") or {}
    notes: dict[str, Any] | None = None
    for name, item in contacts.items():
        if matches_alias(str(name), contact_id):
            notes = item
            break
    if not isinstance(notes, dict):
        return ""
    lines = ["【从你们真实聊天里总结出来的（供参考）】"]
    for key in ("her_style", "her_cares", "his_style", "do", "dont"):
        val = notes.get(key)
        if isinstance(val, list):
            lines.extend(f"- {x}" for x in val[:5])
        elif val:
            lines.append(f"- {val}")
    pairs = notes.get("sample_pairs") or []
    if pairs:
        lines.append("- 真实对话样例：")
        for pair in pairs[:5]:
            if isinstance(pair, dict) and pair.get("her") and pair.get("him"):
                lines.append(f"  她：{pair['her'][:40]}  →  他：{pair['him'][:40]}")
    return "\n".join(lines)


def _decide_one(
    store: Store,
    settings: dict[str, Any],
    base: Path,
    task_id: str,
    offline: bool,
    transport: Optional[Transport] = None,
) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise StoreError(f"task not found: {task_id}")

    msg = task_to_message(task)

    pre = _precheck(store, settings, msg, task_id)
    if pre is not None:
        try:
            _advance_toward(store, task_id, pre.state)
            store.apply_decision(task_id, pre)
            store.audit("precheck", task_id, pre.reasons)
        except StoreError as exc:
            return {"task_id": task_id, "error": str(exc)}
        return {
            "task_id": task_id,
            "decision": pre.to_dict(),
            "analysis": None,
            "risk": {
                "level": pre.risk_level.value,
                "reasons": list(pre.reasons),
                "requires_human": pre.requires_human,
                "topics": [],
            },
            "duplicate_of": None,
            "skipped_model": True,
        }

    mem = Memory(store, settings)
    ctx = mem.context(msg.platform, msg.contact_id)
    recent, facts = ctx["turns"], ctx["facts"]
    stats_text = stats_mod.render_stats(ctx["stats"])
    # 先把历史读出来，再把当前这条写进记忆，避免提示词里出现两遍当前消息
    mem.record_incoming(msg)

    persona = PersonaStore(base / "config" / "persona.json").persona

    # 真实 API 走"先判断、后起草"两次调用；离线/注入 transport 走单次（便于测试）
    two_step = (not offline) and transport is None
    if offline:
        classifier = Classifier(
            settings, base, transport=transport or offline_transport, sleep=lambda _s: None
        )
    else:
        classifier = Classifier(settings, base, transport=transport)

    text_simple = msg.content_type == "text" and bool(
        settings.get("reply", {}).get("text_simple", True)
    )
    try:
        if two_step and text_simple:
            # 文字消息：不做模型风险判断，硬规则把关；模型只负责写那一句话
            analysis = Analysis(
                intent="text_simple",
                risk_level=RiskLevel.R0,
                confidence=1.0,
                uncertain=False,
                source="rules_text",
            )
        elif two_step:
            analysis = classifier.judge(msg, persona, recent, facts, stats_text)
        else:
            analysis = classifier.analyze(
                msg, persona, recent, facts, stats_text=stats_text
            )
    except ClassifyError as exc:
        analysis = Analysis.fallback(f"classify_error:{str(exc)[:120]}")

    assessment = risk_mod.assess(
        msg, analysis, settings, recent_count=len(recent), use_model=not text_simple
    )

    decision = route_mod.route(
        msg,
        analysis,
        assessment,
        settings,
        paused=False,
        duplicate_of=None,
    )

    # 限流：自动模式下超限就退回人工，不硬发
    if decision.state is State.PREPARING_REPLY:
        ok, why = limits_mod.reply_allowed(store, settings, msg.platform, msg.contact_id)
        if not ok:
            decision.state = State.WAITING_APPROVAL
            decision.requires_human = True
            decision.reasons.append(why)

    # 媒体消息没有引用目标就没法做"原生引用"，直接交人工，别等到发送时才失败
    if msg.requires_quote and not str(task.get("quote_target") or "").strip():
        decision = Decision(
            State.ERROR_HOLD,
            ["quote_target_missing"] + assessment.reasons[:2],
            assessment.level,
            True,
            next_action="human",
        )

    draft = ""
    desire_data: dict[str, Any] = {}
    desire_text = ""
    if two_step:
        # 媒体消息先读"她想要什么"，再写回复；文本消息跳过这一步省成本
        if msg.content_type in ("video", "image", "audio", "mixed"):
            try:
                desire_data = classifier.desire(msg, persona, recent, facts, stats_text)
                desire_text = desire_mod.render_for_draft(desire_data)
                store.set_desire(task_id, desire_data)
            except ClassifyError as exc:
                store.audit("desire_failed", task_id, {"error": str(exc)[:200]})

        # 只有"会走到人面前"的任务才值得花第二次调用；R3 直接不写草稿
        if (
            decision.state
            in (State.PREPARING_REPLY, State.WAITING_APPROVAL, State.VERIFYING_QUOTE)
            and assessment.level is not RiskLevel.R3
        ):
            try:
                if msg.content_type == "text" and settings.get("reply", {}).get("text_simple", True):
                    draft = classifier.draft_simple(
                        msg,
                        persona,
                        recent,
                        notes_text=_notes_text(base, msg.contact_id),
                        max_chars=int(settings.get("reply", {}).get("text_max_chars", 20)),
                    )
                else:
                    draft = classifier.draft(
                        msg, analysis, persona, recent, facts, stats_text, desire_text=desire_text
                    )
            except ClassifyError as exc:
                store.audit("draft_failed", task_id, {"error": str(exc)[:200]})
    else:
        draft = str(analysis.raw.get("reply_text", "")) if analysis.source == "model" else ""
    decision.draft_text = draft

    # 自动模式：R0 且通过限流 → 直接进待发送；需要引用的交给发送端现场校验引用条
    if decision.state is State.PREPARING_REPLY and draft.strip():
        if msg.requires_quote:
            decision.state = State.VERIFYING_QUOTE
            decision.requires_human = False
            decision.reasons.append("auto_quote_at_send")
        else:
            decision.state = State.READY_TO_SEND
            decision.requires_human = False
            decision.reasons.append("auto_send")
    try:
        _advance_toward(store, task_id, decision.state)
        store.apply_decision(task_id, decision, draft=draft)
    except StoreError as exc:
        return {"task_id": task_id, "error": str(exc)}

    store.set_confidence(task_id, analysis.confidence)

    if decision.state in (State.WAITING_APPROVAL, State.VERIFYING_QUOTE):
        store.enqueue_approval(task_id)

    return {
        "task_id": task_id,
        "decision": decision.to_dict(),
        "analysis": analysis.to_dict(),
        "risk": assessment.to_dict(),
        "desire": desire_data,
        "duplicate_of": None,
    }


def cmd_decide(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    try:
        return emit(_decide_one(store, settings, base, args.task_id, args.offline))
    except StoreError as exc:
        return fail(str(exc))


def cmd_run(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """适配器的主入口：观测一批消息 → 立刻给出决策。"""
    store = Store(db_path(base, settings, args.db))
    payload = read_payload()
    observed = _observe_messages(store, settings, payload, args.allow_all)
    decisions = []
    for item in observed["created"]:
        try:
            decisions.append(
                _decide_one(store, settings, base, item["task_id"], args.offline)
            )
        except StoreError as exc:
            decisions.append({"task_id": item["task_id"], "error": str(exc)})
    observed["decisions"] = decisions
    return emit(observed)


def cmd_list(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    return emit(store.list_tasks(platform=args.platform, state=args.state, limit=args.limit))


def cmd_status(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    return emit({"task": task, "counts": store.counts()})


def cmd_queue(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    items = approval_mod.pending(store)
    if args.human:
        for it in items:
            sys.stderr.write(approval_mod.render_card(it) + "\n")
        sys.stderr.write(f"共 {len(items)} 条待审\n")
    return emit(items)


def cmd_approve(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    try:
        return emit(approval_mod.approve(store, args.task_id, args.by, args.note))
    except StoreError as exc:
        return fail(str(exc))


def cmd_reject(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    try:
        return emit(approval_mod.reject(store, args.task_id, args.by, args.note))
    except StoreError as exc:
        return fail(str(exc))


def cmd_mark_sent(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    try:
        state = store.mark_sent(args.task_id, args.status, args.evidence, args.detail)
    except StoreError as exc:
        return fail(str(exc))
    task = store.get_task(args.task_id)
    if task and args.status == "verified" and task.get("draft_text"):
        Memory(store, settings).record_outgoing(task["platform"], task["contact_id"], task["draft_text"])
    return emit({"task_id": args.task_id, "state": state.value})


def cmd_pause(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    store.set_pause(args.scope, True, by=args.by)
    return emit({"paused": args.scope})


def cmd_resume(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    store.set_pause(args.scope, False, by=args.by)
    return emit({"resumed": args.scope})


def cmd_presend(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    effective = copy.deepcopy(settings)
    if getattr(args, "live", False):
        # 显式 live：只对本次这条任务忽略 dry_run，其它闸门照旧
        effective["app"]["dry_run"] = False
    blockers = verify_mod.pre_send_blockers(task, effective, auto_quote=getattr(args, "auto_quote", False))
    return emit({"task_id": args.task_id, "blockers": blockers, "allowed": not blockers},
                code=0 if not blockers else 3)


def cmd_cover(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """把一条任务标记为"已被同一批的另一条回复覆盖"，避免重复回。"""
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    if task["state"] in ("DONE", "CANCELLED", "FAILED_CONFIRMED"):
        return emit({"task_id": args.task_id, "state": task["state"], "skipped": True})
    try:
        _advance_toward(store, args.task_id, State.CANCELLED)
        store.set_state(args.task_id, State.CANCELLED, f"covered_by:{args.by}")
    except StoreError as exc:
        return fail(str(exc))
    return emit({"task_id": args.task_id, "state": "CANCELLED", "covered_by": args.by})


def cmd_outcome(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """记录一次发送结果，用于连续失败熔断。"""
    store = Store(db_path(base, settings, args.db))
    if args.ok:
        limits_mod.record_success(store, args.platform)
        return emit({"platform": args.platform, "ok": True})
    tripped = limits_mod.record_failure(store, settings, args.platform)
    return emit({"platform": args.platform, "ok": False, "circuit_open": tripped})


def cmd_profile(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """离线分析历史聊天，产出 config/relation_notes.json（给回复用）。"""
    payload = read_payload()
    stats = payload.get("stats") or {}
    pairs = payload.get("pairs") or []
    classifier = Classifier(settings, base)
    try:
        notes = classifier.profile(stats, pairs)
    except ClassifyError as exc:
        return fail(str(exc))
    out = base / "config" / "relation_notes.json"
    out.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    return emit(
        {
            "ok": True,
            "path": str(out),
            "counts": {k: len(v) for k, v in notes.items() if isinstance(v, list)},
            "confidence": notes.get("confidence"),
        }
    )


def cmd_allow_reply(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    ok, why = limits_mod.reply_allowed(store, settings, args.platform, args.contact)
    return emit({"allowed": ok, "reason": why})


def cmd_auto_ready(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    max_risk = str(settings.get("reply", {}).get("auto_max_risk", "R1"))
    only_media = bool(settings.get("reply", {}).get("only_media", True))
    return emit(
        store.auto_ready(
            platform=args.platform, limit=args.limit, max_risk=max_risk, only_media=only_media
        )
    )


def cmd_reconcile(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """人工复核后修正异常态（主要给 SEND_UNKNOWN 用）。"""
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    try:
        store.set_state(args.task_id, State(args.to), f"reconcile: {args.note}"[:300])
    except StoreError as exc:
        return fail(str(exc))
    updated = store.get_task(args.task_id)
    return emit({"task_id": args.task_id, "state": (updated or {}).get("state")})


def cmd_attach_media(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    if not args.summary and not args.quote_target:
        return fail("summary 和 quote-target 至少要给一个", 2)
    store.attach_media(args.task_id, args.summary or None, args.quote_target or None)
    return emit(
        {
            "task_id": args.task_id,
            "media_summary": (args.summary or "")[:120],
            "quote_target": (args.quote_target or "")[:120],
        }
    )


def cmd_prepare(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """人工审核通过后，把任务推进到 READY_TO_SEND。"""
    store = Store(db_path(base, settings, args.db))
    task = store.get_task(args.task_id)
    if not task:
        return fail(f"task not found: {args.task_id}", 2)
    if task["state"] != State.PREPARING_REPLY.value:
        return fail(f"状态不是 PREPARING_REPLY: {task['state']}", 3)
    if not (task.get("draft_text") or "").strip():
        return fail("没有草稿，拒绝进入待发送", 3)
    if task.get("requires_quote") and not task.get("quote_target_verified"):
        return fail("需要引用但引用目标未验证", 3)
    store.set_state(args.task_id, State.READY_TO_SEND, f"prepare by {args.by}")
    store.audit("prepare", args.task_id, {"by": args.by})
    return emit({"task_id": args.task_id, "state": State.READY_TO_SEND.value})


def cmd_quote_check(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    payload = read_payload()
    check = verify_mod.verify_quote(
        payload.get("expected", {}), payload.get("observed", {}), settings
    )
    return emit(check.to_dict(), code=0 if check.ok else 3)


def cmd_send_check(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    payload = read_payload()
    result = verify_mod.classify_send(payload)
    return emit(result.to_dict(), code=0 if result.ok else 3)


def cmd_memory(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    store = Store(db_path(base, settings, args.db))
    mem = Memory(store, settings)
    if args.action == "add":
        mem.remember_fact(args.platform, args.contact, args.text, source=args.source)
        return emit({"ok": True})
    if args.action == "recent":
        return emit(mem.recent(args.platform, args.contact, args.limit))
    if args.action == "facts":
        return emit(mem.facts(args.platform, args.contact, keyword=args.keyword or ""))
    if args.action == "clear":
        n = mem.clear(args.platform, args.contact)
        return emit({"cleared": n})
    return fail("未知 action", 2)


def cmd_config_check(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    problems: list[str] = []
    try:
        persona = PersonaStore(base / "config" / "persona.json").persona
    except Exception as exc:  # noqa: BLE001
        problems.append(f"persona: {exc}")
        persona = {}
    if not settings.get("reply", {}).get("allowlisted_contacts", {}).get("wechat"):
        problems.append("wechat 白名单为空（默认不处理任何人，这是安全的）")
    return emit(
        {
            "ok": not [p for p in problems if not p.endswith("（默认不处理任何人，这是安全的）")],
            "dry_run": settings.get("app", {}).get("dry_run"),
            "mode": settings.get("reply", {}).get("mode"),
            "persona_id": persona.get("id"),
            "allowlist": settings.get("reply", {}).get("allowlisted_contacts"),
            "notes": problems,
        }
    )


def cmd_selftest(args: argparse.Namespace, base: Path, settings: dict[str, Any]) -> int:
    """离线端到端自检：不联网、不碰微信/抖音、不发任何消息。"""
    steps: list[dict[str, Any]] = []
    tmp = Path(tempfile.mkdtemp(prefix="core_selftest_"))
    local = deep_merge(
        settings,
        {
            "app": {"dry_run": True},
            "reply": {"mode": "manual", "allowlisted_contacts": {"wechat": ["__selftest__"], "douyin": []}},
        },
    )
    store = Store(tmp / "test.db")

    payload = {
        "platform": "wechat",
        "contact_id": "__selftest__",
        "contact_verified": True,
        "messages": [
            {"content_type": "text", "text": "今天好累啊", "source_message_id": "st-1"},
        ],
    }
    observed = _observe_messages(store, local, payload, allow_all=False)
    steps.append({"step": "observe", "created": len(observed["created"])})

    again = _observe_messages(store, local, payload, allow_all=False)
    steps.append({"step": "observe_again", "created": len(again["created"]), "duplicates": len(again["duplicates"])})

    task_id = observed["created"][0]["task_id"]
    d1 = _decide_one(store, local, base, task_id, offline=True)
    steps.append({"step": "decide", "state": d1["decision"]["state"], "risk": d1["risk"]["level"], "reasons": d1["risk"]["reasons"][:4]})

    approval_mod.approve(store, task_id, "selftest")
    steps.append({"step": "approve", "state": store.get_task(task_id)["state"]})

    store.set_state(task_id, State.READY_TO_SEND, "selftest")
    blockers = verify_mod.pre_send_blockers(store.get_task(task_id), local)
    steps.append({"step": "presend", "blockers": blockers})

    send = verify_mod.classify_send({"input_cleared": True})
    steps.append({"step": "send_check_input_cleared_only", "status": send.status})

    store.set_state(task_id, State.SENDING, "selftest")
    state = store.mark_sent(task_id, "verified", "ui_verified", "selftest bubble_seen")
    steps.append({"step": "mark_sent", "state": state.value})

    risky = {
        "platform": "wechat",
        "contact_id": "__selftest__",
        "contact_verified": True,
        "messages": [{"content_type": "text", "text": "能不能先借我五百，我转账还你", "source_message_id": "st-2"}],
    }
    observed2 = _observe_messages(store, local, risky, allow_all=False)
    d2 = _decide_one(store, local, base, observed2["created"][0]["task_id"], offline=True)
    steps.append({"step": "risky", "state": d2["decision"]["state"], "risk": d2["risk"]["level"]})

    bad = Analysis.fallback("selftest_invalid")
    steps.append({"step": "fallback_is_not_r0", "risk": bad.risk_level.value, "requires_human": bad.requires_human})

    ok = (
        steps[1]["created"] == 0
        and steps[1]["duplicates"] == 1
        and steps[2]["state"] == "WAITING_APPROVAL"
        and steps[3]["state"] == "PREPARING_REPLY"
        and "dry_run" in steps[4]["blockers"]
        and steps[5]["status"] == "unknown"
        and steps[6]["state"] == "DONE"
        and steps[7]["risk"] == "R2"
        and steps[7]["state"] == "WAITING_APPROVAL"
        and steps[8]["risk"] == "R1"
    )
    for f in tmp.glob("*"):
        try:
            f.unlink()
        except OSError:
            pass
    try:
        tmp.rmdir()
    except OSError:
        pass
    return emit({"ok": ok, "steps": steps}, code=0 if ok else 1)


# ------------------------------------------------------------------ 解析

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="core.cli", description="共享 AI 核心（只做决策，不发消息）")
    ap.add_argument("--base", default=str(BASE), help="核心根目录")
    ap.add_argument("--db", default=None, help="覆盖数据库路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化数据库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("observe", help="登记观测到的消息（stdin JSON）")
    p.add_argument("--allow-all", action="store_true", help="测试用：忽略白名单")
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("decide", help="对一条任务做分析与路由")
    p.add_argument("--task-id", required=True)
    p.add_argument("--offline", action="store_true", help="不联网，用占位模型（自检/测试）")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("run", help="观测 + 决策（适配器主入口）")
    p.add_argument("--allow-all", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list", help="列出任务")
    p.add_argument("--platform", default="")
    p.add_argument("--state", default="")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("status", help="查看单个任务")
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("queue", help="人工审核队列")
    p.add_argument("--human", action="store_true", help="额外输出可读卡片到 stderr")
    p.set_defaults(func=cmd_queue)

    for name, fn in (("approve", cmd_approve), ("reject", cmd_reject)):
        p = sub.add_parser(name, help=f"{name} 一条待审任务")
        p.add_argument("--task-id", required=True)
        p.add_argument("--by", default="user")
        p.add_argument("--note", default="")
        p.set_defaults(func=fn)

    p = sub.add_parser("mark-sent", help="登记发送结果")
    p.add_argument("--task-id", required=True)
    p.add_argument("--status", required=True, choices=["verified", "unknown", "failed"])
    p.add_argument("--evidence", default="ui_observed")
    p.add_argument("--detail", default="")
    p.set_defaults(func=cmd_mark_sent)

    p = sub.add_parser("presend", help="发送前硬性检查")
    p.add_argument("--task-id", required=True)
    p.add_argument("--live", action="store_true", help="本次忽略 dry_run（其余闸门不变）")
    p.add_argument("--auto-quote", action="store_true",
                   help="允许 VERIFYING_QUOTE 任务由发送端现场校验引用")
    p.set_defaults(func=cmd_presend)

    p = sub.add_parser("auto-ready", help="自动模式可发送的任务")
    p.add_argument("--platform", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_auto_ready)

    p = sub.add_parser("allow-reply", help="限流检查（同会话间隔/日上限/熔断）")
    p.add_argument("--platform", required=True)
    p.add_argument("--contact", required=True)
    p.set_defaults(func=cmd_allow_reply)

    p = sub.add_parser("profile", help="分析历史聊天，生成关系笔记（stdin JSON）")
    p.add_argument("--contact", default="")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("cover", help="标记为被同批另一条回复覆盖")
    p.add_argument("--task-id", required=True)
    p.add_argument("--by", required=True)
    p.set_defaults(func=cmd_cover)

    p = sub.add_parser("outcome", help="记录发送成败（熔断计数）")
    p.add_argument("--platform", required=True)
    p.add_argument("--ok", action="store_true")
    p.set_defaults(func=cmd_outcome)

    p = sub.add_parser("prepare", help="审核通过后推进到 READY_TO_SEND")
    p.add_argument("--task-id", required=True)
    p.add_argument("--by", default="user")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("attach-media", help="写入适配器取到的媒体证据")
    p.add_argument("--task-id", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--quote-target", default="")
    p.set_defaults(func=cmd_attach_media)

    p = sub.add_parser("reconcile", help="人工复核后修正异常态")
    p.add_argument("--task-id", required=True)
    p.add_argument("--to", required=True, choices=["DONE", "ERROR_HOLD", "FAILED_CONFIRMED"])
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("quote-check", help="引用目标校验（stdin JSON）")
    p.set_defaults(func=cmd_quote_check)

    p = sub.add_parser("send-check", help="发送结果归一化（stdin JSON）")
    p.set_defaults(func=cmd_send_check)

    p = sub.add_parser("pause", help="暂停某个平台或全部")
    p.add_argument("--scope", default="all", choices=["all", "wechat", "douyin"])
    p.add_argument("--by", default="user")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="恢复")
    p.add_argument("--scope", default="all", choices=["all", "wechat", "douyin"])
    p.add_argument("--by", default="user")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("memory", help="记忆管理")
    p.add_argument("action", choices=["add", "recent", "facts", "clear"])
    p.add_argument("--platform", required=True)
    p.add_argument("--contact", required=True)
    p.add_argument("--text", default="")
    p.add_argument("--source", default="user")
    p.add_argument("--keyword", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("config-check", help="校验配置与人设")
    p.set_defaults(func=cmd_config_check)

    p = sub.add_parser("selftest", help="离线端到端自检（不联网、不发消息）")
    p.set_defaults(func=cmd_selftest)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    base = Path(args.base)
    settings = load_settings(base)
    try:
        return args.func(args, base, settings)
    except (StoreError, RuntimeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
