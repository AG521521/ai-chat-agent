"""「她想要什么」分析层。

视频/图文不是用来复述的，是用来读人的：她发这个，通常是在要一种反应
（被哄、被夸、被表态、被陪着吐槽、想让你做点什么）。
这一层只输出**假设**，不强加事实；置信度低就直说不知道，交给人工。

输出必须是结构化 JSON，解析失败一律降级为"未知"，绝不默认有把握。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .model import IncomingMessage

DESIRE_SYSTEM = """你是一个很懂女生的情感分析师。你的任务不是复述视频内容，
而是判断：她把这个发给他，**心里想要什么反应**。

你要结合三样东西：
1. 视频/图片本身的文案、字幕、语音转写（可能不完整）；
2. 最近的真实聊天（她最近的情绪、在忙什么、两人的节奏）；
3. 角色的关系背景与红线。

判断要点：
- 她发"别人男朋友多好"类的视频 → 多半是在**暗示/试探**，不是真的在夸别人；
- 她发吃的、想去的、好看的 → 可能是**想要你安排/表态**；
- 她发练功、受伤、累 → 想要**心疼和具体的关心**，不想要大道理；
- 她发吐槽、吵架、情感观点类 → 想要**认同和站队**；
- 她发搞笑、萌宠、日常 → 想要**一起乐一下**；
- 她发自己的照片/视频 → 想要**被夸，但要具体、不油腻**。

硬性要求：
- 这是推断，不是事实。证据不足就把 confidence 调低、把 uncertain 设为 true；
- 不要把"她一定想要"写成事实；
- 不要建议花钱、不要建议承诺具体日期；
- 不要输出任何回复正文，只输出分析。

只返回一个 JSON 对象：
{
  "content_summary": "这条视频/图大概在讲什么（一句话）",
  "why_she_sent": "她为什么发给你（一句话，标注是推断）",
  "what_she_wants": ["被哄", "被夸", "想让你表态"],
  "emotional_need": "她此刻的情绪需求（一句话）",
  "suggested_move": "你建议他做的具体一步（不花钱、不承诺日期）",
  "avoid": ["不要讲道理", "不要敷衍"],
  "confidence": 0.0,
  "uncertain": true
}"""

ALLOWED_WANTS = {
    "被哄", "被夸", "被陪", "被理解", "想让你表态", "想让你行动",
    "想让你一起乐", "想被关心身体", "试探态度", "只想分享",
}


def _clean_list(value: Any, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = str(item).strip()
        if text:
            out.append(text[:60])
    return out


def parse_desire(content: str) -> dict[str, Any]:
    """解析模型输出；不合规就抛 ValueError（调用方降级处理）。"""
    if not content or not content.strip():
        raise ValueError("空的欲望分析结果")
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

    required = {"content_summary", "why_she_sent", "what_she_wants", "suggested_move", "confidence"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"缺少字段: {sorted(missing)}")

    confidence = float(data.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence 越界")

    wants = _clean_list(data.get("what_she_wants"))
    return {
        "content_summary": str(data.get("content_summary", ""))[:200],
        "why_she_sent": str(data.get("why_she_sent", ""))[:200],
        "what_she_wants": wants,
        "emotional_need": str(data.get("emotional_need", ""))[:160],
        "suggested_move": str(data.get("suggested_move", ""))[:200],
        "avoid": _clean_list(data.get("avoid"), 5),
        "confidence": round(confidence, 3),
        "uncertain": bool(data.get("uncertain", True)),
    }


def render_for_draft(desire: dict[str, Any]) -> str:
    """把分析渲染成给起草模型的提示（明确标注是推断）。"""
    if not desire:
        return ""
    wants = "、".join(desire.get("what_she_wants") or []) or "未知"
    avoid = "、".join(desire.get("avoid") or []) or "无"
    return "\n".join(
        [
            "【她的潜台词（这是推断，不是已确认事实；证据不足时不要当成真的）】",
            f"- 视频内容：{desire.get('content_summary') or '未知'}",
            f"- 她为什么发：{desire.get('why_she_sent') or '未知'}",
            f"- 她想要的反应：{wants}",
            f"- 情绪需求：{desire.get('emotional_need') or '未知'}",
            f"- 建议做法：{desire.get('suggested_move') or '无'}",
            f"- 要避免：{avoid}",
            f"- 把握程度：{desire.get('confidence')}"
            + ("（不确定）" if desire.get("uncertain") else ""),
            "写作要求：回复要接住**她想要的反应**，不要复述视频内容、不要讲道理。",
        ]
    )


def build_messages(
    msg: IncomingMessage,
    persona: dict[str, Any],
    recent: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    stats_text: str = "",
) -> list[dict[str, str]]:
    from . import prompt as prompt_mod

    system = DESIRE_SYSTEM + "\n\n【角色设定】\n" + prompt_mod._fmt_persona(persona)
    user = (
        f"【对话统计】\n{stats_text or '(无)'}\n\n"
        f"【近期对话】\n{prompt_mod._fmt_recent(recent)}\n\n"
        f"【长期记忆】\n{prompt_mod._fmt_facts(facts)}\n\n"
        f"【当前消息】\n平台：{msg.platform}\n类型：{msg.content_type}\n"
        f"可读文本：{(msg.text or '(无)')[:400]}\n"
        f"媒体内容：{(msg.media_summary or '(无)')[:800]}\n\n"
        "请按要求返回 JSON。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
