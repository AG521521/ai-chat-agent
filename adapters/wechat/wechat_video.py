"""微信视频消息 -> 抽帧 -> 待回复任务。

只做"看到视频"这一步：把最近的视频文件找出来、抽帧、写进 inbox 和待办队列。
回复内容由上层（代理或人）填写，发送由 send_text.ps1 负责，默认 dry-run。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\weixin_agent")
INBOX = ROOT / "inbox"
JOBS = ROOT / "jobs"
PENDING = JOBS / "pending.jsonl"
DRAFTS = JOBS / "drafts.jsonl"
FFMPEG = Path(r"C:\Users\yourname\douyin-agent\node_modules\ffmpeg-static\ffmpeg.exe")
WECHAT_VIDEO_ROOT = Path(r"D:\微信\xwechat_files")

FRAME_COUNT = 6
# 长音频分段：参照 yzfly/douyin-mcp-server（9 分钟一段），避免整段喂模型
SEGMENT_SECONDS = 540


def video_dirs() -> list[Path]:
    """每个账号的 msg/video/<yyyy-mm> 目录。"""
    out = []
    for acct in WECHAT_VIDEO_ROOT.glob("wxid_*"):
        base = acct / "msg" / "video"
        if base.is_dir():
            out.extend(p for p in base.iterdir() if p.is_dir())
    return out


def newest_video(since_ts: float, scan_limit: int = 400) -> Path | None:
    """在 since_ts 之后落盘的最新一个 mp4（微信把收到的视频存成 md5.mp4）。"""
    newest: tuple[float, Path] | None = None
    checked = 0
    for d in video_dirs():
        try:
            entries = sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for p in entries:
            checked += 1
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < since_ts:
                break  # sorted desc, the rest are older
            if newest is None or mtime > newest[0]:
                newest = (mtime, p)
            if checked >= scan_limit:
                break
        if checked >= scan_limit:
            break
    return newest[1] if newest else None


def extract_frames(video: Path, out_dir: Path, count: int = FRAME_COUNT) -> list[Path]:
    """均匀抽 count 帧，长边缩到 480，够看清画面又不占空间。"""
    if not FFMPEG.exists():
        raise RuntimeError(f"ffmpeg not found: {FFMPEG}")
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    # fps=count/duration 让帧均匀铺满整段；极短视频退化成每秒一帧
    step = max(duration / max(count, 1), 0.5)
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(video), "-vf", f"fps=1/{step:.3f},scale=480:-2",
           "-frames:v", str(count), str(out_dir / "f%02d.jpg")]
    subprocess.run(cmd, check=True, timeout=180)
    frames = sorted(out_dir.glob("f*.jpg"))
    if not frames:
        subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(video), "-vf", "fps=1,scale=480:-2",
                        "-frames:v", str(count), str(out_dir / "f%02d.jpg")],
                       check=True, timeout=180)
        frames = sorted(out_dir.glob("f*.jpg"))
    return frames


def probe_duration(video: Path) -> float:
    ffprobe = FFMPEG.with_name("ffprobe.exe")
    try:
        if ffprobe.exists():
            out = subprocess.run([str(ffprobe), "-v", "error", "-show_entries",
                                  "format=duration", "-of", "default=nw=1:nk=1", str(video)],
                                 capture_output=True, text=True, timeout=60)
            return float(out.stdout.strip() or 0)
        out = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(video)],
                             capture_output=True, text=True, timeout=60)
        for line in out.stderr.splitlines():
            if "Duration:" in line:
                hh, mm, ss = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    return 10.0


def create_job(chat: str, video: Path, note: str = "") -> dict:
    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    work = INBOX / job_id
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, work / "video.mp4")
    frames = extract_frames(work / "video.mp4", work / "frames")
    meta = {
        "job_id": job_id,
        "chat": chat,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "video_src": str(video),
        "video_mtime": datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": round(probe_duration(work / "video.mp4"), 1),
        "frames": [str(p.relative_to(ROOT)) for p in frames],
        "note": note,
        "status": "pending",
    }
    (work / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    JOBS.mkdir(parents=True, exist_ok=True)
    with PENDING.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
    return meta


def pending_jobs() -> list[dict]:
    if not PENDING.exists():
        return []
    out = []
    with PENDING.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def save_draft(job_id: str, text: str, source: str = "agent") -> dict:
    JOBS.mkdir(parents=True, exist_ok=True)
    draft = {"job_id": job_id, "text": text, "source": source,
             "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "draft"}
    with DRAFTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(draft, ensure_ascii=False) + "\n")
    return draft


def mark_status(job_id: str, status: str) -> None:
    for line in (PENDING, DRAFTS):
        if not line.exists():
            continue
        rows = []
        changed = False
        with line.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("job_id") == job_id:
                    obj["status"] = status
                    obj["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
                rows.append(obj)
        if changed:
            with line.open("w", encoding="utf-8") as fh:
                for obj in rows:
                    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


ASR_JS = ROOT / "adapters" / "asr.mjs"


def audio_is_loud(wav: Path) -> bool:
    """音量太低（彩铃、静音、只有环境声）就不值得转写。"""
    try:
        out = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(wav), "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        for line in out.stderr.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip()) > -38
    except (OSError, ValueError, subprocess.SubprocessError):
        return True
    return True


def clean_asr(text: str) -> str:
    """whisper 在静音上会复读同一句，检测退化输出并丢弃。"""
    t = (text or "").strip()
    if not t:
        return ""
    for size in range(3, 13):
        chunk = t[:size]
        if chunk and t.count(chunk) > 4 and len(t) > size * 5:
            return ""
    if len(t) > 40 and len(set(t)) < 12:
        return ""
    if len(t) > 500:
        t = t[:500]
    return t


def extract_audio(video: Path, out_wav: Path, max_seconds: int = 0) -> Path | None:
    """抽 16k 单声道 wav。max_seconds=0 表示整段（默认），不再像以前那样只取前 90 秒。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    cmd.append(str(out_wav))
    try:
        subprocess.run(cmd, check=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        return None
    return out_wav if out_wav.exists() and out_wav.stat().st_size > 2000 else None


def wav_seconds(wav: Path) -> float:
    """16k/16bit/单声道 wav 的时长（按文件大小估算，避免再调 ffprobe）。"""
    try:
        return max(0.0, (wav.stat().st_size - 44) / (16000 * 2))
    except OSError:
        return 0.0


def split_audio(wav: Path, out_dir: Path, segment_seconds: int = SEGMENT_SECONDS) -> list[Path]:
    """把长音频切成 N 段（默认 9 分钟），返回按顺序排列的分段。

    参照 douyin-mcp-server 的做法：长音频整段喂模型既慢又容易超时/超限。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("seg_*.wav"):
        old.unlink(missing_ok=True)
    pattern = str(out_dir / "seg_%03d.wav")
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
           "-f", "segment", "-segment_time", str(segment_seconds), "-c", "copy", pattern]
    try:
        subprocess.run(cmd, check=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        return []
    return sorted(out_dir.glob("seg_*.wav"))


def transcribe_audio(wav: Path, timeout: int = 900) -> str:
    """调用本地 whisper-small；长音频自动分段转写后拼接。"""
    if not asr_available():
        return ""
    segments: list[Path]
    if wav_seconds(wav) > SEGMENT_SECONDS + 30:
        segments = split_audio(wav, wav.parent / "audio_segments")
        if not segments:
            segments = [wav]
    else:
        segments = [wav]

    parts: list[str] = []
    for seg in segments:
        if not audio_is_loud(seg):
            continue
        try:
            out = subprocess.run(["node", str(ASR_JS), str(seg)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (subprocess.SubprocessError, OSError):
            continue
        if out.returncode != 0:
            continue
        text = clean_asr(out.stdout)
        if text:
            parts.append(text)
    return "\n".join(parts)


def asr_available() -> bool:
    return ASR_JS.exists() and (ROOT / "node_modules").exists()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--since-min", type=float, default=30, help="只看最近多少分钟内落盘的视频")
    ap.add_argument("--chat", default="小A")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print(json.dumps(pending_jobs(), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    since = time.time() - args.since_min * 60
    v = newest_video(since)
    if not v:
        print(f"no new video in the last {args.since_min} min")
        raise SystemExit(1)
    meta = create_job(args.chat, v)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
