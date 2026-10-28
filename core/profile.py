"""关系画像：从真实聊天里总结"她怎么说话、你怎么回、什么有效"。

方法借鉴 863401402/she-love-me：
先用确定性统计（条数、长度、问句比例、高频词）打底，再让模型做定性总结，
并且要求给真实对话样例，避免模型空谈。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

PROFILE_SYSTEM = """你是一个很懂亲密关系的分析师。下面是一对情侣的真实聊天统计和样例
（男方在备考，女方在集训）。请总结出**可以直接用来改善回复**的规律。

要求：
- 只根据给出的数据说话，不要编造没出现过的内容；
- 重点回答：她平时怎么说话、她在意什么、他现在的回复哪里好/哪里差、以后该怎么回；
- 给 8 条以内的"该做"和"别做"；
- 从样例里挑 6 组最典型的（她说了什么 → 他怎么回）原样保留，供以后模仿；
- 不要输出建议"分手/试探/冷处理"这类内容。

只返回 JSON：
{
  "her_style": ["她说话的特点"],
  "her_cares": ["她在意的点"],
  "his_style": ["他现在的回复风格"],
  "do": ["以后该怎么做"],
  "dont": ["以后别怎么做"],
  "sample_pairs": [{"her": "她的话", "him": "他的话"}],
  "confidence": 0.0
}"""


def _terms(texts: list[str], top: int = 10) -> list[list[Any]]:
    c = Counter()
    for t in texts:
        for run in re.findall(r"[\u4e00-\u9fff]+", t or ""):
            for i in range(len(run) - 1):
                c[run[i : i + 2]] += 1
    return [[k, v] for k, v in c.most_common(top) if v >= 2]


def compute_stats(hers: list[str], his: list[str], pairs: list[dict[str, str]]) -> dict[str, Any]:
    q = sum(1 for t in hers if re.search(r"[?？]|吗|呢|吧$", t or ""))
    return {
        "her_count": len(hers),
        "his_count": len(his),
        "her_avg_len": round(sum(len(t) for t in hers) / len(hers), 1) if hers else 0,
        "his_avg_len": round(sum(len(t) for t in his) / len(his), 1) if his else 0,
        "her_question_ratio": round(q / len(hers), 2) if hers else 0,
        "her_top_terms": _terms(hers),
        "his_top_terms": _terms(his),
        "pair_count": len(pairs),
    }


def build_messages(stats: dict[str, Any], pairs: list[dict[str, str]]) -> list[dict[str, str]]:
    sample = "\n".join(
        f"- 她：{p['her'][:60]}\n  他：{p['him'][:60]}" for p in pairs[:40]
    ) or "(无)"
    user = (
        "【统计】\n"
        f"她 {stats.get('her_count')} 条，平均 {stats.get('her_avg_len')} 字，"
        f"问句比例 {stats.get('her_question_ratio')}\n"
        f"他 {stats.get('his_count')} 条，平均 {stats.get('his_avg_len')} 字\n"
        f"她高频词：{stats.get('her_top_terms')}\n"
        f"他高频词：{stats.get('his_top_terms')}\n\n"
        "【真实对话样例】\n" + sample + "\n\n请按要求返回 JSON。"
    )
    return [{"role": "system", "content": PROFILE_SYSTEM}, {"role": "user", "content": user}]


def parse_notes(content: str) -> dict[str, Any]:
    if not content or not content.strip():
        raise ValueError("空的画像结果")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("没有 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("不是对象")
    out: dict[str, Any] = {}
    for key in ("her_style", "her_cares", "his_style", "do", "dont"):
        val = data.get(key)
        if isinstance(val, list):
            out[key] = [str(x)[:80] for x in val[:8]]
        elif val:
            out[key] = [str(val)[:80]]
        else:
            out[key] = []
    pairs = data.get("sample_pairs")
    out["sample_pairs"] = [
        {"her": str(p.get("her", ""))[:80], "him": str(p.get("him", ""))[:80]}
        for p in (pairs or [])
        if isinstance(p, dict) and p.get("her") and p.get("him")
    ][:6]
    out["confidence"] = round(float(data.get("confidence", 0.0)), 3)
    return out
