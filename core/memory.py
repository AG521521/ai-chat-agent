"""记忆：近期上下文与长期事实，默认按联系人严格隔离。

隔离规则（执行规范 1.4 / 6.1）：
* 默认命名空间是 ``platform:contact``，两个平台各自独立；
* 只有显式配置 ``memory.link_groups`` 里声明了同一组，才允许共享；
* 共享是**配置行为**，不是模型推断行为——推断出来的"可能是同一个人"
  绝不能自动合并记忆。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .model import IncomingMessage
from .naming import matches_alias
from .store import Store
from . import stats as stats_mod


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


class Memory:
    def __init__(self, store: Store, settings: dict[str, Any]):
        self.store = store
        self.settings = settings

    # ---------------------------------------------------------- 命名空间
    def namespace(self, platform: str, contact_id: str) -> str:
        """联系人 → 命名空间。

        成员名支持切词匹配：备注从「小A」改成「小A（白名单）」后，
        仍然落在同一个 ``linked:xxx`` 命名空间里，记忆不会断。
        """
        groups = self.settings.get("memory", {}).get("link_groups", []) or []
        for group in groups:
            for member in group.get("members") or []:
                try:
                    m_platform, alias = list(member)[0], list(member)[1]
                except (IndexError, TypeError):
                    continue
                if m_platform != platform:
                    continue
                if matches_alias(str(alias), contact_id):
                    return f"linked:{group.get('id', 'group')}"
        return f"{platform}:{contact_id}"

    def recent_limit(self) -> int:
        return int(
            self.settings.get("memory", {}).get(
                "recent_turns",
                self.settings.get("reply", {}).get("recent_turns", 20),
            )
        )

    # ---------------------------------------------------------- 写入
    def record_incoming(self, msg: IncomingMessage) -> None:
        text = (msg.text or msg.media_summary or "").strip()
        if not text:
            return
        self.store.add_turn(
            self.namespace(msg.platform, msg.contact_id),
            msg.platform,
            msg.contact_id,
            role="contact",
            text=text[:500],
            source="observed",
            confidence=1.0 if msg.evidence_level.value != "inferred" else 0.6,
        )

    def record_outgoing(self, platform: str, contact_id: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.store.add_turn(
            self.namespace(platform, contact_id),
            platform,
            contact_id,
            role="self",
            text=text[:500],
            source="sent",
            confidence=1.0,
        )

    def remember_fact(
        self,
        platform: str,
        contact_id: str,
        text: str,
        source: str = "user",
        confidence: float = 1.0,
    ) -> None:
        self.store.add_fact(
            self.namespace(platform, contact_id),
            platform,
            contact_id,
            text.strip(),
            source=source,
            confidence=confidence,
        )

    # ---------------------------------------------------------- 读取
    def recent(self, platform: str, contact_id: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
        return self.store.recent_turns(
            self.namespace(platform, contact_id),
            limit or self.recent_limit(),
        )

    def facts(self, platform: str, contact_id: str, keyword: str = "") -> list[dict[str, Any]]:
        if not self.settings.get("memory", {}).get("use_long_term", True):
            return []
        return self.store.facts(self.namespace(platform, contact_id), keyword=keyword)

    def context(
        self,
        platform: str,
        contact_id: str,
        recent_limit: Optional[int] = None,
        older_limit: int = 6,
    ) -> dict[str, Any]:
        """拼装给模型用的上下文：确定性统计 + 分层采样 + 长期事实。"""
        ns = self.namespace(platform, contact_id)
        turns = self.store.all_turns(ns)
        stats = stats_mod.compute_stats(turns, ns)
        sample = stats_mod.stratified_sample(
            turns, recent_limit or self.recent_limit(), older_limit
        )
        return {
            "namespace": ns,
            "stats": stats,
            "turns": sample,
            "all_turn_count": len(turns),
            "facts": self.facts(platform, contact_id),
        }

    def clear(self, platform: str, contact_id: str) -> int:
        return self.store.clear_memory(platform, contact_id)
