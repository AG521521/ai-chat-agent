"""回复限流与熔断。

用户要求：不要刷屏、不要重复回同一个人。
三个闸门：
1. 同一会话两条回复之间至少隔 min_gap_minutes；
2. 每个会话每天最多 daily_limit 条；
3. 同一平台连续失败达到阈值 → 平台级熔断（写 meta: circuit:<platform>）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .store import Store


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def reply_allowed(
    store: Store, settings: dict[str, Any], platform: str, contact_id: str
) -> tuple[bool, str]:
    reply = settings.get("reply", {})
    daily_limit = int(reply.get("daily_limit", 20))
    min_gap_seconds = float(reply.get("min_gap_minutes", 5)) * 60

    if store.meta_get(f"circuit:{platform}") == "1":
        return False, "circuit_open"

    with store.read() as conn:
        rows = conn.execute(
            "SELECT updated_at FROM tasks WHERE platform=? AND contact_id=? AND state='DONE' "
            "ORDER BY updated_at DESC LIMIT 200",
            (platform, contact_id),
        ).fetchall()

    now = datetime.now().astimezone()
    today = 0
    last: datetime | None = None
    for row in rows:
        ts = _parse(row["updated_at"])
        if not ts:
            continue
        if ts.astimezone().date() == now.date():
            today += 1
        if last is None or ts > last:
            last = ts

    if today >= daily_limit:
        return False, f"daily_limit:{today}/{daily_limit}"
    if last is not None:
        gap = (now - last.astimezone()).total_seconds()
        if gap < min_gap_seconds:
            return False, f"min_gap:{int(gap)}s<{int(min_gap_seconds)}s"
    return True, ""


def record_failure(store: Store, settings: dict[str, Any], platform: str) -> bool:
    """记一次失败；达到阈值就熔断该平台。返回是否触发熔断。"""
    threshold = int(settings.get("recovery", {}).get("circuit_breaker_threshold", 3))
    key = f"fail_streak:{platform}"
    streak = int(store.meta_get(key, "0") or 0) + 1
    with store.tx() as cur:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(streak)),
        )
    if streak >= threshold:
        store.set_pause(platform, True, by="circuit_breaker")
        with store.tx() as cur:
            cur.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"circuit:{platform}", "1"),
            )
        return True
    return False


def record_success(store: Store, platform: str) -> None:
    with store.tx() as cur:
        cur.execute(
            "INSERT INTO meta(key, value) VALUES(?, '0') "
            "ON CONFLICT(key) DO UPDATE SET value='0'",
            (f"fail_streak:{platform}",),
        )
