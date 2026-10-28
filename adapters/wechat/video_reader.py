"""点开微信里的视频 -> 等它缓存到本地 -> 抽帧 + OCR 字幕 + 语音转写，读出整段文案。"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(r"D:\weixin_agent")))

import src.wechat_ui as w
from adapters.wechat_video import (newest_video, extract_frames, extract_audio,
                                   transcribe_audio)

ROOT = Path(r"D:\weixin_agent")


def play_and_wait(win: dict, x: int, y: int, wait: float = 6.0) -> bool:
    before = {x["hwnd"] for x in w.windows()}
    w.post_click(win, x, y)
    time.sleep(1.0)
    for _ in range(12):
        time.sleep(0.5)
        new = [x for x in w.windows() if x["hwnd"] not in before]
        if new:
            print("[play] player window:", new[0]["title"], new[0]["w"], "x", new[0]["h"])
            time.sleep(wait)
            # 关掉播放器：Esc 或点右上角关闭
            u = w.user32
            u.PostMessageW(new[0]["hwnd"], 0x0100, 0x1B, 0)   # Esc down
            u.PostMessageW(new[0]["hwnd"], 0x0101, 0x1B, 0)   # Esc up
            time.sleep(0.8)
            return True
    print("[play] no player window appeared")
    return False


def read_video(win: dict, x: int, y: int) -> dict:
    started = time.time()
    play_and_wait(win, x, y)
    video = newest_video(started - 5)
    if not video:
        return {"ok": False, "reason": "no video file cached"}
    print("[read] mp4:", video)
    frames = extract_frames(video, ROOT / "inbox" / f"read_{int(time.time())}" / "frames")
    texts = []
    for f in frames:
        try:
            lines = w.ocr(f)
        except Exception:  # noqa: BLE001
            continue
        joined = " ".join(ln["n"] for ln in lines if ln["n"])
        if joined and joined not in texts:
            texts.append(joined)
    wav = extract_audio(video)
    speech = transcribe_audio(wav) if wav else ""
    return {"ok": True, "mp4": str(video), "frames": len(frames),
            "subtitle": "\n".join(texts)[:1500], "speech": speech[:800]}


if __name__ == "__main__":
    x, y = int(sys.argv[1]), int(sys.argv[2])
    win = w.chat_window("小A")
    info = read_video(win, x, y)
    if not info.get("ok"):
        print("FAILED:", info)
        raise SystemExit(2)
    print("=== 画面文字 ===")
    print(info["subtitle"] or "(无)")
    print("=== 语音转写 ===")
    print(info["speech"] or "(无)")
