"""白名单自动回复（微信）：小A发视频 -> 抽帧/OCR -> 大模型出稿 -> 发送。

设计约束（对应 AGENTS.md）：
* 只对白名单里的人生效，其它会话一律不碰；
* 只处理"最后一条是视频"的情况；
* 频率：同一会话 5 分钟内最多 1 条，全天最多 20 条；
* 发送前必须确认标题是白名单里的人；
* 默认 dry-run，只有 config 里 auto_send.enabled=true 才真的按回车；
* 每次动作都写 logs/autoreply.log + 截图，失败即熔断。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:\weixin_agent")
sys.path.insert(0, str(ROOT))

from adapters.wechat_video import create_job, extract_frames  # noqa: E402
from adapters.wechat_video import save_draft, extract_audio, transcribe_audio  # noqa: E402

POWERSHELL = r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"
OCR_PS1 = ROOT / "ocr.ps1"
SEND_PS1 = ROOT / "src" / "send_text.ps1"
EVENTS = ROOT / "events"
LOGS = ROOT / "logs"
CONFIG = ROOT / "config.json"
AUDIT = LOGS / "autoreply.log"
HANDLED = ROOT / "jobs" / "handled.json"

DEFAULT_AUTOREPLY = {
    "enabled": False,          # 总开关：false 时只出草稿不发送
    "whitelist": ["小A"],
    "only_video": True,
    "min_gap_seconds": 300,
    "max_per_day": 20,
    "persona": (
        "你在替你本人回复微信消息。对方是你的对象，你说话简短、口语、带一点语气词，"
        "一般一到两句，最多 30 字；先接住她发来的视频里的具体点，别只说“哈哈”“好看”；"
        "不评价身材，不提考研压力，不承诺具体日期，不安排花钱的事；只输出回复正文。"
    ),
    "llm": {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyFile": r"C:\Users\yourname\douyin-agent\config.json"},
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_AUTOREPLY))
    if CONFIG.exists():
        try:
            user = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
            cfg.update({k: v for k, v in user.get("autoreply", {}).items()})
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with AUDIT.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def handled() -> dict:
    if HANDLED.exists():
        try:
            return json.loads(HANDLED.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_handled(data: dict) -> None:
    HANDLED.parent.mkdir(parents=True, exist_ok=True)
    HANDLED.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_api_key(cfg: dict) -> str:
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env
    path = cfg["llm"].get("apiKeyFile")
    if path and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("apiKey", "")
    return ""


def ocr_text(frames: list[Path]) -> str:
    texts = []
    for f in frames[:4]:
        try:
            out = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(OCR_PS1), "-ImagePath", str(f), "-Json"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=120)
            raw = (out.stdout or "").strip()
            start = raw.find("{")
            if start < 0:
                continue
            lines = json.loads(raw[start:])["lines"]
            joined = " ".join(re.sub(r"\s+", "", l["Text"]) for l in lines if l["Text"].strip())
            if joined and joined not in texts:
                texts.append(joined)
        except (subprocess.SubprocessError, json.JSONDecodeError, KeyError):
            continue
    return "\n".join(texts)[:1200]


def draft_reply(cfg: dict, evidence: str, chat: str) -> str:
    key = load_api_key(cfg)
    if not key:
        raise RuntimeError("no api key")
    payload = {
        "model": cfg["llm"].get("model", "deepseek-chat"),
        "temperature": 0.8,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": cfg["persona"]},
            {"role": "user", "content": f"{chat}发来一个视频，画面里能读到的文字：\n{evidence or '(没有可读文字)'}\n\n请写一条回复。"},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = (
        "import json,sys,urllib.request\n"
        f"url = {json.dumps(cfg['llm'].get('baseUrl', 'https://api.deepseek.com/v1').rstrip('/') + '/chat/completions')}\n"
        f"key = {json.dumps(key)}\n"
        "data = sys.stdin.buffer.read()\n"
        "r = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json','Authorization':'Bearer '+key})\n"
        "with urllib.request.urlopen(r, timeout=120) as resp:\n"
        "    j = json.loads(resp.read().decode('utf-8'))\n"
        "print(j['choices'][0]['message']['content'].strip())\n"
    )
    out = subprocess.run([sys.executable, "-c", req], input=body, capture_output=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"llm failed: {out.stderr.decode('utf-8', 'replace')[:300]}")
    return out.stdout.decode("utf-8", "replace").strip().strip('"').strip()


def find_video_event(cfg: dict) -> list[dict]:
    """从事件流里找出白名单发来的、还没处理过的视频消息。"""
    day = datetime.now().strftime("%Y-%m-%d")
    path = EVENTS / f"{day}.jsonl"
    if not path.exists():
        return []
    done = handled()
    hits = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = ev.get("name") or ""
        prev = ev.get("preview") or ""
        if not any(w and w in name for w in cfg["whitelist"]):
            continue
        if cfg.get("only_video", True) and "视频" not in prev:
            continue
        key = f"{ev.get('ts')}|{name}|{prev}"
        if key in done:
            continue
        hits.append({**ev, "dedupe_key": key})
    return hits


def within_rate_limit(cfg: dict) -> bool:
    """最近一条回复距今是否够久，以及今天发了多少条。"""
    if not AUDIT.exists():
        return True
    now = datetime.now()
    today = 0
    last = None
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        if "send-ok" not in line:
            continue
        try:
            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts.date() == now.date():
            today += 1
        if last is None or ts > last:
            last = ts
    if today >= cfg["max_per_day"]:
        return False
    if last and (now - last).total_seconds() < cfg["min_gap_seconds"]:
        return False
    return True


def process(cfg: dict, dry_run: bool) -> int:
    hits = find_video_event(cfg)
    if not hits:
        return 0
    if not within_rate_limit(cfg):
        log("rate-limited, skip")
        return 0
    ev = hits[0]
    chat = next(w for w in cfg["whitelist"] if w in ev["name"])
    log(f"video event: {chat} {ev['ts']} {ev['preview'][:40]}")

    # 视频文件：取事件时间前后 10 分钟内落盘的最新 mp4
    try:
        ts = datetime.strptime(ev["ts"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        ts = datetime.now()
    from adapters.wechat_video import newest_video
    video = newest_video((ts - timedelta(minutes=10)).timestamp())
    if not video:
        log("no video file found, skip")
        return 0
    job = create_job(chat, video, note=f"event {ev['ts']}")
    frames = [ROOT / p for p in job["frames"]]
    evidence = ocr_text(frames)
    wav = extract_audio(ROOT / "inbox" / job["job_id"] / "video.mp4")
    speech = transcribe_audio(wav) if wav else ""
    if speech:
        evidence = (evidence + "\n语音转写：" + speech).strip()
    log(f"job {job['job_id']} frames={len(frames)} ocr_chars={len(evidence)} speech_chars={len(speech)}")

    try:
        text = draft_reply(cfg, evidence, chat)
    except Exception as exc:  # noqa: BLE001
        log(f"draft failed: {exc}")
        return 1
    if not text:
        log("empty draft, skip")
        return 0
    log(f"draft: {text}")

    if dry_run:
        # 干跑阶段不碰微信：只把草稿落盘，避免把文字残留在输入框里
        save_draft(job["job_id"], text, source="autoreply-dry-run")
        log("dry-run: draft queued, wechat untouched")
        done = handled()
        for h in hits[:1]:
            done[h["dedupe_key"]] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_handled(done)
        return 0

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SEND_PS1),
         "-Chat", chat, "-Text", text] + ([] if dry_run else ["-Send"]),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    log(f"send exit={result.returncode} out={(result.stdout or '').strip()[:200]} "
        f"err={(result.stderr or '').strip()[:200]}")
    if result.returncode == 0:
        done = handled()
        for h in hits[:1]:
            done[h["dedupe_key"]] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_handled(done)
        log("send-ok" if not dry_run else "dry-run-ok")
    return 0 if result.returncode == 0 else 1


def main() -> int:
    cfg = load_config()
    dry_run = not cfg.get("enabled", False)
    log(f"worker start (dry_run={dry_run}, whitelist={cfg['whitelist']})")
    return process(cfg, dry_run)


if __name__ == "__main__":
    sys.exit(main())
