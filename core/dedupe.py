"""去重键与模糊重复判定。

原则（执行规范 2.去重要求）：
* 优先用平台原生消息 ID；
* 拿不到就组合「平台 + 联系人 + 类型 + 规范化文本/媒体摘要」；
* 模糊相似不算"已确认重复"，只作为提示，交由上层走人工或等下一次观测。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher

from .model import IncomingMessage

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）()\[\]【】<>《》…—\-_/\\|,.!?;:'\"`~@#$%^&*+=]")
_KEEP = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]")


def normalize(text: str) -> str:
    """规范化文本：全半角统一、去零宽、压空白、保留标点。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def content_fingerprint(text: str) -> str:
    """只保留中英文数字后的指纹，用于内容级去重（标点变化不影响）。"""
    t = _KEEP.sub("", normalize(text)).lower()
    return t


def media_signature(msg: IncomingMessage) -> str:
    """媒体摘要签名：优先显式 media_sig，其次媒体路径末段，最后媒体摘要文本。"""
    if msg.media_sig:
        return f"sig:{msg.media_sig}"
    if msg.media_path:
        return f"path:{msg.media_path.replace('\\', '/').rsplit('/', 1)[-1]}"
    if msg.media_summary:
        return "sum:" + content_fingerprint(msg.media_summary)[:64]
    return ""


def dedupe_key(msg: IncomingMessage) -> str:
    """稳定去重键。

    有原生消息 ID 时它是一个强键；否则退化为内容指纹 + 媒体签名。
    注意：退化键只能证明"看起来是同一条"，不能证明"平台上是同一条"。
    """
    platform = msg.platform
    contact = normalize(msg.contact_id)
    if msg.source_message_id:
        raw = f"id|{platform}|{contact}|{msg.source_message_id}"
        return "m_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    parts = [
        "ui",
        platform,
        contact,
        msg.content_type,
        content_fingerprint(msg.text)[:200],
        media_signature(msg)[:80],
    ]
    raw = "|".join(parts)
    return "c_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def similarity(a: str, b: str) -> float:
    """0..1 的文本相似度（规范化后）。"""
    x = content_fingerprint(a)
    y = content_fingerprint(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    return SequenceMatcher(None, x, y).ratio()


def duplicate_hint(a: str, b: str, threshold: float = 0.85) -> str:
    """返回 ``confirmed`` / ``possible`` / ``none``。

    confirmed 仅代表"内容指纹完全一致"，仍不等于平台级同一条消息；
    调用方必须把它和原生 ID 的确定性区分开。
    """
    fa, fb = content_fingerprint(a), content_fingerprint(b)
    if fa and fb and fa == fb:
        return "confirmed"
    if similarity(a, b) >= threshold:
        return "possible"
    return "none"
