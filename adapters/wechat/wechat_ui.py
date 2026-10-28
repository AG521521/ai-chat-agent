"""微信窗口自动化（PostMessage 版）。

为什么用 PostMessage 而不是鼠标/键盘注入：
* 注入需要窗口在前台，会被浏览器挡住、被前台锁拒绝；
* PostMessage 直接进窗口消息队列，窗口隐藏（alpha=0）也能用，屏幕上完全无感。

已验证的通道：
* WM_LBUTTONDOWN/UP（客户区坐标）→ 聚焦输入框
* WM_CHAR（UTF-16 码元）→ 输入中文
* WM_KEYDOWN/UP VK_RETURN → 发送
"""
from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from ctypes import wintypes
from pathlib import Path

ROOT = Path(r"D:\weixin_agent")
POWERSHELL = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
OCR_PS1 = ROOT / "ocr.ps1"
CAP_PS1 = ROOT / "src" / "capture_window.ps1"

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_RETURN = 0x0D
VK_BACK = 0x08


def _proc_name(pid: int) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30)
        line = out.stdout.strip().splitlines()
        return line[0].split(",")[0].strip('"').lower() if line else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def windows() -> list[dict]:
    """所有微信的 Qt 窗口：句柄、可见性、矩形、标题。"""
    found: list[dict] = []

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "Qt51514QWindowIcon":
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _proc_name(pid.value) != "weixin.exe":
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        found.append({"hwnd": hwnd, "title": title.value, "pid": pid.value,
                      "left": r.left, "top": r.top, "w": w, "h": h,
                      "visible": bool(user32.IsWindowVisible(hwnd))})
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def main_window() -> dict | None:
    """列表主窗口（标题就是“微信”）。"""
    cands = [w for w in windows() if w["title"] == "微信"]
    return max(cands, key=lambda d: d["w"] * d["h"]) if cands else None


def chat_window(name: str) -> dict | None:
    """独立聊天窗口，标题就是联系人名（可能带后缀，用包含匹配）。"""
    key = re.sub(r"\s+", "", name)
    for w in windows():
        if re.sub(r"\s+", "", w["title"]).startswith(key) and w["title"] != "微信":
            return w
    return None


def capture(hwnd: int) -> Path:
    out = Path(tempfile.gettempdir()) / "wx_cap"
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(CAP_PS1), "-Hwnd", str(hwnd), "-OutDir", str(out)],
                   capture_output=True, text=True, timeout=120)
    png = out / "cap.png"
    if not png.exists():
        raise RuntimeError("capture failed")
    return png


def ocr(png: Path) -> list[dict]:
    out = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(OCR_PS1), "-ImagePath", str(png), "-Json"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=180)
    raw = (out.stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        raise RuntimeError(f"ocr failed: {raw[:200]} {(out.stderr or '')[:200]}")
    lines = json.loads(raw[start:])["lines"]
    for ln in lines:
        ln["n"] = re.sub(r"\s+", "", ln["Text"])
    return lines


def _client_point(win: dict, x: int, y: int) -> int:
    pt = wintypes.POINT(win["left"] + int(x), win["top"] + int(y))
    user32.ScreenToClient(win["hwnd"], ctypes.byref(pt))
    return (pt.y << 16) | (pt.x & 0xFFFF)


def post_click(win: dict, x: int, y: int, hwnd: int | None = None) -> None:
    target = hwnd or win["hwnd"]
    lp = _client_point(win, x, y)
    user32.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(target, WM_LBUTTONDOWN, 1, lp)
    time.sleep(0.05)
    user32.PostMessageW(target, WM_LBUTTONUP, 0, lp)
    time.sleep(0.35)


def post_text(hwnd: int, text: str) -> None:
    for ch in text:
        # 逐 UTF-16 码元发送（emoji 是代理对）
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            code = int.from_bytes(data[i:i + 2], "little")
            user32.PostMessageW(hwnd, WM_CHAR, code, 0)
            time.sleep(0.02)
        time.sleep(0.02)


def post_backspace(hwnd: int, n: int) -> None:
    for _ in range(n):
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_BACK, 0)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_BACK, 0)
        time.sleep(0.03)


