"""微信发送（阶段 E）：会做检查、会核验结果，失败不重发。

边界：
* 只对共享核心白名单里的人生效（读 D:\ai_chat_core 的配置，不在本文件另立名单）；
* 默认 dry-run：走到"引用已挂好 + 文字已进输入框"就停，然后关窗口丢弃，不发送；
* 加 --send 才真的点发送按钮；
* 任何一步校验不过就中止，并把状态标成 corresponding 结果，绝不"先发再说"。

为什么必须用真实鼠标：
* PostMessage 点击对会话列表无效（实测），双击会话行才会打开独立窗口；
* alpha=0 的隐藏窗口对鼠标命中透明，所以发送期间必须把窗口显示出来。

用法::

    python src/wechat_send.py --contact 测试号 --text "在吗" --dry-run
    python src/wechat_send.py --contact 测试号 --text "在吗" --send
    python src/wechat_send.py --contact 测试号 --text "这个视频不错" \
        --quote-text "致敬长征" --send
"""
from __future__ import annotations

import argparse
import ctypes
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
CORE = Path(r"D:\ai_chat_core")
POWERSHELL = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
PYTHON = r"C:\Users\yourname\AppData\Local\Programs\Python\Python314\python.exe"
LOGS = ROOT / "logs"

sys.path.insert(0, str(ROOT))
import src.wechat_ui as w  # noqa: E402
import src.wechat_bubbles as bubbles  # noqa: E402

from PIL import Image  # noqa: E402

user32 = w.user32
kernel32 = ctypes.windll.kernel32

VK_CONTROL = 0x11
VK_A = 0x41
VK_V = 0x56
VK_DELETE = 0x2E
WM_CLOSE = 0x0010
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

SYSTEM_PAT = re.compile(
    r"对方已取消|已拒绝|以上是打招呼|消息已发出|撤回|加入了群聊|邀请|开启了好友|"
    r"你已添加|现在可以开始聊天|拍了拍|语音通话|视频通话|通话时长|^\d{1,2}:\d{2}$|^昨天|^星期"
)


# ------------------------------------------------------------------ 基础

def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "wechat_send.log").open("a", encoding="utf-8") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def run_ps(script: str) -> None:
    subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / script)],
        capture_output=True,
        timeout=120,
    )


