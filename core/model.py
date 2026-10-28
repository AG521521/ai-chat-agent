"""统一消息模型与决策对象（平台无关）。

这一层只定义数据契约，不做任何 I/O、不调用模型、不碰平台 UI。
两个适配器（微信 / 抖音）把各自的界面观测结果映射成这里的
``IncomingMessage``，核心再把 ``Decision`` / ``SendResult`` 交还回去。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional

Platform = Literal["wechat", "douyin"]
MessageType = Literal["text", "image", "video", "audio", "mixed", "unknown"]

PLATFORMS = ("wechat", "douyin")
MESSAGE_TYPES = ("text", "image", "video", "audio", "mixed", "unknown")


def now_iso() -> str:
    """本地时区的 ISO 时间，带偏移量，便于跨平台对齐。"""
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def new_task_id() -> str:
    return uuid.uuid4().hex


class State(str, Enum):
    """任务状态机（与执行规范第 3 节一致）。"""

    OBSERVED = "OBSERVED"
    VALIDATING = "VALIDATING"
    ANALYZING = "ANALYZING"
    DECIDING = "DECIDING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PREPARING_REPLY = "PREPARING_REPLY"
    VERIFYING_QUOTE = "VERIFYING_QUOTE"
    READY_TO_SEND = "READY_TO_SEND"
    SENDING = "SENDING"
    VERIFYING_RESULT = "VERIFYING_RESULT"
    DONE = "DONE"
    # 异常态
    PAUSED = "PAUSED"
    ERROR_HOLD = "ERROR_HOLD"
    SEND_UNKNOWN = "SEND_UNKNOWN"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {State.DONE, State.FAILED_CONFIRMED, State.CANCELLED}


class RiskLevel(str, Enum):
    R0 = "R0"  # 普通聊天
    R1 = "R1"  # 谨慎处理
    R2 = "R2"  # 高风险
    R3 = "R3"  # 系统风险


RISK_ORDER = {RiskLevel.R0: 0, RiskLevel.R1: 1, RiskLevel.R2: 2, RiskLevel.R3: 3}
RISK_LEVELS = tuple(RISK_ORDER)


def max_risk(*levels: RiskLevel | str | None) -> RiskLevel:
    """取更谨慎的那一个（规则与模型冲突时的合并方式）。"""
    worst = RiskLevel.R0
    for lv in levels:
        if lv is None:
            continue
        try:
            cur = lv if isinstance(lv, RiskLevel) else RiskLevel(str(lv).upper())
        except ValueError:
            # 非法风险值一律按最高处理和人工介入
            return RiskLevel.R3
        if RISK_ORDER[cur] > RISK_ORDER[worst]:
            worst = cur
    return worst


class EvidenceLevel(str, Enum):
    """证据等级，决定了"我们能声称什么"。"""

    PLATFORM_ID = "platform_id"      # 拿到平台原生消息 ID
    UI_VERIFIED = "ui_verified"      # 界面复核过（截图/气泡/预览）
    UI_OBSERVED = "ui_observed"      # 只是看到了界面内容
    INFERRED = "inferred"            # 推断关联，未经确认


@dataclass
class IncomingMessage:
    """一条收到的消息（平台无关视图）。"""

    platform: Platform
    contact_id: str
    content_type: MessageType = "text"
    text: str = ""
    observed_at: Optional[str] = None
    source_message_id: Optional[str] = None
    media_path: Optional[str] = None
    media_summary: Optional[str] = None
    media_sig: Optional[str] = None
    quote_target: Optional[str] = None
    quote_target_verified: bool = False
    contact_verified: bool = False
    is_allowlisted: bool = False
    ui_state_uncertain: bool = False
    requires_quote: bool = False
    evidence_level: EvidenceLevel = EvidenceLevel.UI_OBSERVED
    task_id: Optional[str] = None
    raw_ref: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observed_at is None:
            self.observed_at = now_iso()
        if not self.task_id:
            self.task_id = new_task_id()
        if isinstance(self.evidence_level, str):
            self.evidence_level = EvidenceLevel(self.evidence_level)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "task_id": self.task_id,
            "platform": self.platform,
            "contact_id": self.contact_id,
            "content_type": self.content_type,
            "text": self.text,
            "observed_at": self.observed_at,
            "source_message_id": self.source_message_id,
            "media_path": self.media_path,
            "media_summary": self.media_summary,
            "media_sig": self.media_sig,
            "quote_target": self.quote_target,
            "quote_target_verified": self.quote_target_verified,
            "contact_verified": self.contact_verified,
            "is_allowlisted": self.is_allowlisted,
            "ui_state_uncertain": self.ui_state_uncertain,
            "requires_quote": self.requires_quote,
            "evidence_level": self.evidence_level.value,
            "raw_ref": self.raw_ref,
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncomingMessage":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Analysis:
    """模型侧的结构化判断（必须经过 schema 校验）。"""

    intent: str = "unknown"
    risk_level: RiskLevel = RiskLevel.R1
    confidence: float = 0.0
    uncertain: bool = True
    reason_codes: list[str] = field(default_factory=list)
    requires_human: bool = True
    media_understanding: str = "unavailable"  # full / partial / unavailable
    suggested_reply_strategy: str = "warm_conversational"
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = "model"  # model / fallback

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "risk_level": self.risk_level.value,
            "confidence": round(float(self.confidence), 3),
            "uncertain": bool(self.uncertain),
            "reason_codes": list(self.reason_codes),
            "requires_human": bool(self.requires_human),
            "media_understanding": self.media_understanding,
            "suggested_reply_strategy": self.suggested_reply_strategy,
            "source": self.source,
        }

    @classmethod
    def fallback(cls, reason: str) -> "Analysis":
        """模型不可用 / 输出非法时的兜底：绝不默认低风险。"""
        return cls(
            intent="unknown",
            risk_level=RiskLevel.R1,
            confidence=0.0,
            uncertain=True,
            reason_codes=[reason],
            requires_human=True,
            media_understanding="unavailable",
            source="fallback",
        )


@dataclass
class RiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    requires_human: bool = True
    topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "reasons": list(self.reasons),
            "requires_human": bool(self.requires_human),
            "topics": list(self.topics),
        }


@dataclass
class QuoteCheck:
    ok: bool
    level: str  # platform_id / ui_verified / unverified
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "level": self.level, "reasons": list(self.reasons)}


@dataclass
class SendResult:
    """发送结果。verified 才算成功，其余都要人工/复核。"""

    status: str  # verified / unknown / failed
    evidence: EvidenceLevel = EvidenceLevel.UI_OBSERVED
    detail: str = ""
    screenshot: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "evidence": self.evidence.value,
            "detail": self.detail,
            "screenshot": self.screenshot,
        }


@dataclass
class Decision:
    state: State
    reasons: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.R1
    requires_human: bool = True
    draft_text: str = ""
    next_action: str = "hold"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reasons": list(self.reasons),
            "risk_level": self.risk_level.value,
            "requires_human": bool(self.requires_human),
            "draft_text": self.draft_text,
            "next_action": self.next_action,
        }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