def post_enter(hwnd: int) -> None:
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0x001C0001)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0xC01C0001)


def open_chat(name: str, verbose: bool = True) -> dict | None:
    """已经开着就直接用；没开就在主窗口点会话行，等新窗口出现。"""
    win = chat_window(name)
    if win:
        return win
    main = main_window()
    if not main:
        return None
    png = capture(main["hwnd"])
    lines = ocr(png)
    key = re.sub(r"\s+", "", name)
    row = None
    for ln in lines:
        if key in ln["n"] and ln["X"] < main["w"] * 0.45 and ln["Y"] > 80:
            row = ln
            break
    if not row:
        if verbose:
            print(f"[open_chat] '{name}' not visible in list")
        return None
    cx, cy = row["X"] + row["W"] / 2, row["Y"] + row["H"] / 2
    if verbose:
        print(f"[open_chat] click row '{row['n']}' at {cx:.0f},{cy:.0f}")
    post_click(main, cx, cy)
    for _ in range(10):
        time.sleep(0.5)
        win = chat_window(name)
        if win:
            return win
    if verbose:
        print("[open_chat] chat window did not appear")
    return None


def input_text(win: dict) -> str:
    """输入框里现在有什么（OCR 底部一条带）。空输入框只会读到占位提示。"""
    png = capture(win["hwnd"])
    lines = ocr(png)
    band = [ln for ln in lines if ln["Y"] > win["h"] * 0.88]
    texts = [ln["n"] for ln in band]
    if not texts:
        return ""
    if all(t in ("长按鼠标语音输入", "长按鼠标", "语音输入", "发送") for t in texts):
        return ""
    return "".join(t for t in texts if t != "发送")


def input_empty(win: dict) -> bool:
    return input_text(win) == ""


def clear_input(win: dict, backspaces: int = 240) -> None:
    """盲删：输入框为空时退格是无害的，比反复 OCR 快得多。"""
    post_click(win, win["w"] * 0.5, win["h"] - 110)
    time.sleep(0.3)
    post_backspace(win["hwnd"], backspaces)
    time.sleep(0.3)


def _popup_menu(pid: int) -> dict | None:
    """微信右键菜单是独立的 Qt 弹窗。"""
    best = None

    def cb(hwnd, _):
        nonlocal best
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if "QWindowToolSaveBits" not in cls.value:
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        best = {"hwnd": hwnd, "left": r.left, "top": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top}
        return False

    user32.EnumWindows(EnumProc(cb), 0)
    return best


