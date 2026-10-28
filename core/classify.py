"""模型调用 + 结构化输出校验。

硬规则（执行规范 4 / 6.2）：
* 只接受严格 JSON；
* 缺字段、类型不对、风险值非法、超时、网络错误 → 一律 ``ClassifyError``；
* 调用方必须把 ``ClassifyError`` 变成"人工审核"，**绝不能变成低风险**。
* 模型自报的 risk_level 只是输入之一，硬拦截由 risk.py 独立执行。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import prompt as prompt_mod
from .model import Analysis, IncomingMessage, RiskLevel

Transport = Callable[[dict[str, Any], dict[str, Any]], str]

VALID_RISK = {"R0", "R1", "R2", "R3"}
VALID_MEDIA = {"full", "partial", "unavailable"}


class ClassifyError(RuntimeError):
    pass


def load_api_key(settings: dict[str, Any], base_dir: Path) -> str:
    llm = settings.get("llm", {})
    env_name = llm.get("api_key_env", "DEEPSEEK_API_KEY")
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    rel = llm.get("api_key_file", "")
    if rel:
        p = Path(rel)
        if not p.is_absolute():
            p = base_dir / p
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ClassifyError(f"密钥文件不可读: {exc}") from exc
            key = (data.get("api_key") or data.get("apiKey") or "").strip()
            if key:
                return key
    return ""


def _http_transport(payload: dict[str, Any], settings: dict[str, Any]) -> str:
    llm = settings.get("llm", {})
    base = llm.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
    timeout = float(llm.get("timeout_seconds", 30))
    key = payload.pop("_api_key", "")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise ClassifyError(f"HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ClassifyError(f"网络错误: {exc}") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ClassifyError("模型响应结构异常") from exc


def parse_analysis(content: str) -> Analysis:
    """把模型输出解析成 Analysis；任何不合规都抛错。"""
    if not content or not content.strip():
        raise ClassifyError("模型返回空内容")

    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ClassifyError("模型输出里没有 JSON 对象")
    blob = text[start : end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ClassifyError(f"JSON 解析失败: {exc}") from exc

    required = {
        "risk_level",
        "confidence",
        "uncertain",
        "requires_human",
        "reason_codes",
        "media_understanding",
    }
    missing = required - set(data)
    if missing:
        raise ClassifyError(f"缺少字段: {sorted(missing)}")

    risk = str(data["risk_level"]).strip().upper()
    if risk not in VALID_RISK:
        raise ClassifyError(f"非法 risk_level: {data['risk_level']}")

    try:
        confidence = float(data["confidence"])
    except (TypeError, ValueError) as exc:
        raise ClassifyError("confidence 不是数字") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ClassifyError(f"confidence 越界: {confidence}")

    if not isinstance(data["uncertain"], bool):
        raise ClassifyError("uncertain 必须是布尔")
    if not isinstance(data["requires_human"], bool):
        raise ClassifyError("requires_human 必须是布尔")

    codes = data["reason_codes"]
    if not isinstance(codes, list) or not all(isinstance(x, str) for x in codes):
        raise ClassifyError("reason_codes 必须是字符串数组")

    media = str(data["media_understanding"]).strip().lower()
    if media not in VALID_MEDIA:
        raise ClassifyError(f"非法 media_understanding: {media}")

    reply_text = str(data.get("reply_text", "") or "").strip()

    return Analysis(
        intent=str(data.get("intent", "unknown"))[:64],
        risk_level=RiskLevel(risk),
        confidence=confidence,
        uncertain=bool(data["uncertain"]),
        reason_codes=codes,
        requires_human=bool(data["requires_human"]),
        media_understanding=media,
        suggested_reply_strategy=str(data.get("suggested_reply_strategy", "warm_conversational"))[:48],
        raw={"reply_text": reply_text},
        source="model",
    )


class Classifier:
    def __init__(
        self,
        settings: dict[str, Any],
        base_dir: Path,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.base_dir = Path(base_dir)
        self.transport = transport or _http_transport
        self.sleep = sleep
        self.calls = 0

    def _messages_payload(self, messages: list[dict[str, str]], json_mode: bool = False) -> dict[str, Any]:
        llm = self.settings.get("llm", {})
        payload: dict[str, Any] = {
            "model": llm.get("model", "deepseek-chat"),
            "temperature": float(llm.get("temperature", 0.8)),
            "max_tokens": int(llm.get("max_tokens", 400)),
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _call(
        self,
        messages: list[dict[str, str]],
        parse: Callable[[str], Any],
        json_mode: bool = False,
    ) -> Any:
        """带退避重试的单次模型调用；解析失败也算失败，会重试。"""
        payload = self._messages_payload(messages, json_mode=json_mode)
        if self.transport is _http_transport:
            api_key = load_api_key(self.settings, self.base_dir)
            if not api_key:
                raise ClassifyError("缺少 API key（环境变量或 config/secrets.local.json）")
            payload["_api_key"] = api_key

        retries = int(self.settings.get("recovery", {}).get("max_analysis_retries", 2))
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self.calls += 1
                content = self.transport(dict(payload), self.settings)
                return parse(content)
            except (ClassifyError, ValueError, KeyError, TypeError) as exc:
                last = exc if isinstance(exc, ClassifyError) else ClassifyError(str(exc))
                if attempt < retries:
                    self.sleep(1.0 + attempt * 1.0)
        raise ClassifyError(str(last) if last else "未知模型错误")

    # ---------------------------------------------------------- 单次：判断+起草合一（离线/兼容）
    def analyze(
        self,
        msg: IncomingMessage,
        persona: dict[str, Any],
        recent: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        extra_rules: str = "",
        stats_text: str = "",
    ) -> Analysis:
        messages = prompt_mod.build_messages(
            persona, msg, recent, facts, extra_rules=extra_rules, stats_text=stats_text
        )
        return self._call(messages, parse_analysis, json_mode=True)

    # ---------------------------------------------------------- 两次：先判断，再起草
    def judge(
        self,
        msg: IncomingMessage,
        persona: dict[str, Any],
        recent: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        stats_text: str = "",
    ) -> Analysis:
        messages = prompt_mod.build_judge_messages(persona, msg, recent, facts, stats_text)
        return self._call(messages, parse_analysis, json_mode=True)

    def draft(
        self,
        msg: IncomingMessage,
        analysis: Analysis,
        persona: dict[str, Any],
        recent: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        stats_text: str = "",
        desire_text: str = "",
    ) -> str:
        messages = prompt_mod.build_draft_messages(
            persona, msg, analysis, recent, facts, stats_text, desire_text=desire_text
        )
        return self._call(messages, parse_draft)

    def desire(
        self,
        msg: IncomingMessage,
        persona: dict[str, Any],
        recent: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        stats_text: str = "",
    ) -> dict[str, Any]:
        """「她想要什么」分析（只对媒体消息调用）。"""
        from . import desire as desire_mod

        messages = desire_mod.build_messages(msg, persona, recent, facts, stats_text)
        return self._call(messages, desire_mod.parse_desire, json_mode=True)

    def draft_simple(
        self,
        msg: IncomingMessage,
        persona: dict[str, Any],
        recent: list[dict[str, Any]],
        notes_text: str = "",
        max_chars: int = 20,
    ) -> str:
        """文字消息的极简回复：一句话，照着她的话回。"""
        messages = prompt_mod.build_simple_messages(persona, msg, recent, notes_text)
        text = self._call(messages, parse_draft)
        text = text.strip().splitlines()[0].strip() if text.strip() else ""
        return text[:max_chars]

    def profile(self, stats: dict[str, Any], pairs: list[dict[str, Any]]) -> dict[str, Any]:
        """关系画像（离线分析历史聊天）。"""
        from . import profile as profile_mod

        messages = profile_mod.build_messages(stats, pairs)
        return self._call(messages, profile_mod.parse_notes, json_mode=True)


def parse_draft(content: str) -> str:
    """起草结果归一化：允许纯文本，也允许 {"reply_text": ...}。"""
    if not content or not content.strip():
        raise ClassifyError("起草返回空内容")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict) and data.get("reply_text"):
                    text = str(data["reply_text"]).strip()
            except json.JSONDecodeError:
                pass
    text = text.strip().strip('"').strip("“”").strip()
    if not text:
        raise ClassifyError("起草结果为空")
    return text[:400]
