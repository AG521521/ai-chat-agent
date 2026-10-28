"""风险判定：规则先行，模型只做辅助。

硬性约定（执行规范第 4 节）：
1. 先跑硬规则（联系人/白名单/登录/引用/重复/发送状态）；
2. 再取模型判断；
3. 两者冲突时**取更谨慎**；
4. 置信度不足 → 人工；
5. 模型不能覆盖任何硬性拦截。
"""
from __future__ import annotations

import re
from typing import Any

from .model import (
    Analysis,
    IncomingMessage,
    RiskAssessment,
    RiskLevel,
    max_risk,
)


def _kw(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# 话题规则：命中即抬到对应等级，并记录 reason code
TOPIC_RULES: list[tuple[str, RiskLevel, re.Pattern[str]]] = [
    (
        "crisis",
        RiskLevel.R2,
        _kw(r"自杀|不想活|想死|自残|活着没意思|绝望|抑郁|撑不下去"),
    ),
    (
        "money",
        RiskLevel.R2,
        _kw(
            r"转账|借钱|还钱|打钱|汇款|付款|代付|收红包|发红包|退款|银行卡|支付宝|"
            r"微信支付|花呗|借呗|贷款|余额|转账记录|多少钱|报价|买单|垫付"
        ),
    ),
    (
        "privacy",
        RiskLevel.R2,
        _kw(
            r"身份证|密码|验证码|住址|家庭地址|学校地址|门牌|定位|开房|裸|私密照|"
            r"床照|聊天记录截图|手机号|身份证号|银行卡号|工资|薪水"
        ),
    ),
    (
        "major_relationship_decisions",
        RiskLevel.R2,
        _kw(r"分手|离婚|订婚|结婚|见家长|搬出来|同居|分开吧|结束吧|别联系了|断了吧|冷静一下"),
    ),
    (
        "major_promises",
        RiskLevel.R2,
        _kw(r"我保证|我发誓|永远|一辈子|一定娶|彩礼|买房|买车|辞职|退学|包在我身上"),
    ),
    (
        "intense_conflict",
        RiskLevel.R2,
        _kw(r"滚|闭嘴|受够了|拉黑|删了你|不理我|别理我|吵|凶|吼|你骗我|发错|给谁|到底|凭什么"),
    ),
]

# 含糊/低信息量回复：上下文不足，默认人工
AMBIGUOUS_RE = re.compile(
    r"^(嗯+|哦+|噢+|啊+|好+|行+|随便|都行|都可以|没事|算了|呵呵|哈+|呃+|"
    r"你自己看|不知道|无所谓|看吧|你说了算|？+|\?+)[。.!！~～]*$"
)

# 系统级风险码：命中即禁止自动发送
SYSTEM_FLAGS: dict[str, str] = {
    "contact_unverified": "联系人身份未确认",
    "ui_state_uncertain": "界面状态不确定",
}


def topic_hits(text: str) -> list[tuple[str, RiskLevel]]:
    hits: list[tuple[str, RiskLevel]] = []
    if not text:
        return hits
    for name, level, pat in TOPIC_RULES:
        if pat.search(text):
            hits.append((name, level))
    return hits


def system_flags(msg: IncomingMessage) -> list[str]:
    """只包含"必须停下来"的系统风险；引用未验证走 VERIFYING_QUOTE 单独处理。"""
    flags: list[str] = []
    if not msg.contact_verified:
        flags.append("contact_unverified")
    if msg.ui_state_uncertain:
        flags.append("ui_state_uncertain")
    return flags


def assess(
    msg: IncomingMessage,
    analysis: Analysis,
    settings: dict[str, Any],
    recent_count: int = 0,
    use_model: bool = True,
) -> RiskAssessment:
    """合并规则与模型，得到最终风险等级。

    use_model=False 用于极简文字回复：只看硬规则（话题/界面/联系人），
    不采信模型对"你想我了吗"这类话的过度紧张判断。
    """
    reasons: list[str] = []
    topics: list[str] = []

    rule_level = RiskLevel.R0
    for name, level in topic_hits(f"{msg.text}\n{msg.media_summary or ''}"):
        topics.append(name)
        reasons.append(f"topic:{name}")
        rule_level = max_risk(rule_level, level)

    text = (msg.text or "").strip()
    if AMBIGUOUS_RE.match(text):
        reasons.append("ambiguous_short")
        rule_level = max_risk(rule_level, RiskLevel.R1)

    if msg.content_type in ("video", "image", "audio", "mixed"):
        if analysis.media_understanding != "full":
            reasons.append("media_partial")
            rule_level = max_risk(rule_level, RiskLevel.R1)

    if recent_count == 0 and len(text) < 6:
        reasons.append("low_context")
        rule_level = max_risk(rule_level, RiskLevel.R1)

    model_level = analysis.risk_level if use_model else RiskLevel.R0
    level = max_risk(rule_level, model_level)
    if use_model and model_level != rule_level:
        reasons.append(f"merged:rules={rule_level.value},model={model_level.value}")

    min_conf = float(settings.get("reply", {}).get("min_confidence", 0.9))
    if use_model and (analysis.uncertain or analysis.confidence < min_conf):
        reasons.append("low_confidence")
        level = max_risk(level, RiskLevel.R1)

    if analysis.source == "fallback":
        reasons.append("model_fallback")
        level = max_risk(level, RiskLevel.R1)

    flags = system_flags(msg)
    if flags:
        for f in flags:
            reasons.append(f"system:{f}")
        level = RiskLevel.R3

    require_human_for = {
        str(x).upper() for x in settings.get("risk", {}).get("require_human_for", ["R1", "R2", "R3"])
    }
    always_review = set(settings.get("risk", {}).get("always_review_topics", []))
    requires_human = (
        level.value in require_human_for
        or bool(always_review & set(topics))
        or analysis.requires_human
        or bool(flags)
        or level is not RiskLevel.R0
    )

    return RiskAssessment(
        level=level,
        reasons=reasons,
        requires_human=requires_human,
        topics=topics,
    )
