"""发送前检查、引用校验、发送结果归一化。

这里刻意把"输入框清空"和"真的发出去了"分开：
清空只是前提，不是证据。抖音侧历史上就因为把清空当成功，
造成过"已发出但没记账 → 下一轮重复发"。
"""
from __future__ import annotations

from typing import Any, Optional

from .model import EvidenceLevel, QuoteCheck, SendResult


def pre_send_blockers(
    task: dict[str, Any], settings: dict[str, Any], auto_quote: bool = False
) -> list[str]:
    """返回阻止发送的原因列表；空列表才允许进入 SENDING。

    auto_quote=True 时允许任务停在 VERIFYING_QUOTE：引用目标由**发送端**
    在点击发送前用引用条 OCR 现场校验（校验不过就不发）。
    """
    blockers: list[str] = []

    state = task.get("state")
    if state != "READY_TO_SEND" and not (auto_quote and state == "VERIFYING_QUOTE"):
        blockers.append(f"state_not_ready_to_send:{state}")
    if not task.get("contact_verified"):
        blockers.append("contact_unverified")
    if not task.get("allowlisted"):
        blockers.append("not_allowlisted")

    draft = (task.get("draft_text") or "").strip()
    max_len = int(settings.get("reply", {}).get("max_length", 120))
    if not draft:
        blockers.append("empty_draft")
    elif len(draft) > max_len:
        blockers.append(f"draft_too_long:{len(draft)}>{max_len}")

    if task.get("requires_quote"):
        if auto_quote:
            if not (task.get("quote_target") or "").strip():
                blockers.append("quote_target_missing")
        else:
            if not task.get("quote_target_verified"):
                blockers.append("quote_unverified")
            quote_cfg = settings.get("video", {})
            if quote_cfg.get("require_quote_verification", True) and not task.get(
                "quote_target_verified"
            ):
                blockers.append("quote_verification_required")

    if settings.get("app", {}).get("dry_run", True):
        blockers.append("dry_run")

    if settings.get("app", {}).get("global_paused", False):
        blockers.append("global_paused")

    flag = task.get("last_error") or ""
    if flag.startswith("circuit_open"):
        blockers.append("circuit_breaker_open")

    return blockers


def verify_quote(
    expected: dict[str, Any],
    observed: dict[str, Any],
    settings: Optional[dict[str, Any]] = None,
) -> QuoteCheck:
    """校验"我要引用的"和"界面里我引用的"是同一条。

    expected: target_id / hash / author / text
    observed: preview_id / hash / author / preview_text / quote_chip
    """
    settings = settings or {}
    reasons: list[str] = []

    exp_id = (expected.get("target_id") or "").strip()
    obs_id = (observed.get("preview_id") or "").strip()
    if exp_id and obs_id and exp_id == obs_id:
        return QuoteCheck(True, "platform_id", ["matched_by_platform_id"])

    exp_hash = (expected.get("hash") or "").strip()
    obs_hash = (observed.get("hash") or "").strip()
    if exp_hash and obs_hash and exp_hash == obs_hash:
        return QuoteCheck(True, "ui_verified", ["matched_by_media_hash"])

    exp_author = (expected.get("author") or "").strip()
    obs_author = (observed.get("author") or "").strip()
    if exp_hash and obs_hash and exp_hash != obs_hash:
        reasons.append("hash_mismatch")

    if observed.get("quote_chip"):
        preview = (observed.get("preview_text") or "").replace("\n", "")
        probe = (expected.get("text") or "").replace("\n", "")[:12]
        if probe and probe in preview:
            if exp_author and obs_author and exp_author != obs_author:
                return QuoteCheck(False, "unverified", reasons + ["author_mismatch"])
            return QuoteCheck(True, "ui_verified", reasons + ["matched_by_quote_chip_text"])
        reasons.append("quote_chip_text_mismatch")

    if not reasons:
        reasons.append("no_readable_quote_evidence")
    return QuoteCheck(False, "unverified", reasons)


def classify_send(raw: dict[str, Any]) -> SendResult:
    """把适配器的原始反馈归一化成 verified / unknown / failed。

    raw 字段（全部可选）：
      error: str              —— 明确失败原因
      input_cleared: bool     —— 回车后输入框是否已空
      bubble_seen: bool       —— 会话里是否出现这条消息气泡
      quote_seen: bool        —— 气泡是否带引用条
      preview_seen: bool      —— 会话列表预览是否更新为该文本
      text_echo: bool         —— 页面里能否读到回复文本
      screenshot: str
    """
    if raw.get("error"):
        return SendResult("failed", EvidenceLevel.UI_OBSERVED, str(raw["error"])[:300])

    screenshot = raw.get("screenshot")
    input_cleared = bool(raw.get("input_cleared"))
    bubble = bool(raw.get("bubble_seen"))
    quote = bool(raw.get("quote_seen"))
    preview = bool(raw.get("preview_seen"))
    echo = bool(raw.get("text_echo"))

    if bubble and (quote or echo):
        return SendResult(
            "verified",
            EvidenceLevel.UI_VERIFIED,
            "bubble_seen" + ("+quote" if quote else "+text"),
            screenshot,
        )
    if preview and input_cleared:
        return SendResult(
            "verified", EvidenceLevel.UI_VERIFIED, "preview_updated+input_cleared", screenshot
        )
    if bubble:
        return SendResult("verified", EvidenceLevel.UI_VERIFIED, "bubble_seen", screenshot)
    if input_cleared:
        # 关键：输入框空了不等于发出去了
        return SendResult(
            "unknown",
            EvidenceLevel.UI_OBSERVED,
            "input_cleared_only: 需要人工复核会话，禁止直接重发",
            screenshot,
        )
    return SendResult("failed", EvidenceLevel.UI_OBSERVED, "editor still holds text", screenshot)
