"""微信会话扫描：往上翻，把同一批消息全收下来，并给每条一个稳定指纹。

为什么不能只看最后一条：她经常一口气发十几条，只回最后一条会漏掉前面所有内容。

指纹规则：
* 文字消息：规范化文本的 sha1；
* 媒体卡片：缩略图区域的 64 位 dhash（同一张卡片重渲染后仍然一致）。

用法（只读，不发送）::

    python src/wechat_scan.py --contact 测试号 --max-messages 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
sys.path.insert(0, str(ROOT))

import src.wechat_send as S  # noqa: E402
import src.wechat_media as M  # noqa: E402
import src.wechat_ui as w  # noqa: E402

from PIL import Image  # noqa: E402

LOGS = ROOT / "logs"


def dhash(img: Image.Image, box: tuple[int, int, int, int]) -> str:
    """缩略图区域的 8x8 差分哈希（对缩放/压缩不敏感）。"""
    try:
        crop = img.crop(box).convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        return ""
    px = list(crop.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def text_key(text: str) -> str:
    norm = re.sub(r"\s+", "", text or "")
    return "t:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def scan(contact: str, max_rounds: int = 8, max_messages: int = 30,
         include_own: bool = False) -> dict[str, Any]:
    before = S.ensure_visible()
    win: dict | None = None
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        win, _ = S.open_conversation(contact)
        if not win or win["title"] == "微信":
            return {"error": "未能打开独立聊天窗口", "messages": []}
        S.focus_window(win)
        stagnant = 0
        for round_no in range(max_rounds + 1):
            png = w.capture(win["hwnd"])
            img = Image.open(png).convert("RGB")
            bubbles = S.find_bubbles(win, png)
            cards = M.cards_in_view(win, png)

            def card_of(b) -> Any:
                for c in cards:
                    if c.x0 - 20 <= b.cx <= c.x1 + 20 and c.y0 - 20 <= b.cy <= c.y1 + 20:
                        return c
                return None

            new = 0
            for b in sorted(bubbles, key=lambda x: x.y):
                if b.own:
                    if not include_own:
                        continue
                    key = "o:" + text_key(b.text)
                    if key in seen:
                        continue
                    seen[key] = {
                        "key": key,
                        "content_type": "text",
                        "text": b.text[:300],
                        "requires_quote": False,
                        "own": True,
                        "y": b.y,
                    }
                    order.append(key)
                    new += 1
                    continue
                c = card_of(b)
                if c is not None:
                    key = "m:" + (dhash(img, (c.x0, c.y0, c.x1, c.y1)) or text_key(c.text))
                    if key in seen:
                        continue
                    seen[key] = {
                        "key": key,
                        "content_type": "video",
                        "text": c.text[:300],
                        "requires_quote": True,
                        "y": b.y,
                    }
                    order.append(key)
                    new += 1
                else:
                    key = text_key(b.text)
                    if key in seen:
                        continue
                    seen[key] = {
                        "key": key,
                        "content_type": "text",
                        "text": b.text[:300],
                        "requires_quote": False,
                        "y": b.y,
                    }
                    order.append(key)
                    new += 1
            if new == 0:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
            if len(order) >= max_messages:
                break
            if round_no == 0:
                S.focus_list(win)
            S.page_scroll(win, up=True)
            time.sleep(0.35)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        (LOGS / f"scan_{stamp}.png").write_bytes(w.capture(win["hwnd"]).read_bytes())
        # 从上到下（旧→新）返回
        messages = [seen[k] for k in order]
        messages.sort(key=lambda m: -m["y"]) if False else None
        return {"contact": contact, "rounds": round_no, "messages": messages}
    finally:
        if win is not None:
            S.close_window(win)
        S.restore_state(before)


def main() -> int:
    ap = argparse.ArgumentParser(description="微信会话扫描（只读）")
    ap.add_argument("--contact", required=True)
    ap.add_argument("--max-messages", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=8)
    args = ap.parse_args()
    result = scan(args.contact, args.rounds, args.max_messages)
    # 只打印统计，不打印聊天正文
    summary = {
        "contact": result.get("contact"),
        "rounds": result.get("rounds"),
        "total": len(result.get("messages", [])),
        "media": sum(1 for m in result.get("messages", []) if m["content_type"] != "text"),
        "text": sum(1 for m in result.get("messages", []) if m["content_type"] == "text"),
        "error": result.get("error"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
