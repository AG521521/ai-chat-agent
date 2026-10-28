"""联系人名匹配：界面读到的名字常有空格、备注后缀、全角括号。

规则（保守）：
* 全等即命中；
* 否则按分隔符切词，**任一切词与白名单条目全等**才算命中；
* 不做任意子串匹配 —— 避免"白名单粉丝"这种名字误命中。
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[（）()\[\]【】《》<>·,，、\-_/\\|｜\s]+")


def norm_contact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def contact_tokens(value: str) -> set[str]:
    raw = (value or "").strip()
    parts = {p for p in _SPLIT.split(raw) if p}
    whole = norm_contact(raw)
    if whole:
        parts.add(whole)
    return parts


def matches_alias(alias: str, contact_name: str) -> bool:
    """alias 是否指向这个联系人（白名单条目 / 记忆合并别名）。"""
    a = norm_contact(alias)
    if not a:
        return False
    tokens = contact_tokens(contact_name)
    return a in tokens
