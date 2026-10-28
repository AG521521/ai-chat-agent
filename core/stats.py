"""确定性统计 + 分层采样（借鉴 863401402/she-love-me 的三层架构）。

问题：把"最近 N 条原文"塞进提示词，既贵又容易让模型以为"这就是全部历史"。
做法：
1. 先在本地算出确定性统计（条数、角色比、活跃天数、常见词、间隔、近期分布）；
2. 再按"近期 + 历史分层"取样少量代表片段；
3. 提示词里明确标注"样本只覆盖部分历史"。

统计全部是本地字符串/时间运算，不调用模型，可复现、可解释。
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z]{3,}")
_STOP = {
    "今天", "明天", "昨天", "什么", "怎么", "就是", "这个", "那个", "我们", "你们",
    "他们", "可以", "没有", "不是", "还是", "然后", "现在", "时候", "知道", "觉得",
    "自己", "一个", "这样", "那样", "真的", "有点", "已经", "因为", "所以", "但是",
}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _terms(text: str, top: int = 8) -> list[tuple[str, int]]:
    """无依赖的中文 bigram + 英文词频统计（只用于"常见词"提示，不做语义）。"""
    c = Counter()
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _STOP:
                c[bg] += 1
    for w in _WORD.findall(text or ""):
        c[w.lower()] += 1
    return [(k, v) for k, v in c.most_common(top) if v >= 2]


def compute_stats(turns: list[dict[str, Any]], namespace: str) -> dict[str, Any]:
    """对某个命名空间的全部对话轮次做确定性统计。"""
    if not turns:
        return {
            "namespace": namespace,
            "total": 0,
            "from_contact": 0,
            "from_self": 0,
            "days_active": 0,
            "avg_chars": 0,
            "median_gap_minutes": None,
            "top_terms": [],
            "last_7d": 0,
            "note": "没有历史记录",
        }

    contact = [t for t in turns if t.get("role") == "contact"]
    myself = [t for t in turns if t.get("role") == "self"]
    stamps = [s for s in (_parse_ts(t.get("at")) for t in turns) if s]
    lengths = [len((t.get("text") or "")) for t in turns]

    gaps: list[float] = []
    for prev, cur in zip(stamps, stamps[1:]):
        delta = (cur - prev).total_seconds() / 60.0
        if 0 <= delta < 60 * 24:
            gaps.append(delta)
    gaps.sort()
    median_gap = gaps[len(gaps) // 2] if gaps else None

    now = datetime.now(stamps[0].tzinfo) if stamps and stamps[0].tzinfo else datetime.now()
    last7 = 0
    if stamps:
        for s in stamps:
            try:
                if (now - s).days <= 7:
                    last7 += 1
            except TypeError:
                continue

    joined = "\n".join(t.get("text") or "" for t in turns)
    return {
        "namespace": namespace,
        "total": len(turns),
        "from_contact": len(contact),
        "from_self": len(myself),
        "days_active": len({s.date() for s in stamps}),
        "avg_chars": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        "median_gap_minutes": round(median_gap, 1) if median_gap is not None else None,
        "top_terms": _terms(joined),
        "last_7d": last7,
        "note": "全部为本地确定性统计，不涉及模型推断",
    }


def stratified_sample(
    turns: list[dict[str, Any]],
    recent_limit: int = 12,
    older_limit: int = 6,
) -> list[dict[str, Any]]:
    """近期全取 + 历史均匀抽样，返回按时间正序的样本。"""
    if not turns:
        return []
    if len(turns) <= recent_limit + older_limit:
        return list(turns)

    recent = turns[-recent_limit:]
    older = turns[:-recent_limit]
    if older_limit <= 0 or not older:
        return recent

    step = max(1, len(older) // older_limit)
    picked = [older[i] for i in range(0, len(older), step)][:older_limit]
    seen = {(t.get("at"), t.get("role"), t.get("text")) for t in picked}
    merged = picked + [t for t in recent if (t.get("at"), t.get("role"), t.get("text")) not in seen]
    return merged


def render_stats(stats: dict[str, Any]) -> str:
    """把统计渲染成提示词里的一段短文本。"""
    if not stats or not stats.get("total"):
        return "(无历史统计)"
    top = "、".join(f"{k}×{v}" for k, v in (stats.get("top_terms") or [])[:6]) or "(无)"
    gap = stats.get("median_gap_minutes")
    return "\n".join(
        [
            f"总轮次 {stats['total']}（对方 {stats['from_contact']} / 我 {stats['from_self']}）",
            f"活跃天数 {stats['days_active']}，近 7 天 {stats['last_7d']} 条，平均每条 {stats['avg_chars']} 字",
            f"相邻消息间隔中位数 {gap if gap is not None else '未知'} 分钟",
            f"高频片段：{top}",
            "注意：以上是本地统计，样本只覆盖部分历史，不要假设你看到了全部对话。",
        ]
    )
