"""F 阶段自动 runner（微信）：扫一整批 → 观测 → 出稿 → R0 自动发。

与手动模式的区别：
* 不是只处理最后一条：先往上翻，把同一批消息全收下来；
* 同一批里只回复**最后一条媒体消息**，其余（含文字）全部标记，避免刷屏；
* R0 直接发；R1/R2 留在审核队列；
* 每条发送前过 presend + 发送端的引用条现场校验。

用法：
    python src/auto_runner.py --contact 测试号 --once            # 预演
    python src/auto_runner.py --contact 测试号 --once --send --live
    python src/auto_runner.py --contact 测试号 --loop 300 --send --live
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
sys.path.insert(0, str(ROOT))

import src.agent_worker as A  # noqa: E402
import src.wechat_media as media  # noqa: E402
import src.wechat_scan as scan_mod  # noqa: E402

LOGS = ROOT / "logs"
RUN_LOG = LOGS / "auto_runner.log"


def log(obj: dict[str, Any]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with RUN_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] " + json.dumps(obj, ensure_ascii=False) + "\n")


def payload_from_scan(contact: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    out = []
    for m in messages:
        out.append(
            {
                "content_type": m["content_type"],
                "text": m.get("text", ""),
                "media_sig": m.get("key") if m["content_type"] != "text" else None,
                "requires_quote": bool(m.get("requires_quote")),
                "quote_target_verified": False,
                "evidence_level": "ui_observed",
                "raw_ref": {"key": m.get("key")},
            }
        )
    return {
        "platform": "wechat",
        "contact_id": contact,
        "contact_verified": True,
        "messages": out,
    }


def dispatch_ready(summary: dict[str, Any], do_send: bool, live: bool) -> None:
    """把已经就绪的自动任务发出去（无论这一轮有没有新消息都要做）。"""
    _, ready = A.core_call(["auto-ready", "--platform", "wechat", "--limit", "3"])
    tasks = ready if isinstance(ready, list) else []
    sent = []
    for t in tasks:
        tid = t.get("task_id")
        if not tid:
            continue
        try:
            res = A.dispatch(tid, do_send=do_send, live=live, auto_quote=True)
        except Exception as exc:  # noqa: BLE001
            res = {"task_id": tid, "error": str(exc)[:120]}
        sent.append({
            "task_id": tid,
            "status": res.get("status") or res.get("error"),
            "blocked": res.get("blocked"),
            "state_after": res.get("state_after"),
        })
    summary["sent"] = sent


def run_once(contact: str, do_send: bool, live: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {"contact": contact, "at": datetime.now().strftime("%H:%M:%S")}

    scanned = scan_mod.scan(contact)
    messages = scanned.get("messages") or []
    summary["scanned"] = len(messages)
    summary["media_in_batch"] = sum(1 for m in messages if m["content_type"] != "text")
    if not messages:
        summary["note"] = "没扫到消息"
        dispatch_ready(summary, do_send, live)
        return summary

    _, obs = A.core_call(["observe"], payload_from_scan(contact, messages))
    created = (obs or {}).get("created", [])
    summary["created"] = len(created)
    summary["duplicates"] = len((obs or {}).get("duplicates", []))
    if not created:
        summary["note"] = "这一批都处理过了"
        dispatch_ready(summary, do_send, live)
        return summary

    # 媒体任务补充文案：最新一张用引用条（干净），其余用缩略图文字
    media_tasks = [c for c in created if c.get("content_type") != "text"]
    if media_tasks:
        try:
            inspected = media.inspect(contact)
            caption = ((inspected.get("caption") or {}).get("caption") or "").strip()
            latest = inspected.get("latest") or {}
            lines = [x for x in (latest.get("lines") or []) if x.strip()]
            probe = max(lines, key=len) if lines else (latest.get("text") or "")[:32]
            if caption:
                A.core_call([
                    "attach-media", "--task-id", media_tasks[-1]["task_id"],
                    "--summary", caption[:400],
                    "--quote-target", (probe or caption)[:120],
                ])
                summary["caption_source"] = "quote_bar"
        except Exception as exc:  # noqa: BLE001
            summary["caption_error"] = str(exc)[:120]

    # 逐条决策
    decisions = []
    for item in created:
        try:
            _, d = A.core_call(["decide", "--task-id", item["task_id"]])
            decisions.append(d)
        except Exception as exc:  # noqa: BLE001
            decisions.append({"task_id": item["task_id"], "error": str(exc)[:120]})
    summary["states"] = {}
    for d in decisions:
        st = ((d.get("decision") or {}).get("state")) or "error"
        summary["states"][st] = summary["states"].get(st, 0) + 1

    # 同一批只回最后一条媒体；其余标记为已覆盖
    media_ids = [c["task_id"] for c in media_tasks]
    primary = None
    for tid in reversed(media_ids):
        st = next((((d.get("decision") or {}).get("state")) for d in decisions if d.get("task_id") == tid), None)
        if st in ("READY_TO_SEND", "VERIFYING_QUOTE", "WAITING_APPROVAL", "PREPARING_REPLY"):
            primary = tid
            break
    covered = 0
    for tid in media_ids:
        if tid == primary:
            continue
        try:
            A.core_call(["cover", "--task-id", tid, "--by", primary or "batch"])
            covered += 1
        except Exception:  # noqa: BLE001
            pass
    summary["primary"] = primary
    summary["covered"] = covered

    # 自动发送
    dispatch_ready(summary, do_send, live)
    return summary


def load_contacts() -> list[str]:
    """从 config/auto_contacts.json 读联系人（UTF-8，避开命令行编码问题）。"""
    path = ROOT / "config" / "auto_contacts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(x) for x in (data.get("wechat") or []) if str(x).strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="F 阶段自动 runner（微信）")
    ap.add_argument("--contact", default="", help="留空则读 config/auto_contacts.json")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="循环间隔秒数")
    ap.add_argument("--send", action="store_true", help="真的发送")
    ap.add_argument("--live", action="store_true", help="忽略 dry_run（本次运行）")
    args = ap.parse_args()

    contacts = [args.contact] if args.contact else load_contacts()
    if not contacts:
        print(json.dumps({"error": "没有可处理的联系人（config/auto_contacts.json 为空）"}, ensure_ascii=False))
        return 2

    while True:
        for contact in contacts:
            try:
                summary = run_once(contact, args.send, args.live)
                log(summary)
                print(json.dumps(summary, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                err = {"contact": contact, "error": str(exc)[:300],
                       "at": datetime.now().strftime("%H:%M:%S")}
                log(err)
                print(json.dumps(err, ensure_ascii=False))
        if args.once or not args.loop:
            break
        time.sleep(max(30, args.loop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
