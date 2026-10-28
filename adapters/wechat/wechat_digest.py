#!/usr/bin/env python
"""WeChat 4.x session-list digest for the always-on B account.

Modes:
  --pid N             capture + parse one window
  --image PATH        parse an existing screenshot
  --auto              capture the account whose avatar matches ref_avatar.png
  --learn --pid N     store that window's avatar as the reference account
  --discover          print logged-in WeChat windows (pid -> size)

Artifacts under D:\\wexin_duokai\\daily: wechat_<stamp>.png, digest_<stamp>.json,
and with --md: a YYYY-MM-DD_HHMM.md summary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw

ROOT = r"D:\weixin_agent"
DAILY = os.path.join(ROOT, "daily")
REF_AVATAR = os.path.join(ROOT, "ref_avatar.png")
POWERSHELL = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
OCR_PS1 = os.path.join(ROOT, "ocr.ps1")
PROBE_PS1 = os.path.join(ROOT, "wechat_probe.ps1")

WIN_W, WIN_H = 2100, 1330            # canonical window size (200% DPI desktop)
PANEL_X0, PANEL_X1 = 133, 613        # session-list panel inside that window
PANEL_Y0 = 40
AVATAR_RAIL_BOX = (28, 55, 112, 132)  # account avatar in the left rail
AVATAR_X0, AVATAR_X1 = 18, 100        # unread badge band inside the panel
ROW_GAP = 95                          # 1x pixels between session rows
AVATAR_MAX_DIST = 0.06                # max signature distance still accepted as the B account

TIME_RE = re.compile(r"^(\d{1,2}[:：]\d{2}|星期[一二三四五六日天]|[昨前]天.{0,6}|\d{1,2}/\d{1,2})$")


def ps(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    # errors="replace": never let a mis-encoded byte turn stdout into None
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"] + args,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def run_ocr(image_path: str) -> list[dict]:
    out = ps(["-File", OCR_PS1, "-ImagePath", image_path, "-Json"])
    raw = (out.stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        raise RuntimeError(f"OCR produced no json. stdout={raw[:300]} stderr={(out.stderr or '')[:300]}")
    return json.loads(raw[start:])["lines"]


def capture_window(pid: int, out_png: str, allow_restore: bool = False,
                   flash_restore: bool = True) -> str:
    out_dir = os.path.dirname(out_png)
    extra = ["-EnsureHidden"]
    if allow_restore:
        extra.append("-AllowRestore")
        if flash_restore:
            extra.append("-FlashRestore")
    ps(["-File", PROBE_PS1, "-ProcId", str(pid), "-OutDir", out_dir,
        "-Width", str(WIN_W), "-Height", str(WIN_H)] + extra)
    produced = os.path.join(out_dir, f"shot_pid{pid}.png")
    if not os.path.exists(produced):
        raise RuntimeError(f"capture failed, nothing at {produced}")
    if os.path.abspath(produced) != os.path.abspath(out_png):
        os.replace(produced, out_png)
    return out_png


def discover() -> dict[int, str]:
    script = (
        "Add-Type @\"\n"
        "using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;\n"
        "public class DW{public delegate bool EP(IntPtr h,IntPtr l);"
        "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();"
        "[DllImport(\"user32.dll\")]public static extern bool EnumWindows(EP cb,IntPtr p);"
        "[DllImport(\"user32.dll\")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);"
        "[DllImport(\"user32.dll\",CharSet=CharSet.Unicode)]public static extern int GetClassNameW(IntPtr h,StringBuilder s,int n);"
        "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out R c);"
        "[DllImport(\"user32.dll\")]public static extern bool IsIconic(IntPtr h);"
        "public struct R{public int L,T,RR,B;}"
        "public static List<string> All(){var r=new List<string>();EnumWindows((h,l)=>{uint pid;GetWindowThreadProcessId(h,out pid);"
        "var c=new StringBuilder(512);GetClassNameW(h,c,512);if(c.ToString()!=\"Qt51514QWindowIcon\")return true;"
        "R rr;GetWindowRect(h,out rr);r.Add(pid+\"|\"+(rr.RR-rr.L)+\"x\"+(rr.B-rr.T)+\"|\"+IsIconic(h));return true;},IntPtr.Zero);return r;}}\n"
        "\"@;[void][DW]::SetProcessDPIAware();[DW]::All()"
    )
    out = ps(["-Command", script], timeout=90)
    found = {}
    for line in out.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        p, size, iconic = parts
        try:
            w, h = (int(v) for v in size.split("x"))
        except ValueError:
            continue
        if not p.strip().isdigit():
            continue
        is_min = iconic.strip().lower() == "true"
        # a minimized chat window keeps its identity but shrinks; login windows are ~586x773
        if is_min or (w > 900 and h > 700):
            found[int(p)] = size + (" (min)" if is_min else "")
    return found


def _sig(crop: Image.Image) -> np.ndarray:
    return np.asarray(crop.convert("L").resize((32, 32), Image.LANCZOS), dtype=float) / 255.0


def _runs(values, thresh: float, min_len: int) -> list[tuple[int, int]]:
    out, start = [], None
    for i, val in enumerate(values):
        if val >= thresh:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    if start is not None and len(values) - start >= min_len:
        out.append((start, len(values)))
    return out


def find_avatar(img: Image.Image) -> Image.Image | None:
    """Locate the account avatar in the left rail by colourfulness, so window shifts don't break it."""
    region = img.crop((0, 0, 260, 260)).convert("RGB")
    arr = np.asarray(region, dtype=float)
    mx, mn = arr.max(axis=2), arr.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    mask = (sat > 0.18) & (mx > 45)
    row_runs = _runs(mask.sum(axis=1), 12, 36)
    if not row_runs:
        return None
    y0, y1 = row_runs[0]
    col_runs = _runs(mask[y0:y1].sum(axis=0), 8, 36)
    if not col_runs:
        return None
    x0, x1 = col_runs[0]
    return region.crop((x0, y0, x1, y1))


