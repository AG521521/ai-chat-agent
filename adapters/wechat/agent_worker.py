"""微信适配器（阶段 B）：把现有事件流喂给共享核心。

边界（刻意做得很窄）：
* **只读** ``events/YYYY-MM-DD.jsonl``，不碰微信窗口、不点、不输入、不发消息；
* 联系人与白名单由**核心**判定，这里不自己放行；
* 媒体（视频/图片）本阶段不处理，只标注类型，走"证据不完整 → 人工"；
* 所有状态落在 ``D:\\ai_chat_core\\var\\agent.db``，不在本工程写任何文件。

用法::

    python src/agent_worker.py --offline          # 不联网，验证链路
    python src/agent_worker.py --limit 2          # 真调模型，但只出稿
    python src/agent_worker.py --day 2026-10-29
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
CORE = Path(r"D:\ai_chat_core")
PYTHON = r"C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe"
EVENTS = ROOT / "events"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VIDEO_MARK = "视频"


class CoreError(RuntimeError):
    pass


def core_call(args: list[str], payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """调用核心 CLI。核心只输出 JSON，stderr 才是错误。"""
    cmd = [PYTHON, "-m", "core.cli", *args]
    proc = subprocess.run(
        cmd,
        cwd=str(CORE),
        input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    out = (proc.stdout or "").strip()
    data: Any = {}
    if out.startswith("{") or out.startswith("["):
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {"raw": out[:500]}
    if proc.returncode not in (0, 3):
        raise CoreError((proc.stderr or out or f"exit={proc.returncode}")[:500])
    return proc.returncode, data


def normalize_name(raw: str) -> str:
    """OCR 会在中文之间插空格；去掉空白后再比对。"""
    return re.sub(r"\s+", "", raw or "")


def load_events(day: str) -> list[dict[str, Any]]:
    path = EVENTS / f"{day}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def to_message(event: dict[str, Any]) -> dict[str, Any]:
    preview = event.get("preview") or ""
    is_video = VIDEO_MARK in preview
    return {
        "content_type": "video" if is_video else "text",
        "text": preview,
        "observed_at": event.get("ts") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # 微信读屏拿不到平台原生消息 ID，这里刻意留空，让核心用内容指纹去重，
        # 而不是伪造一个"看起来像原生 ID"的强键。
        "source_message_id": None,
        "requires_quote": is_video,
        "quote_target_verified": False,
        "evidence_level": "ui_observed",
        "raw_ref": {
            "event_kind": event.get("kind"),
            "event_time": event.get("time"),
            "screenshot": event.get("screenshot"),
        },
    }


def group_by_contact(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        name = normalize_name(ev.get("name") or "")
        if not name:
            continue
        grouped.setdefault(name, []).append(ev)
    return grouped


def dispatch(task_id: str, do_send: bool, live: bool, auto_quote: bool = False) -> dict[str, Any]:
    """把一条任务交给发送器，并把结果回写核心。"""
    _, data = core_call(["status", "--task-id", task_id])
    task = (data or {}).get("task")
    if not task:
        raise CoreError(f"核心库里没有任务 {task_id}")
    state = task.get("state")
    if state not in ("READY_TO_SEND", "VERIFYING_QUOTE"):
        return {"task_id": task_id, "error": f"状态不可发送: {state}", "sent": False}

    presend_args = ["presend", "--task-id", task_id]
    if live:
        presend_args.append("--live")
    if auto_quote:
        presend_args.append("--auto-quote")
    _, pre = core_call(presend_args)
    blockers = (pre or {}).get("blockers", [])
    if blockers:
        return {"task_id": task_id, "blocked": blockers, "sent": False}

    if do_send:
        # 限流放在真正发送前：同会话间隔 / 日上限 / 熔断
        _, allow = core_call(
            ["allow-reply", "--platform", "wechat", "--contact", str(task["contact_id"])]
        )
        if not (allow or {}).get("allowed", False):
            return {
                "task_id": task_id,
                "blocked": [(allow or {}).get("reason") or "rate_limited"],
                "sent": False,
            }

    import src.wechat_send as sender  # 同目录，延迟导入避免无谓依赖

    outcome = sender.send(
        contact=str(task["contact_id"]),
        text=str(task.get("draft_text") or ""),
        do_send=do_send,
        quote_text=str(task.get("quote_target") or "") if task.get("requires_quote") else "",
        quote_expect=str(task.get("media_summary") or "") if task.get("requires_quote") else "",
    )
    result = {"task_id": task_id, **outcome.to_dict()}

    if do_send and outcome.status in ("verified", "unknown", "failed"):
        mapping = {
            "verified": ("verified", "ui_verified"),
            "unknown": ("unknown", "ui_observed"),
            "failed": ("failed", "ui_observed"),
        }
        status, evidence = mapping[outcome.status]
        core_call(
            [
                "mark-sent",
                "--task-id",
                task_id,
                "--status",
                status,
                "--evidence",
                evidence,
                "--detail",
                outcome.detail[:200],
            ]
        )
        core_call(["outcome", "--platform", "wechat"] + (["--ok"] if status == "verified" else []))
        _, after = core_call(["status", "--task-id", task_id])
        result["state_after"] = ((after or {}).get("task") or {}).get("state")
    return result


def enrich_media(created: list[dict[str, Any]], contact: str) -> dict[str, Any]:
    """对视频/图片任务，从界面读卡片文案写回核心（阶段 D）。"""
    targets = [c for c in created if c.get("content_type") in ("video", "image")]
    if not targets:
        return {"enriched": 0}
    try:
        import src.wechat_media as media
    except Exception as exc:  # noqa: BLE001
        return {"enriched": 0, "error": f"媒体模块不可用: {exc}"}

    observed = media.inspect(contact)
    latest = observed.get("latest") or {}
    caption = ((observed.get("caption") or {}).get("caption") or "").strip()
    thumb = (latest.get("text") or "").strip()
    # 优先用引用条里的完整文案（比缩略图 OCR 干净得多）；没有再退回缩略图文字
    text = caption or thumb
    if not text:
        return {"enriched": 0, "error": observed.get("error") or "没读到媒体卡片文案"}

    # 引用定位需要一个"单行里出现"的短探针：取卡片最长的一行
    lines = [ln.strip() for ln in (latest.get("lines") or []) if ln.strip()]
    probe = max(lines, key=len) if lines else (thumb[:32] or text[:32])

    enriched = 0
    for item in targets:
        try:
            core_call(
                [
                    "attach-media",
                    "--task-id",
                    item["task_id"],
                    "--summary",
                    text[:400],
                    "--quote-target",
                    probe[:120],
                ]
            )
            enriched += 1
        except CoreError:
            continue
    return {
        "enriched": enriched,
        "media_chars": len(text),
        "quote_probe": probe[:40],
        "caption_source": "quote_bar" if caption else "thumbnail_ocr",
        "evidence": latest.get("evidence"),
    }


def run(day: str, limit: int, offline: bool) -> dict[str, Any]:
    events = load_events(day)
    grouped = group_by_contact(events)
    summary: dict[str, Any] = {
        "day": day,
        "events": len(events),
        "contacts": {},
        "offline": offline,
        "sent_anything": False,
    }
    used = 0
    for contact, evs in grouped.items():
        # 同一会话按时间正序，保证"近期上下文"的顺序是对的
        evs.sort(key=lambda e: e.get("ts") or "")
        payload = {
            "platform": "wechat",
            # 事件来自经头像指纹校验过的 B 号窗口，所以可以断言"联系人窗口正确"；
            # 昵称是否在白名单里，仍然交给核心判断。
            "contact_verified": True,
            "contact_id": contact,
            "messages": [to_message(e) for e in evs],
        }
        _, data = core_call(["observe"], payload)
        created = (data or {}).get("created", [])
        duplicates = (data or {}).get("duplicates", [])
        media_info = enrich_media(created, contact) if created else {"enriched": 0}

        decisions: list[dict[str, Any]] = []
        for item in created:
            if limit and used >= limit:
                break
            used += 1
            decide_args = ["decide", "--task-id", item["task_id"]]
            if offline:
                decide_args.append("--offline")
            try:
                _, d = core_call(decide_args)
                decisions.append(d)
            except CoreError as exc:
                decisions.append({"task_id": item["task_id"], "error": str(exc)})

        entries = []
        for item in decisions:
            dec = item.get("decision") or {}
            risk = item.get("risk") or {}
            entries.append(
                {
                    "task_id": item.get("task_id"),
                    "state": dec.get("state"),
                    "risk": risk.get("level"),
                    "reasons": (risk.get("reasons") or [])[:4],
                    "draft_chars": len((dec.get("draft_text") or "").strip()),
                    "error": item.get("error"),
                }
            )
        summary["contacts"][contact] = {
            "allowlisted": (data or {}).get("allowlisted"),
            "created": len(created),
            "duplicates": len(duplicates),
            "media": media_info,
            "tasks": entries,
        }
        if limit and used >= limit:
            summary["stopped_by_limit"] = True
            break

    code, queue = core_call(["queue"])
    summary["pending_approvals"] = len(queue) if isinstance(queue, list) else 0
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="微信 → 共享核心（只读、只出稿、不发送）")
    ap.add_argument("--day", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条任务，0 表示不限")
    ap.add_argument("--offline", action="store_true", help="不调用真实模型")
    ap.add_argument("--dispatch-task", default="", help="把指定任务交给发送器")
    ap.add_argument("--send", action="store_true", help="dispatch 时真的发送")
    ap.add_argument("--live", action="store_true", help="dispatch 时忽略 dry_run（仅本条）")
    args = ap.parse_args()

    try:
        if args.dispatch_task:
            summary = dispatch(args.dispatch_task, args.send, args.live)
        else:
            summary = run(args.day, args.limit, args.offline)
    except (CoreError, subprocess.SubprocessError, OSError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc)}, ensure_ascii=False) + "\n")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