def right_click_message(win: dict, x: int, y: int) -> bool:
    lp = _client_point(win, x, y)
    user32.PostMessageW(win["hwnd"], WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(win["hwnd"], 0x0204, 2, lp)   # WM_RBUTTONDOWN
    time.sleep(0.06)
    user32.PostMessageW(win["hwnd"], 0x0205, 0, lp)   # WM_RBUTTONUP
    for _ in range(10):
        time.sleep(0.3)
        if _popup_menu(win["pid"]):
            return True
    return False


def click_quote(win: dict, verbose: bool = True) -> bool:
    """右键后点菜单里的“引用”。菜单是弹窗，用 OCR 找它的位置。"""
    menu = _popup_menu(win["pid"])
    if not menu:
        return False
    png = capture(menu["hwnd"])
    lines = ocr(png)
    item = next((ln for ln in lines if "引用" in ln["n"]), None)
    if not item:
        if verbose:
            print("[quote] 引用 item not found by OCR:", [l["n"] for l in lines])
        return False
    cx = int(item["X"] + item["W"] / 2)
    cy = int(item["Y"] + item["H"] / 2)
    lp = (cy << 16) | (cx & 0xFFFF)
    user32.PostMessageW(menu["hwnd"], WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(menu["hwnd"], WM_LBUTTONDOWN, 1, lp)
    time.sleep(0.05)
    user32.PostMessageW(menu["hwnd"], WM_LBUTTONUP, 0, lp)
    time.sleep(0.8)
    if verbose:
        print(f"[quote] clicked 引用 at {cx},{cy}")
    return True


def quote_reply(win: dict, video_x: int, video_y: int, text: str,
                dry_run: bool = False, verbose: bool = True) -> dict:
    """右键视频 → 引用 → 打字 → 发送。"""
    if not right_click_message(win, video_x, video_y):
        return {"ok": False, "stage": "menu", "detail": "no context menu"}
    if not click_quote(win, verbose=verbose):
        return {"ok": False, "stage": "menu", "detail": "引用 not found"}
    win_now = chat_window("小A") or win
    post_click(win_now, win_now["w"] * 0.55, win_now["h"] - 80)
    time.sleep(0.25)
    post_text(win_now["hwnd"], text)
    time.sleep(0.4)
    if dry_run:
        return {"ok": True, "stage": "staged", "detail": ""}
    post_enter(win_now["hwnd"])
    time.sleep(1.2)
    png = capture(win_now["hwnd"])
    shot = ROOT / "logs" / f"quote_{time.strftime('%Y%m%d_%H%M%S')}.png"
    shot.write_bytes(png.read_bytes())
    remaining = input_text(win_now)
    ok = text[:6] not in remaining
    return {"ok": ok, "stage": "sent" if ok else "still_staged", "detail": str(shot)}


def send(chat: str, text: str, dry_run: bool = False, verbose: bool = True) -> dict:
    """返回 {'ok': bool, 'stage': str, 'detail': str}。"""
    win = open_chat(chat, verbose=verbose)
    if not win:
        return {"ok": False, "stage": "open_chat", "detail": "chat window not found"}
    if verbose:
        print(f"[send] window '{win['title']}' {win['w']}x{win['h']} visible={win['visible']}")
    title_ok = re.sub(r"\s+", "", win["title"]).startswith(re.sub(r"\s+", "", chat))
    if not title_ok:
        return {"ok": False, "stage": "verify", "detail": f"title mismatch: {win['title']}"}

    clear_input(win)
    # 聚焦输入框：底部区域中间偏左
    post_click(win, win["w"] * 0.5, win["h"] - 110)
    time.sleep(0.3)
    post_text(win["hwnd"], text)
    time.sleep(0.5)

    if dry_run:
        png = capture(win["hwnd"])
        shot = ROOT / "logs" / f"staged_{time.strftime('%Y%m%d_%H%M%S')}.png"
        shot.write_bytes(png.read_bytes())
        return {"ok": True, "stage": "staged", "detail": str(shot)}

    post_enter(win["hwnd"])
    time.sleep(1.2)
    png = capture(win["hwnd"])
    shot = ROOT / "logs" / f"sent_{time.strftime('%Y%m%d_%H%M%S')}.png"
    shot.write_bytes(png.read_bytes())
    remaining = input_text(win)
    ok = text[:6] not in remaining
    return {"ok": ok, "stage": "sent" if ok else "still_staged", "detail": str(shot)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["windows", "open", "send", "probe"])
    ap.add_argument("--chat", default="文件传输助手")
    ap.add_argument("--text", default="")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    if a.cmd == "windows":
        print(json.dumps(windows(), ensure_ascii=False, indent=2))
    elif a.cmd == "open":
        print(json.dumps(open_chat(a.chat), ensure_ascii=False))
    elif a.cmd == "send":
        print(json.dumps(send(a.chat, a.text, dry_run=not a.send), ensure_ascii=False))
    else:
        m = main_window()
        print("main:", m)
        print("chat:", chat_window(a.chat))
