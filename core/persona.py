"""人设：单一来源的配置加载与校验。

人设只放这里，业务代码里不再写死提示词。
支持按 mtime 热加载，改完文件不必重启进程。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "persona": {
        "id": "relationship_assistant_v1",
        "display_style": "温柔、自然、口语化、简洁",
        "relationship_context": "仅使用用户明确提供且允许使用的背景",
        "tone": [
            "先回应情绪，再考虑是否解决问题",
            "表达自然，不像客服或问答机器人",
            "避免反复使用固定安慰句式",
            "允许适当追问，但不要连续盘问",
        ],
        "boundaries": [
            "不虚构自己亲历的事件",
            "不擅自做重大承诺",
            "不替用户决定重大关系事项",
            "不根据模糊图片或视频编造事实",
            "高风险话题交由真人确认",
        ],
        "output_rules": [
            "只输出回复正文，不要解释、不要引号、不要多条候选",
            "一到两句，口语，长度不超过配置上限",
            "抓得住对方消息里的具体细节，不要只说“哈哈”“好看”",
            "看不清楚就不编，宁可换角度关心对方本人",
        ],
        "memory": {
            "recent_turns": 20,
            "use_long_term_memory": True,
            "isolate_by_contact": True,
        },
    }
}

REQUIRED = ("id", "display_style", "tone", "boundaries")


class PersonaError(ValueError):
    pass


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class PersonaStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._cache: dict[str, Any] | None = None
        self._mtime: float = -1.0

    def load(self) -> dict[str, Any]:
        """读取人设；config/persona.local.json 会覆盖 persona.json（私有关系背景）。"""
        local = self.path.with_name("persona.local.json")
        sources = [p for p in (self.path, local) if p.exists()]
        if not sources:
            if self._cache is None:
                self._cache = copy.deepcopy(DEFAULTS)
            return self._cache

        mtime = max(p.stat().st_mtime for p in sources)
        if self._cache is not None and mtime == self._mtime:
            return self._cache

        merged: dict[str, Any] = {}
        for src in sources:
            try:
                raw = json.loads(src.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError) as exc:
                raise PersonaError(f"人设文件解析失败({src.name}): {exc}") from exc
            if not isinstance(raw, dict):
                raise PersonaError(f"{src.name} 顶层必须是对象")
            merged = _merge(merged, raw)

        persona = merged.get("persona", merged)
        if not isinstance(persona, dict):
            raise PersonaError("人设文件顶层必须是对象")
        missing = [k for k in REQUIRED if k not in persona]
        if missing:
            raise PersonaError(f"人设缺少字段: {missing}")

        self._cache = _merge(DEFAULTS, {"persona": persona})
        self._mtime = mtime
        return self._cache

    @property
    def persona(self) -> dict[str, Any]:
        return self.load()["persona"]