def avatar_sig(img: Image.Image) -> np.ndarray:
    """Signature from a full window screenshot (auto-locates the rail avatar)."""
    crop = find_avatar(img)
    if crop is None:
        crop = img.crop(AVATAR_RAIL_BOX)
    return _sig(crop)


def ref_avatar_sig(path: str) -> np.ndarray:
    """Signature from an already-cropped avatar image (what --learn stores)."""
    return _sig(Image.open(path).convert("RGB"))


def clean(text: str) -> str:
    return re.sub(r"\s+", "", text).strip(" ·、。")


def cluster_rows(lines: list[dict]) -> list[list[dict]]:
    lines = sorted(lines, key=lambda d: d["Y"])
    rows: list[list[dict]] = []
    for ln in lines:
        cy = ln["Y"] + ln["H"] / 2
        if rows:
            last_cy = float(np.mean([l["Y"] + l["H"] / 2 for l in rows[-1]]))
            if cy - last_cy < ROW_GAP:
                rows[-1].append(ln)
                continue
        rows.append([ln])
    return rows


def parse_panel(image_path: str, debug_dir: str | None = None) -> dict:
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    panel = img.crop((PANEL_X0, PANEL_Y0, min(PANEL_X1, W), H))
    pw, ph = panel.size

    text_x0 = 100
    scale = 2
    text_img = panel.crop((text_x0, 0, pw, ph))
    big = text_img.resize((text_img.width * scale, text_img.height * scale), Image.LANCZOS)
    tmp = os.path.join(tempfile.gettempdir(), "wc_panel.png")
    big.save(tmp)
    raw = run_ocr(tmp)
    lines = [{"X": l["X"] / scale + text_x0, "Y": l["Y"] / scale, "W": l["W"] / scale,
              "H": l["H"] / scale, "Text": l["Text"]} for l in raw]
    time_x = pw - 130

    rows = []
    for cluster in cluster_rows(lines):
        left = [l for l in cluster if l["X"] < time_x and l["W"] > 16 and l["H"] >= 13]
        if not left:
            continue
        left.sort(key=lambda l: l["Y"])
        if left[0]["Y"] < 45:
            continue
        placeholder = ""
        if left[0]["Text"].strip().startswith("["):
            name_line = None
            placeholder = clean(left[0]["Text"])
        else:
            name_line = left[0]
        row_y = int(left[0]["Y"])
        row = {"name": clean(name_line["Text"]) if name_line else "",
               "name_y": row_y, "time": "", "preview": "", "unread": False,
               "unread_count": None, "raw": [l["Text"] for l in cluster]}
        for ln in cluster:
            t = clean(ln["Text"])
            if ln["X"] >= time_x and not row["time"]:
                row["time"] = t
            elif ln is not name_line and ln["Y"] > row_y + 10 and not row["preview"]:
                row["preview"] = t
        if not row["name"]:
            if not row["preview"] and not row["time"]:
                continue
            row["name"] = "(未识别)"
            if not row["preview"] and placeholder:
                row["preview"] = placeholder
        rows.append(row)

    arr = np.asarray(panel).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # official-account style avatars are the brand green square
    green = (r < 150) & (g > 125) & (b < 150) & ((g - r) > 35) & ((g - b) > 35)
    # cluster green squares in the avatar column, same way red badges are clustered
    gband = green[:, AVATAR_X0:AVATAR_X1]
    gys = np.where(gband.sum(axis=1) >= 42)[0]
    gclusters: list[list[int]] = []
    for y in gys:
        if gclusters and y - gclusters[-1][-1] <= 10:
            gclusters[-1].append(int(y))
        else:
            gclusters.append([int(y)])
    green_centers = [sum(c) / len(c) for c in gclusters if len(c) >= 40]
    for row in rows:
        row["push_avatar"] = any(-32 <= (gc - row["name_y"]) <= 46 for gc in green_centers)
    red = (r > 185) & (g < 110) & (b < 110)
    band = red[:, AVATAR_X0:AVATAR_X1]
    ys = np.where(band.sum(axis=1) >= 3)[0]
    clusters: list[list[int]] = []
    for y in ys:
        if clusters and y - clusters[-1][-1] <= 8:
            clusters[-1].append(int(y))
        else:
            clusters.append([int(y)])
    centers = [sum(c) / len(c) for c in clusters if len(c) >= 3]
    for row in rows:
        for c in centers:
            if -30 <= (c - row["name_y"]) <= 60:
                row["unread"] = True

    result = {"captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "image": os.path.abspath(image_path), "window_size": [W, H],
              "rows": rows, "badge_y": [round(c, 1) for c in centers],
              "raw_lines": [{"x": l["X"], "y": l["Y"], "h": l["H"], "text": l["Text"]} for l in lines]}

    if debug_dir:
        dbg = panel.copy()
        d = ImageDraw.Draw(dbg)
        for c in centers:
            d.rectangle([AVATAR_X0, c - 2, AVATAR_X1, c + 2], fill=(0, 160, 255))
        for row in rows:
            d.line([0, row["name_y"] + 18, pw, row["name_y"] + 18], fill=(255, 0, 0), width=1)
        dbg.save(os.path.join(debug_dir, "debug_panel.png"))
    return result


