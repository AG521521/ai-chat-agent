"""单测共享的常量与小工具。"""
from __future__ import annotations

from core.model import Analysis, IncomingMessage, RiskLevel

SETTINGS = {
    "app": {"dry_run": True, "global_paused": False},
    "reply": {
        "mode": "manual",
        "min_confidence": 0.9,
        "max_length": 120,
        "allowlisted_contacts": {"wechat": ["c1"], "douyin": []},
    },
    "risk": {
        "require_human_for": ["R1", "R2", "R3"],
        "always_review_topics": [
            "major_relationship_decisions",
            "money",
            "privacy",
            "major_promises",
            "intense_conflict",
            "crisis",
        ],
    },
    "video": {"require_quote_verification": True, "if_quote_uncertain": "human_review"},
    "memory": {
        "recent_turns": 20,
        "use_long_term": True,
        "isolate_by_contact": True,
        "link_groups": [],
    },
    "recovery": {"max_analysis_retries": 1, "resend_if_status_unknown": False},
}


def msg(**kw) -> IncomingMessage:
    base = {
        "platform": "wechat",
        "contact_id": "c1",
        "contact_verified": True,
        "is_allowlisted": True,
        "content_type": "text",
        "text": "今天课上教了新的动作",
    }
    base.update(kw)
    return IncomingMessage(**base)


def analysis(
    level: str = "R0",
    confidence: float = 0.95,
    uncertain: bool = False,
    requires_human: bool = False,
    source: str = "model",
    media: str = "full",
) -> Analysis:
    return Analysis(
        intent="daily_sharing",
        risk_level=RiskLevel(level),
        confidence=confidence,
        uncertain=uncertain,
        requires_human=requires_human,
        reason_codes=[],
        media_understanding=media,
        source=source,
    )