def set_clipboard(text: str) -> None:
    """用 ctypes 写剪贴板（中文安全，不经过命令行转义）。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    user32.EmptyClipboard()
    user32.SetClipboardData(CF_UNICODETEXT, handle)
    user32.CloseClipboard()


def real_click(win: dict, x: int, y: int, settle: float = 0.35) -> None:
    sx, sy = win["left"] + int(x), win["top"] + int(y)
    user32.SetCursorPos(sx, sy)
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(settle)


def real_dblclick(win: dict, x: int, y: int) -> None:
    sx, sy = win["left"] + int(x), win["top"] + int(y)
    user32.SetCursorPos(sx, sy)
    time.sleep(0.12)
    for _ in range(2):
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.12)


def real_key(vk: int, ctrl: bool = False, settle: float = 0.12) -> None:
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        time.sleep(0.03)
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, 2, 0)
    if ctrl:
        time.sleep(0.03)
        user32.keybd_event(VK_CONTROL, 0, 2, 0)
    time.sleep(settle)


# ------------------------------------------------------------------ 窗口

def window_state() -> str:
    p = ROOT / "window_state.txt"
    try:
        return p.read_text(encoding="utf-8").strip() or "hidden"
    except OSError:
        return "hidden"


def ensure_visible() -> str:
    """把微信窗口显示出来（真实点击必需）。返回原始状态，收尾时恢复。"""
    before = window_state()
    run_ps("show_wechat.ps1")
    time.sleep(1.2)
    return before


def restore_state(before: str) -> None:
    if before == "hidden":
        run_ps("hide_wechat.ps1")
        time.sleep(0.6)


def open_conversation(name: str) -> tuple[dict | None, Path | None]:
    """双击会话行打开独立聊天窗口；返回 (win, 截图)。"""
    win = w.chat_window(name)
    if win:
        return win, None
    main = w.main_window()
    if not main:
        return None, None
    png = w.capture(main["hwnd"])
    lines = w.ocr(png)
    key = norm(name)
    row = next(
        (
            ln
            for ln in lines
            if key in norm(ln["n"]) and ln["X"] < main["w"] * 0.3 and ln["Y"] > 80
        ),
        None,
    )
    if not row:
        return None, png
    for _ in range(2):
        real_dblclick(main, row["X"] + row["W"] / 2, row["Y"] + row["H"] / 2)
        time.sleep(1.4)
        opened = w.chat_window(name)
        if opened:
            return opened, None
    return main, png


def close_window(win: dict) -> None:
    user32.PostMessageW(win["hwnd"], WM_CLOSE, 0, 0)
    time.sleep(0.5)


def focus_window(win: dict) -> bool:
    """确保聊天窗口在前台；真实键盘事件只发给前台窗口。"""
    user32.SetForegroundWindow(win["hwnd"])
    time.sleep(0.35)
    if user32.GetForegroundWindow() == win["hwnd"]:
        return True
    # 前台锁没拿到就点一下标题栏
    real_click(win, win["w"] // 2, 28, settle=0.4)
    user32.SetForegroundWindow(win["hwnd"])
    time.sleep(0.3)
    return user32.GetForegroundWindow() == win["hwnd"]


def stage_text(win: dict, text: str, stamp: str) -> tuple[bool, str]:
    """把文字放进输入框。

    输入框在窗口底部，但"底部 - 105"有时会点到工具栏（实测失败过一次），
    所以按多个候选高度重试，直到 OCR 能在输入区读到我们的文字。
    """
    candidates = [win["h"] - 190, win["h"] - 150, int(win["h"] * 0.85), win["h"] - 105]
    staged = ""
    for y in candidates:
        real_click(win, win["w"] * 0.45, y, settle=0.3)
        real_key(VK_A, ctrl=True)
        real_key(VK_DELETE)
        set_clipboard(text)
        real_key(VK_V, ctrl=True, settle=0.6)
        png = w.capture(win["hwnd"])
        (LOGS / f"send_staged_{stamp}.png").write_bytes(png.read_bytes())
        staged = " ".join(quote_bar(w.ocr(png), win))
        if norm(text)[:4] in norm(staged):
            return True, staged
    return False, staged


# ------------------------------------------------------------------ 元素定位

def bottom_band(lines: list[dict], win: dict) -> list[dict]:
    """输入区（比聊天区更靠下），避免把刚发出的气泡当成输入框残留。"""
    return [ln for ln in lines if ln["Y"] > win["h"] * 0.77 and ln["n"].strip()]


PLACEHOLDERS = {
    "长按鼠标语音输入", "长按鼠标", "语音输入", "按住说话", "发送", "O", "o",
}


def _is_placeholder(text: str) -> bool:
    """输入框占位提示（OCR 常把"语"读成"浯"之类，所以用包含判断）。"""
    t = norm(text)
    if not t:
        return True
    if t in PLACEHOLDERS:
        return True
    return "长按鼠标" in t or "音输入" in t or "按住说话" in t


def _band_text(lines: list[dict], win: dict) -> list[str]:
    return [
        norm(t)
        for t in quote_bar(lines, win)
        if norm(t) and not _is_placeholder(t)
    ]


def input_is_empty(lines: list[dict], win: dict) -> bool:
    return not _band_text(lines, win)


@dataclass
class Bubble:
    text: str
    own: bool
    cx: int
    cy: int
    y: int
    raw: dict = field(default_factory=dict)


def find_bubbles(win: dict, png: Path) -> list[Bubble]:
    lines = w.ocr(png)
    img = Image.open(png).convert("RGB")
    out: list[Bubble] = []
    standalone = win["title"] != "微信"
    left = 55 if standalone else win["w"] * 0.22
    for ln in lines:
        text = (ln["n"] or "").strip()
        if not text or SYSTEM_PAT.search(text):
            continue
        if not (left < ln["X"] < win["w"] * 0.99):
            continue
        if not (win["h"] * 0.07 < ln["Y"] < win["h"] * 0.755):
            continue
        info = bubbles.classify_line(img, ln)
        out.append(
            Bubble(
                text=text,
                own=info["own"],
                cx=int(ln["X"] + ln["W"] / 2),
                cy=int(ln["Y"] + ln["H"] / 2),
                y=int(ln["Y"]),
                raw=ln,
            )
        )
    return out


def find_quote_target(bubbles_list: list[Bubble], needle: str) -> Bubble | None:
    """按文本片段找"对方发的"目标气泡。

    needle 可能是多行拼接的卡片文案，单行 OCR 匹配不到，
    所以取前 16 个字符作为探针，并保留窗口内的顺序（从新到旧）。
    """
    key = norm(needle)
    if not key:
        return None
    probe = key if len(key) <= 16 else key[:16]
    for b in reversed(bubbles_list):
        if b.own:
            continue
        t = norm(b.text)
        if key in t or (probe and probe in t):
            return b
        # 多行卡片：任意一行能匹配也算
        for part in (needle or "").split():
            p = norm(part)
            if len(p) >= 6 and p in t:
                return b
    return None


def menu_items(win: dict) -> tuple[dict | None, list[dict]]:
    menu = w._popup_menu(win["pid"])
    if not menu:
        return None, []
    png = w.capture(menu["hwnd"])
    lines = w.ocr(png)
    return menu, [ln for ln in lines if ln["n"].strip()]


def click_menu_item(menu: dict, item: dict) -> None:
    lp = ((item["Y"] + item["H"] // 2) << 16) | ((item["X"] + item["W"] // 2) & 0xFFFF)
    user32.PostMessageW(menu["hwnd"], 0x0200, 0, lp)
    user32.PostMessageW(menu["hwnd"], 0x0201, 1, lp)
    time.sleep(0.05)
    user32.PostMessageW(menu["hwnd"], 0x0202, 0, lp)
    time.sleep(0.9)


def focus_list(win: dict) -> None:
    """把焦点交给消息列表（否则 PageUp/PageDown 不生效）。

    点横向中心：1:1 聊天里气泡都在两侧，中间通常是空白；点中文字气泡也只是选中，
    不会产生副作用。
    """
    real_click(win, win["w"] // 2, int(win["h"] * 0.45), settle=0.3)


def page_scroll(win: dict, up: bool = True, steps: int = 1) -> None:
    key = 0x21 if up else 0x22  # PageUp / PageDown
    for _ in range(steps):
        real_key(key)
        time.sleep(0.4)


def locate_quote_target(win: dict, needle: str, max_rounds: int = 8) -> tuple[Bubble | None, Path]:
    """在当前视图找目标；找不到就 PageUp 往上翻，返回 (目标, 最后一次截图)。"""
    png = w.capture(win["hwnd"])
    target = find_quote_target(find_bubbles(win, png), needle)
    if target:
        return target, png
    focus_list(win)
    for _ in range(max_rounds):
        page_scroll(win, up=True)
        png = w.capture(win["hwnd"])
        target = find_quote_target(find_bubbles(win, png), needle)
        if target:
            return target, png
    return None, png


def quote_bar(lines: list[dict], win: dict) -> list[str]:
    return [ln["n"] for ln in bottom_band(lines, win)]


# ------------------------------------------------------------------ 主流程

@dataclass
class SendOutcome:
    status: str  # verified / unknown / failed / aborted / dry_run
    stage: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def load_allowlist() -> dict[str, list[str]]:
    p = CORE / "config" / "settings.local.json"
    base = CORE / "config" / "settings.json"
    merged: dict[str, list[str]] = {"wechat": []}
    for f in (base, p):
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            continue
        table = data.get("reply", {}).get("allowlisted_contacts", {})
        for k, v in table.items():
            merged.setdefault(k, [])
            merged[k] = list(v)
    return merged


def allowed(contact: str) -> bool:
    sys.path.insert(0, str(CORE))
    from core.naming import matches_alias

    return any(matches_alias(str(e), contact) for e in load_allowlist().get("wechat", []))


def _cjk_probe(text: str, n: int = 6) -> str:
    """从文本里取一段稳定的中文探针，避开 OCR 对 # 、标点、英文的误读。"""
    runs = re.findall(r"[\u4e00-\u9fa5]+", text or "")
    if runs:
        return max(runs, key=len)[:n]
    return norm(text)[:4]