def pick_target(allow_restore: bool = False) -> tuple[int | None, str | None, float | None]:
    cands = discover()
    if not cands:
        return None, None, None
    ref_sig = None
    if os.path.exists(REF_AVATAR):
        ref_sig = ref_avatar_sig(REF_AVATAR)
    best = None
    tmp_dir = os.path.join(DAILY, "candidates")
    os.makedirs(tmp_dir, exist_ok=True)
    for pid in cands:
        tmp_png = os.path.join(tmp_dir, f"cand_{pid}.png")
        try:
            capture_window(pid, tmp_png, allow_restore=allow_restore)
        except Exception:
            continue
        sig = avatar_sig(Image.open(tmp_png).convert("RGB"))
        dist = float(np.mean(np.abs(sig - ref_sig))) if ref_sig is not None else 0.0
        if best is None or dist < best[2]:
            best = (pid, tmp_png, dist)
    if best and ref_sig is not None and best[2] > AVATAR_MAX_DIST:
        # nothing on screen matches the stored B account -> do not summarise the wrong account
        return None, None, best[2]
    return best if best else (None, None, None)


def _minutes_ago(raw: str):
    """Turn a WeChat list timestamp into minutes-ago, or None when unparseable."""
    s = (raw or "").replace("：", ":").replace(" ", "").strip()
    if not s:
        return None
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    m = re.match(r"^昨天(\d{1,2}):(\d{2})$", s)
    if m:
        return float((now_min + 24 * 60) - (int(m.group(1)) * 60 + int(m.group(2))))
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        delta = now_min - (int(m.group(1)) * 60 + int(m.group(2)))
        if delta < -5:
            delta += 24 * 60
        return float(max(0, delta))
    return None


