"""路由：把「消息 + 分析 + 风险 + 配置」映射到一个状态。

严格按执行规范第 3 节的伪代码实现，顺序不能乱：
暂停 → 联系人不确定 → 白名单 → 重复 → 界面不确定 → R3 → R2 → 低置信 → R1 →
引用未验证 → 人工模式 → 准备回复。
"""
from __future__ import annotations

from typing import Any, Optional

from .model import (
    Analysis,
    Decision,
    IncomingMessage,
    RiskAssessment,
    RiskLevel,
    State,
)


def route(
    msg: IncomingMessage,
    analysis: Analysis,
    assessment: RiskAssessment,
    settings: dict[str, Any],
    *,
    paused: bool = False,
    duplicate_of: Optional[str] = None,
) -> Decision:
    reasons: list[str] = []

    if paused:
        return Decision(State.PAUSED, ["paused"], assessment.level, True, next_action="wait")

    flags = set(assessment.reasons)
    if "system:contact_unverified" in flags:
        return Decision(
            State.ERROR_HOLD, ["contact_unverified"], RiskLevel.R3, True, next_action="human"
        )
    if "system:ui_state_uncertain" in flags:
        return Decision(
            State.ERROR_HOLD, ["ui_state_uncertain"], RiskLevel.R3, True, next_action="human"
        )

    if not msg.is_allowlisted:
        return Decision(State.CANCELLED, ["not_allowlisted"], assessment.level, False,
                        next_action="drop")

    # 自动模式下文字消息不回（手动模式仍然出稿，方便人工参考）
    mode_cfg = str(settings.get("reply", {}).get("mode", "manual")).lower()
    if (
        settings.get("reply", {}).get("only_media", False)
        and msg.content_type == "text"
        and mode_cfg != "manual"
    ):
        return Decision(State.CANCELLED, ["text_skip"] + assessment.reasons[:2],
                        assessment.level, False, next_action="drop")

    if duplicate_of:
        return Decision(State.CANCELLED, ["duplicate", duplicate_of], assessment.level, False,
                        next_action="drop")

    if assessment.level is RiskLevel.R3:
        return Decision(State.ERROR_HOLD, assessment.reasons or ["R3"], RiskLevel.R3, True,
                        next_action="human")

    if msg.requires_quote and not msg.quote_target_verified:
        return Decision(
            State.VERIFYING_QUOTE,
            ["quote_unverified"] + assessment.reasons,
            assessment.level,
            True,
            next_action="verify_quote",
        )

    if assessment.level is RiskLevel.R2:
        return Decision(State.WAITING_APPROVAL, assessment.reasons or ["R2"], RiskLevel.R2, True,
                        next_action="approval")

    # R1：默认人工；自动模式下如果 R1 只是"媒体没看全/置信度不足"，
    # 且没有任何话题命中（金钱/隐私/冲突…），允许自动出稿。
    mode_cfg = str(settings.get("reply", {}).get("mode", "manual")).lower()
    auto_max = str(settings.get("reply", {}).get("auto_max_risk", "R0")).upper()
    topic_blocked = any(r.startswith("topic:") for r in assessment.reasons)

    if assessment.level is RiskLevel.R1:
        if not (
            mode_cfg != "manual"
            and auto_max == "R1"
            and not topic_blocked
            and not analysis.uncertain
        ):
            return Decision(State.WAITING_APPROVAL, assessment.reasons or ["R1"],
                            RiskLevel.R1, True, next_action="approval")

    mode = str(settings.get("reply", {}).get("mode", "manual")).lower()
    dry_run = bool(settings.get("app", {}).get("dry_run", True))

    if mode == "manual" or dry_run:
        return Decision(
            State.WAITING_APPROVAL,
            ["manual_mode" if mode == "manual" else "dry_run"] + assessment.reasons[:3],
            assessment.level,
            True,
            next_action="approval",
        )

    return Decision(State.PREPARING_REPLY, assessment.reasons, assessment.level,
                    assessment.requires_human, next_action="prepare")
