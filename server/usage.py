import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("vault.usage")

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def get_access_token(auth: Dict[str, Any]) -> Optional[str]:
    tokens = auth.get("tokens") or {}
    return (
        auth.get("access_token")
        or auth.get("accessToken")
        or tokens.get("access_token")
        or tokens.get("accessToken")
    )


def get_chatgpt_account_id(auth: Dict[str, Any]) -> Optional[str]:
    tokens = auth.get("tokens") or {}
    return (
        auth.get("account_id")
        or auth.get("accountId")
        or auth.get("chatgpt_account_id")
        or auth.get("chatgptAccountId")
        or tokens.get("account_id")
        or tokens.get("accountId")
    )


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _iso_from_epoch(value: Any) -> Optional[str]:
    timestamp = _number(value)
    if timestamp is None or timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _iso_from_reset_after(value: Any) -> Optional[str]:
    seconds = _number(value)
    if seconds is None or seconds < 0:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _normalize_window(window: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(window, dict):
        return None

    percent_left = _number(window.get("percent_left"))
    if percent_left is None:
        percent_left = _number(window.get("remaining_percent"))
    if percent_left is None:
        used_percent = _number(window.get("used_percent"))
        if used_percent is not None:
            percent_left = 100 - used_percent
    if percent_left is not None:
        percent_left = max(0, min(100, percent_left))

    reset_at = (
        _iso_from_epoch(window.get("reset_time_ms"))
        or _iso_from_epoch(window.get("reset_at"))
        or _iso_from_epoch(window.get("resetsAt"))
        or _iso_from_reset_after(window.get("reset_after_seconds"))
    )

    return {
        "percent_left": percent_left,
        "reset_at": reset_at,
        "window_seconds": _number(window.get("limit_window_seconds")),
    }


def _window_summary(prefix: str, window: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not window:
        return {}
    return {
        f"{prefix}_percent_left": window.get("percent_left"),
        f"{prefix}_reset_at": window.get("reset_at"),
    }


def normalize_usage_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    rate_limit = payload.get("rate_limit") or payload.get("rate_limits") or {}

    candidates = []
    for source, raw in (
        ("five_hour", payload.get("five_hour") or rate_limit.get("five_hour")),
        ("weekly", payload.get("weekly") or rate_limit.get("weekly")),
        ("primary", rate_limit.get("primary_window") or payload.get("primary_window")),
        ("secondary", rate_limit.get("secondary_window") or payload.get("secondary_window")),
    ):
        window = _normalize_window(raw)
        if window:
            candidates.append((source, window))

    five_hour = None
    weekly = None
    primary = None
    secondary = None
    for source, window in candidates:
        seconds = window.get("window_seconds")
        if source == "five_hour" or seconds == 18_000:
            five_hour = window
        elif source == "weekly" or seconds == 604_800:
            weekly = window
        elif source == "primary":
            primary = window
        elif source == "secondary":
            secondary = window

    five_hour = five_hour or primary
    weekly = weekly or secondary

    percentages = [
        value
        for value in (
            five_hour.get("percent_left") if five_hour else None,
            weekly.get("percent_left") if weekly else None,
        )
        if value is not None
    ]
    effective_percent_left = min(percentages) if percentages else None

    updates: Dict[str, Any] = {
        "usage_source": "wham_usage",
        "usage_updated_at": datetime.now(timezone.utc).isoformat(),
        "usage_error": None,
        "plan_type": payload.get("plan_type"),
        "rate_limit_reached": bool(rate_limit.get("limit_reached") or payload.get("limit_reached")),
    }
    updates.update(_window_summary("five_hour", five_hour))
    updates.update(_window_summary("weekly", weekly))

    if effective_percent_left is not None:
        updates["limit_requests"] = 100
        updates["remaining_requests"] = int(effective_percent_left)

    reset_parts = []
    if five_hour and five_hour.get("reset_at"):
        reset_parts.append(f"5h {five_hour['reset_at']}")
    if weekly and weekly.get("reset_at"):
        reset_parts.append(f"weekly {weekly['reset_at']}")
    if reset_parts:
        updates["reset_requests"] = " | ".join(reset_parts)

    return updates


async def fetch_usage_updates(auth: Dict[str, Any]) -> Dict[str, Any]:
    access_token = get_access_token(auth)
    if not access_token:
        raise ValueError("No access token found in auth data")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
    }
    account_id = get_chatgpt_account_id(auth)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    async with httpx.AsyncClient() as client:
        resp = await client.get(WHAM_USAGE_URL, headers=headers, timeout=10.0)

    if resp.status_code == 401:
        raise PermissionError("usage endpoint returned 401")
    if resp.status_code == 403:
        raise InvalidUsageAccountError("usage endpoint returned 403")
    if resp.status_code != 200:
        raise RuntimeError(f"usage endpoint returned {resp.status_code}")

    return normalize_usage_payload(resp.json())


class InvalidUsageAccountError(Exception):
    pass
