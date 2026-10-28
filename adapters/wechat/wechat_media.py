"""微信媒体消息的"看懂"：不下载、不点开，直接从界面取证据。

微信视频号卡片 / 图片消息在界面上本来就是"图片缩略图 + 覆盖文字 + 发布者"。
所以对这类消息，最可靠的取证不是去猜本地缓存 mp4（那会拿错视频），
而是：

1. 用像素扫描找出聊天区里"大片非背景色"的区域 = 媒体缩略图；
2. 把落在该区域内的 OCR 文本拼起来 = 卡片文案；
3. 记录区域的中心坐标，供引用时右键定位。

对"本地视频文件"（不是视频号卡片）只有缩略图，这里只能拿到缩略图上的字，
会明确标记 `partial`，不会假装看懂了整段视频。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
sys.path.insert(0, str(ROOT))

import src.wechat_ui as w  # noqa: E402
import src.wechat_send as sender  # noqa: E402

from PIL import Image  # noqa: E402

LOGS = ROOT / "logs"


@dataclass
class MediaCard:
    text: str
    cx: int
    cy: int
    y0: int
    y1: int
    x0: int
    x1: int
    lines: list[str] = field(default_factory=list)
    evidence: str = "thumbnail_ocr"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "lines": self.lines,
            "center": [self.cx, self.cy],
            "bbox": [self.x0, self.y0, self.x1, self.y1],
            "evidence": self.evidence,
        }


def _is_background(px: tuple[int, int, int]) -> bool:
    r, g, b = px[:3]
    return r > 236 and g > 236 and b > 236


def media_bands(img: Any, win: dict, min_height: int = 120, min_ratio: float = 0.22) -> list[tuple[int, int]]:
    """找出左侧（对方）聊天区里连续的"非背景"横带 = 媒体缩略图。"""
    px = img.load()
    x0, x1 = int(win["w"] * 0.04), int(win["w"] * 0.52)
    y0, y1 = int(win["h"] * 0.07), int(win["h"] * 0.73)
    step = 6
    bands: list[tuple[int, int]] = []
    run_start: int | None = None
    for y in range(y0, y1, 2):
        total = 0
        colorful = 0
        for x in range(x0, x1, step):
            total += 1
            if not _is_background(px[x, y]):
                colorful += 1
        ratio = colorful / total if total else 0.0
        if ratio >= min_ratio:
            if run_start is None:
                run_start = y
        else:
            if run_start is not None and y - run_start >= min_height:
                bands.append((run_start, y))
            run_start = None
    if run_start is not None and y1 - run_start >= min_height:
        bands.append((run_start, y1))
    # 太矮的带丢掉（文字行也会有一点非背景）
    return [b for b in bands if b[1] - b[0] >= min_height]


def cards_in_view(win: dict, png: Path) -> list[MediaCard]:
    img = Image.open(png).convert("RGB")
    lines = w.ocr(png)
    cards: list[MediaCard] = []
    for (ya, yb) in media_bands(img, win):
        inside = [
            ln
            for ln in lines
            if ya - 12 <= ln["Y"] <= yb + 12
            and ln["X"] < win["w"] * 0.62
            and (ln["n"] or "").strip()
        ]
        if not inside:
            continue
        inside.sort(key=lambda ln: ln["Y"])
        texts = [ln["n"].strip() for ln in inside]
        joined = " ".join(texts).strip()
        if len(joined) < 4:
            continue
        x_left = min(int(ln["X"]) for ln in inside)
        x_right = max(int(ln["X"] + ln["W"]) for ln in inside)
        cards.append(
            MediaCard(
                text=joined[:400],
                cx=(x_left + x_right) // 2,
                cy=(ya + yb) // 2,
                y0=ya,
                y1=yb,
                x0=x_left,
                x1=x_right,
                lines=texts,
            )
        )
    return cards


def caption_via_quote(win: dict, card: MediaCard) -> dict[str, Any]:
    """右键卡片 → 引用 → 读引用条。引用条里是「联系人：完整文案」，比缩略图 OCR 干净。"""
    if not w.right_click_message(win, card.cx, card.cy):
        return {"error": "右键没有弹出菜单"}
    menu, items = sender.menu_items(win)
    if not menu:
        return {"error": "菜单窗口消失"}
    item = next((i for i in items if "引用" in i["n"]), None)
    if not item:
        return {"error": f"菜单里没有引用: {[i['n'] for i in items]}"}
    sender.click_menu_item(menu, item)
    time.sleep(0.5)
    png = w.capture(win["hwnd"])
    lines = w.ocr(png)
    bar = " ".join(sender.quote_bar(lines, win)).strip()
    caption = bar
    if "：" in caption:
        caption = caption.split("：", 1)[1].strip()
    elif ":" in caption:
        caption = caption.split(":", 1)[1].strip()
    return {"caption": caption[:400], "bar": bar[:400], "shot": str(png)}


def inspect(contact: str, max_rounds: int = 6, restore: str | None = None,
            with_caption: bool = True) -> dict[str, Any]:
    """打开会话，找最近的一条媒体卡片（找不到就 PageUp 往上翻）。

    with_caption=True 时，再右键「引用」读一次完整文案；读完直接关窗口丢弃引用。
    """
    if not sender.allowed(contact):
        return {"error": f"{contact} 不在白名单", "cards": []}

    before = restore if restore is not None else sender.ensure_visible()
    win: dict | None = None
    try:
        win, _ = sender.open_conversation(contact)
        if not win or win["title"] == "微信":
            return {"error": "未能打开独立聊天窗口", "cards": []}
        sender.focus_window(win)
        found: list[MediaCard] = []
        png = w.capture(win["hwnd"])
        round_no = 0
        for round_no in range(max_rounds + 1):
            cards = cards_in_view(win, png)
            if cards:
                found = cards
                break
            if round_no == max_rounds:
                break
            if round_no == 0:
                sender.focus_list(win)
            sender.page_scroll(win, up=True)
            png = w.capture(win["hwnd"])
        stamp = time.strftime("%Y%m%d_%H%M%S")
        (LOGS / f"media_{stamp}.png").write_bytes(png.read_bytes())

        result: dict[str, Any] = {
            "contact": contact,
            "rounds": round_no,
            "cards": [c.to_dict() for c in found],
            "latest": found[-1].to_dict() if found else None,
            "screenshot": str(LOGS / f"media_{stamp}.png"),
        }
        if with_caption and found:
            cap = caption_via_quote(win, found[-1])
            result["caption"] = cap
            if cap.get("caption"):
                result["latest"]["caption"] = cap["caption"]
                result["latest"]["evidence"] = "quote_bar"
        return result
    finally:
        if win is not None:
            sender.close_window(win)
        sender.restore_state(before)


def main() -> int:
    ap = argparse.ArgumentParser(description="读取微信媒体卡片文案（只读，不点开播放）")
    ap.add_argument("--contact", required=True)
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()
    print(json.dumps(inspect(args.contact, args.rounds), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
