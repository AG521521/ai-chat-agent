"""人工审核队列的读取与卡片渲染（纯展示，决策落库在 store 里）。"""
from __future__ import annotations

from typing import Any

from .store import Store


def pending(store: Store) -> list[dict[str, Any]]:
    return store.pending_approvals()


def render_card(item: dict[str, Any], width: int = 60) -> str:
    """把一条待审任务渲染成可读卡片。日志/终端里不展开完整聊天正文以外的东西。"""
    line = "-" * width
    reasons = "、".join(item.get("reason_codes") or []) or "-"
    text = (item.get("text") or "").replace("\n", " ")
    draft = (item.get("draft_text") or "").replace("\n", " ")
    return "\n".join(
        [
            line,
            f"任务 {item.get('task_id')}  [{item.get('platform')}] {item.get('contact_id')}",
            f"风险 {item.get('risk_level')}   类型 {item.get('content_type')}   状态 {item.get('state')}",
            f"理由 {reasons}",
            f"原消息 {text[:120]}",
            f"草稿   {draft[:120] or '(尚未生成)'}",
            line,
        ]
    )


def approve(store: Store, task_id: str, by: str, note: str = "") -> dict[str, Any]:
    return store.decide_approval(task_id, True, by, note)


def reject(store: Store, task_id: str, by: str, note: str = "") -> dict[str, Any]:
    return store.decide_approval(task_id, False, by, note)