def load_rules() -> dict:
    rules_path = os.path.join(ROOT, "digest_rules.json")
    if not os.path.exists(rules_path):
        return {"always_push": [], "always_reply": []}
    try:
        with open(rules_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return {"always_push": data.get("always_push", []),
                "always_reply": data.get("always_reply", [])}
    except (json.JSONDecodeError, OSError):
        return {"always_push": [], "always_reply": []}


def rank_rows(rows: list[dict], cfg: dict) -> list[dict]:
    """Score sessions so the ones that actually need an answer float to the top."""
    top = []
    promote = cfg.get("promote_keywords", [])
    promote_names = cfg.get("promote_names", [])
    demote_names = cfg.get("demote_names", [])
    demote_preview = cfg.get("demote_preview", [])
    rules = load_rules()
    for row in rows:
        name = row.get("name") or ""
        prev = row.get("preview") or ""
        score = 0.0
        why = []
        if row.get("unread"):
            score += 3
            why.append("未读")
        mins = _minutes_ago(row.get("time", ""))
        if mins is not None:
            if mins <= 180:
                score += 2
                why.append("近3小时")
            elif mins <= 12 * 60:
                score += 1
                why.append("今天")
        hits = [k for k in promote if k in prev or k in name]
        if hits:
            score += 2 * min(len(hits), 2)
            why.append("关键词:" + "/".join(hits[:2]))
        special = [k for k in promote_names if k in name]
        if special:
            score += 5
            why.append("好友/群申请")
        pushed = [k for k in demote_names if k in name] + [k for k in demote_preview if k in prev]
        if pushed:
            score -= 6
            why.append("推送特征")
        if row.get("push_avatar"):
            score -= 5
            why.append("公众号头像")
        # user-maintained lists win over the heuristics
        name_key = re.sub(r"[^\w\u4e00-\u9fff]", "", name + "|" + prev)
        if any(re.sub(r"[^\w\u4e00-\u9fff]", "", k) and re.sub(r"[^\w\u4e00-\u9fff]", "", k) in name_key
               for k in rules["always_push"]):
            score -= 10
            why.append("你的黑名单")
        if any(re.sub(r"[^\w\u4e00-\u9fff]", "", k) and re.sub(r"[^\w\u4e00-\u9fff]", "", k) in name_key
               for k in rules["always_reply"]):
            score += 6
            why.append("你的重点关注")
        if not name or name == "(未识别)":
            score -= 2
        bucket = "push" if score <= -4 else ("reply" if score >= 2 else "other")
        top.append({"row": row, "score": score, "why": why, "bucket": bucket})
    top.sort(key=lambda d: (-d["score"], -(len(d["row"].get("preview") or ""))))
    return top


def _table(lines: list[str], items: list[dict], with_reason: bool = True) -> None:
    if with_reason:
        lines.append("| 会话 | 时间 | 最后一条 | 为什么排上来 |")
        lines.append("| --- | --- | --- | --- |")
        for it in items:
            r = it["row"]
            lines.append(f"| {r['name'] or '(未识别)'} | {r['time'] or '-'} | "
                         f"{r['preview'] or '-'} | {'、'.join(it['why']) or '-'} |")
    else:
        lines.append("| 会话 | 时间 | 最后一条 |")
        lines.append("| --- | --- | --- |")
        for it in items:
            r = it["row"]
            lines.append(f"| {r['name'] or '(未识别)'} | {r['time'] or '-'} | {r['preview'] or '-'} |")


def write_markdown(res: dict, md_path: str, events: list[dict] | None = None) -> None:
    rows = res["rows"]
    cfg = load_config().get("digest", {})
    ranked = rank_rows(rows, cfg)
    top_n = int(cfg.get("top_n", 5))
    reply = [it for it in ranked if it["bucket"] == "reply"][:top_n]
    others = [it for it in ranked if it["bucket"] == "other"]
    pushes = [it for it in ranked if it["bucket"] == "push"]
    unread = sum(1 for r in rows if r.get("unread"))

    lines = [f"# 微信新消息速览 · {res['captured_at']}", ""]
    lines.append(f"- 会话 {len(rows)} 个，未读 {unread} 个，其中 **{len(reply)} 个可能需要你回**")
    lines.append(f"- 截图：[{os.path.basename(res['image'])}]({os.path.basename(res['image'])})")
    lines.append("")

    lines.append(f"## 先回这几个（{len(reply)}）")
    lines.append("")
    if reply:
        _table(lines, reply)
    else:
        lines.append("暂时没有需要优先处理的会话。")
    lines.append("")

    friends = friend_entries(rows)
    lines.append(f"## 今天谁加了你（{len(friends)}）")
    lines.append("")
    if friends:
        lines.append("| 时间 | 昵称 | 内容 |")
        lines.append("| --- | --- | --- |")
        for f in friends:
            lines.append(f"| {f.get('ts', '')[11:19] or '-'} | {f.get('name') or '-'} | {f.get('preview') or '-'} |")
    else:
        lines.append("今天没有新的好友申请或群邀请。")
    lines.append("")

    if others:
        lines.append(f"## 其余会话（{len(others)}）")
        lines.append("")
        _table(lines, others)
        lines.append("")

    if pushes:
        lines.append(f"## 推送 / 营销（{len(pushes)}，可以忽略）")
        lines.append("")
        _table(lines, pushes, with_reason=False)
        lines.append("")

    if events:
        lines.append(f"## 今天的新消息事件（{len(events)} 条）")
        lines.append("")
        lines.append("| 时间 | 事件 | 会话 | 内容 |")
        lines.append("| --- | --- | --- | --- |")
        for ev in events:
            lines.append(f"| {ev['ts'][11:19]} | {ev['kind']} | {ev['name']} | {ev.get('preview') or '-'} |")
        lines.append("")

    lines.append("> 排序规则：未读 +3，近 3 小时 +2，命中待办关键词 +2，好友/群申请 +5，营销特征 -6。")
    lines.append("> 会话名与预览来自截图 OCR，个别字符可能有误，以截图为准。")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


EVENTS_DIR = os.path.join(ROOT, "events")
STATE_FILE = os.path.join(ROOT, "state_last.json")
WATCH_LOG = os.path.join(ROOT, "logs", "watch.log")
CONFIG_FILE = os.path.join(ROOT, "config.json")
NOTIFY_PS1 = os.path.join(ROOT, "popup.ps1")
FRIEND_PAT = re.compile(r"新的朋友|朋友验证|好友申请|添加你|请求添加|请求加|邀请你加入|添加好友")


def friends_path(day: str | None = None) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    os.makedirs(EVENTS_DIR, exist_ok=True)
    return os.path.join(EVENTS_DIR, f"friends_{day}.jsonl")


def record_friends(rows: list[dict]) -> int:
    """Persist any friend-request / group-invite rows seen in this capture (deduped per day)."""
    hits = [r for r in rows
            if FRIEND_PAT.search((r.get("name") or "") + " " + (r.get("preview") or ""))]
    if not hits:
        return 0
    path = friends_path()
    seen = set()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    seen.add((ev.get("name"), ev.get("preview")))
        except OSError:
            pass
    added = 0
    with open(path, "a", encoding="utf-8") as fh:
        for r in hits:
            key = (r.get("name"), r.get("preview"))
            if key in seen:
                continue
            seen.add(key)
            ev = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "name": r.get("name") or "", "preview": r.get("preview") or ""}
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            added += 1
    if added:
        log_watch(f"new friend entries: {added}")
    return added