def send(
    contact: str,
    text: str,
    do_send: bool,
    quote_text: str = "",
    keep_visible: bool = False,
    restore: str | None = None,
    quote_expect: str = "",
) -> SendOutcome:
    if not allowed(contact):
        return SendOutcome("aborted", "allowlist", f"{contact} 不在白名单")
    if not text.strip():
        return SendOutcome("aborted", "text", "回复文本为空")

    before = restore if restore is not None else ensure_visible()
    win: dict | None = None
    opened_here = False
    try:
        win, pre_png = open_conversation(contact)
        if not win:
            return SendOutcome("aborted", "open_chat", f"找不到会话 {contact}")
        if win["title"] == "微信":
            return SendOutcome("aborted", "open_chat", "未能打开独立聊天窗口")
        opened_here = True
        focus_window(win)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        png = w.capture(win["hwnd"])
        (LOGS / f"send_pre_{stamp}.png").write_bytes(png.read_bytes())

        if quote_text:
            target, png = locate_quote_target(win, quote_text)
            if not target:
                return SendOutcome("aborted", "quote_target", "向上翻了 8 屏仍没找到可引用的目标消息")
            if not w.right_click_message(win, target.cx, target.cy):
                return SendOutcome("aborted", "menu", "右键没有弹出菜单")
            menu, items = menu_items(win)
            if not menu:
                return SendOutcome("aborted", "menu", "菜单窗口消失")
            item = next((i for i in items if "引用" in i["n"]), None)
            if not item:
                return SendOutcome("aborted", "menu_item", f"菜单里没有引用: {[i['n'] for i in items]}")
            click_menu_item(menu, item)
            time.sleep(0.4)
            png2 = w.capture(win["hwnd"])
            (LOGS / f"send_quote_{stamp}.png").write_bytes(png2.read_bytes())
            bar = " ".join(quote_bar(w.ocr(png2), win))
            expect = norm(contact)
            if expect not in norm(bar):
                return SendOutcome("aborted", "quote_verify", f"引用条里没有联系人名: {bar[:80]}")
            # 校验探针优先用"文案"（干净），没有才退回缩略图文字
            probe_text = quote_expect or quote_text
            probe = _cjk_probe(probe_text, 6)
            if probe and probe not in norm(bar):
                return SendOutcome("aborted", "quote_verify", f"引用条内容与目标不符: {bar[:80]}")
        else:
            bar = ""

        # 把文字放进输入框（多点重试，避免点到工具栏）
        ok, staged = stage_text(win, text, stamp)
        if not ok:
            return SendOutcome("aborted", "staged_verify", f"文字没进输入框: {staged[:80]}")
        png3 = LOGS / f"send_staged_{stamp}.png"

        if not do_send:
            close_window(win)
            win = None
            return SendOutcome(
                "dry_run", "staged",
                "已引用并填入，未发送（窗口已关闭丢弃）",
                {"quote_bar": bool(bar), "staged": True},
            )

        # 发送：优先点发送按钮（必须是最右侧、文本恰为"发送"的控件），找不到再回车
        lines_now = w.ocr(png3)
        send_btn = next(
            (
                ln
                for ln in bottom_band(lines_now, win)
                if norm(ln["n"]) == "发送" and ln["X"] > win["w"] * 0.8
            ),
            None,
        )
        if send_btn:
            real_click(
                win,
                send_btn["X"] + send_btn["W"] / 2,
                send_btn["Y"] + send_btn["H"] / 2,
                settle=0.2,
            )
        else:
            real_key(0x0D)  # Enter
        time.sleep(1.2)

        # 回到最新消息处再核验（引用时我们可能翻到了上面）
        focus_list(win)
        page_scroll(win, up=False, steps=3)
        time.sleep(0.6)
        after_png = w.capture(win["hwnd"])
        (LOGS / f"send_after_{stamp}.png").write_bytes(after_png.read_bytes())
        after_lines = w.ocr(after_png)
        cleared = input_is_empty(after_lines, win)

        probe = norm(text)[:3] if len(norm(text)) < 6 else norm(text)[:4]

        def _seen(png_path: Path) -> bool:
            if not probe:
                return False
            return any(b.own and probe in norm(b.text) for b in find_bubbles(win, png_path))

        appeared = _seen(after_png)
        if not appeared:
            # OCR 有延迟，再等一拍重拍一次
            time.sleep(1.2)
            after_png = w.capture(win["hwnd"])
            after_lines = w.ocr(after_png)
            appeared = _seen(after_png)

        if appeared and cleared:
            status, detail = "verified", "气泡出现且输入框已清空"
        elif cleared:
            status, detail = "unknown", "输入框已清空，但没看到气泡；禁止直接重发"
        else:
            status, detail = "failed", "输入框仍有内容，发送未完成"

        return SendOutcome(
            status, "sent", detail,
            {
                "input_cleared": cleared,
                "bubble_seen": appeared,
                "used_send_button": bool(send_btn),
                "quote_used": bool(quote_text),
            },
        )
    finally:
        if win is not None and opened_here:
            close_window(win)
        if not keep_visible:
            restore_state(before)


def main() -> int:
    ap = argparse.ArgumentParser(description="微信发送（带引用校验与发送后核验）")
    ap.add_argument("--contact", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--quote-text", default="", help="要引用的目标消息里的片段（只在对方消息里找）")
    ap.add_argument("--send", action="store_true", help="真的发送；不加则 dry-run")
    ap.add_argument("--keep-visible", action="store_true", help="结束后不把窗口重新隐藏")
    ap.add_argument("--restore-state", default=None, help="显式指定收尾时的窗口状态 hidden/shown")
    args = ap.parse_args()

    outcome = send(
        args.contact,
        args.text,
        do_send=args.send,
        quote_text=args.quote_text,
        keep_visible=args.keep_visible,
        restore=args.restore_state,
    )
    log(json.dumps({"contact": args.contact, **outcome.to_dict()}, ensure_ascii=False))
    print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    return 0 if outcome.status in ("verified", "dry_run") else 3


if __name__ == "__main__":
    raise SystemExit(main())