def friend_entries(rows: list[dict]) -> list[dict]:
    """Today's accumulated friend requests, filled in with anything visible right now."""
    out, seen = [], set()
    path = friends_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = (ev.get("name"), ev.get("preview"))
                    if key not in seen:
                        seen.add(key)
                        out.append(ev)
        except OSError:
            pass
    for r in rows:
        if FRIEND_PAT.search((r.get("name") or "") + " " + (r.get("preview") or "")):
            key = (r.get("name"), r.get("preview"))
            if key not in seen:
                seen.add(key)
                out.append({"ts": "", "name": r.get("name") or "", "preview": r.get("preview") or ""})
    return out

DEFAULT_CONFIG = {
    "notify": {
        "enabled": True,
        "min_interval_seconds": 300,   # at most one toast per 5 minutes
        "quiet_hours": ["23:30", "08:00"],
        "skip_when_focused": True,     # stay quiet while you are looking at WeChat
        "max_items": 4,
        "drop_after_minutes": 30,
    }
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_FILE):
        try:
            # utf-8-sig: tolerate a BOM, which Windows editors love to add
            with open(CONFIG_FILE, encoding="utf-8-sig") as fh:
                user = json.load(fh)
            for key, val in user.items():
                if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(val)
                else:
                    cfg[key] = val
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(now: datetime, window: list[str]) -> bool:
    if not window or len(window) != 2:
        return False
    start, end = _minutes(window[0]), _minutes(window[1])
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end          # window crosses midnight


def foreground_pid() -> int:
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def show_toast(title: str, body: str, focus_pid: int = 0, seconds: int = 8) -> bool:
    """Corner popup card. Windows toasts are silenced on this machine, so we draw our own."""
    try:
        subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", NOTIFY_PS1, "-Title", title, "-Body", body,
             "-Seconds", str(seconds), "-FocusPid", str(focus_pid or 0)],
            creationflags=0x08000000,   # CREATE_NO_WINDOW: no console flash
            close_fds=True,
        )
        return True
    except Exception as exc:
        log_watch(f"popup exception: {exc!r}")
        return False


def events_path(day: str | None = None) -> str:
    day = day or datetime.now().strftime("%Y-%m-%d")
    os.makedirs(EVENTS_DIR, exist_ok=True)
    return os.path.join(EVENTS_DIR, f"{day}.jsonl")


def log_watch(msg: str) -> None:
    os.makedirs(os.path.dirname(WATCH_LOG), exist_ok=True)
    with open(WATCH_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def load_events(day: str | None = None) -> list[dict]:
    p = events_path(day)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _key(preview: str) -> str:
    return re.sub(r"[\s·、。，,．“”\"'\[\]（）()]", "", preview or "")[:24]


def diff_unread(prev: list[dict] | None, cur: list[dict]) -> list[tuple[str, dict]]:
    """New-message events: a badge appeared, or an unread chat's preview changed."""
    if not prev:
        return [("当前未读", r) for r in cur if r["unread"]]
    prev_unread = {r["name"]: _key(r["preview"]) for r in prev if r["unread"]}
    events = []
    for row in cur:
        if not row["unread"]:
            continue
        old = prev_unread.get(row["name"])
        if old is None:
            events.append(("新未读", row))
        elif _key(row["preview"]) != old:
            events.append(("新内容", row))
    return events


def watch_loop(interval: int) -> int:
    log_watch(f"watcher start, interval={interval}s")
    prev = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                prev = json.load(fh).get("rows")
        except (json.JSONDecodeError, OSError):
            prev = None
    last_beat = 0.0
    last_notify = 0.0
    pending: list[dict] = []
    while True:
        try:
            pid, img, dist = pick_target(allow_restore=False)
            if not pid:
                log_watch("B window not capturable (minimized / logged out) - skipped this round")
            else:
                cur_png = os.path.join(DAILY, "watch_current.png")
                os.replace(img, cur_png)
                res = parse_panel(cur_png)
                rows = res["rows"]
                record_friends(rows)
                events = diff_unread(prev, rows)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                if events:
                    shot = os.path.join(DAILY, f"event_{stamp}.png")
                    Image.open(cur_png).save(shot)
                    for kind, row in events:
                        ev = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind,
                              "name": row["name"], "time": row["time"], "preview": row["preview"],
                              "screenshot": os.path.basename(shot)}
                        with open(events_path(), "a", encoding="utf-8") as fh:
                            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                        log_watch(f"{kind}: {row['name']} | {row['preview'][:40]}")
                        pending.append({"at": time.time(), "kind": kind, "name": row["name"],
                                        "preview": row["preview"]})
                with open(STATE_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "rows": rows},
                              fh, ensure_ascii=False)
                prev = rows

                # gentle notifications: aggregate, rate-limit, respect quiet hours and focus
                ncfg = load_config()["notify"]
                expire = ncfg["drop_after_minutes"] * 60
                pending[:] = [p for p in pending if time.time() - p["at"] < expire]
                if pending and ncfg["enabled"]:
                    now = datetime.now()
                    due = time.time() - last_notify >= ncfg["min_interval_seconds"]
                    quiet = in_quiet_hours(now, ncfg["quiet_hours"])
                    focused = ncfg["skip_when_focused"] and foreground_pid() == pid
                    log_watch(f"notify check: pending={len(pending)} due={due} quiet={quiet} "
                              f"focused={focused} fg={foreground_pid()} pid={pid}")
                    if due and not quiet and not focused:
                        names = []
                        for item in reversed(pending):
                            label = item["name"] or "(未识别)"
                            if label not in names:
                                names.append(label)
                        head = names[:ncfg["max_items"]]
                        more = f" 等 {len(names)} 个会话" if len(names) > len(head) else ""
                        newest = pending[-1]
                        label = newest["name"] or "(未识别)"
                        detail = newest["preview"][:40] or newest["kind"]
                        if len(head) > 1:
                            body = f"{label}：{detail}\n另有：{'、'.join(head[1:])}{more}"
                        else:
                            body = f"{label}：{detail}"
                        if show_toast("微信有新消息", body, focus_pid=pid, seconds=8):
                            log_watch(f"popup shown: {body.replace(chr(10), ' / ')[:60]}")
                            last_notify = time.time()
                            pending.clear()

                if time.time() - last_beat > 1800:
                    unread = sum(1 for r in rows if r["unread"])
                    log_watch(f"heartbeat: {len(rows)} sessions, {unread} unread")
                    last_beat = time.time()
        except Exception as exc:  # keep the watcher alive no matter what
            import traceback
            log_watch(f"error: {exc!r}")
            log_watch(traceback.format_exc().replace("\n", " | "))
        time.sleep(max(15, interval))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--tag", default="manual")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--include-events", action="store_true")
    ap.add_argument("--watch", type=int, metavar="SECONDS")
    args = ap.parse_args()

    if args.watch:
        return watch_loop(args.watch)

    os.makedirs(DAILY, exist_ok=True)

    if args.discover:
        print(json.dumps({str(k): v for k, v in discover().items()}, ensure_ascii=False))
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.learn:
        pid = args.pid
        if not pid:
            pid, tmp, _ = pick_target()
            if not pid:
                print("no logged-in WeChat window found", file=sys.stderr)
                return 2
        else:
            tmp = os.path.join(DAILY, f"learn_{pid}.png")
            capture_window(pid, tmp)
        shot = Image.open(tmp).convert("RGB")
        crop = find_avatar(shot) or shot.crop(AVATAR_RAIL_BOX)
        crop.save(REF_AVATAR)
        print(f"reference avatar saved from pid={pid} -> {REF_AVATAR}")
        return 0

    if args.image:
        img_path, pid = args.image, None
    elif args.pid:
        img_path, pid = os.path.join(DAILY, f"wechat_{stamp}.png"), args.pid
        capture_window(args.pid, img_path)
    elif args.auto:
        pid, img_path, dist = pick_target(allow_restore=True)
        if not pid:
            print("no logged-in WeChat window found", file=sys.stderr)
            if args.md:
                md = os.path.join(DAILY, f"{datetime.now():%Y-%m-%d_%H%M}.md")
                with open(md, "w", encoding="utf-8") as fh:
                    fh.write(f"# 微信新消息速览 · {datetime.now():%Y-%m-%d %H:%M}\n\n"
                             "未找到已登录的微信窗口（可能未运行、未登录或被最小化到托盘）。\n")
                print(f"md: {md}")
            return 2
        final = os.path.join(DAILY, f"wechat_{stamp}.png")
        os.replace(img_path, final)
        img_path = final
    else:
        ap.error("need --image, --pid, --auto, --learn or --discover")

    res = parse_panel(img_path, debug_dir=DAILY)
    record_friends(res["rows"])
    res["source_pid"] = pid
    res["tag"] = args.tag
    jpath = os.path.join(DAILY, f"digest_{stamp}.json")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)

    print(f"image: {img_path}")
    print(f"json:  {jpath}")
    if args.md:
        md = os.path.join(DAILY, f"{datetime.now():%Y-%m-%d_%H%M}.md")
        write_markdown(res, md, load_events() if args.include_events else None)
        print(f"md:    {md}")

    print(f"{'!':2} {'TIME':12} {'NAME':24} PREVIEW")
    for row in res["rows"]:
        print(f"{'●' if row['unread'] else ' ':2} {row['time']:12} {row['name']:24} {row['preview']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
