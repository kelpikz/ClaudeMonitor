from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


def get_access_token() -> str:
    data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    return data["claudeAiOauth"]["accessToken"]


def fetch_usage(token: str) -> dict:
    response = httpx.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    if response.status_code == 401:
        print("Token expired — run Claude Code briefly to refresh it, then try again.")
        return {}
    response.raise_for_status()
    return response.json()


def format_time_left(resets_at: str | None) -> str:
    if not resets_at:
        return "unknown"

    reset_time = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    remaining_seconds = max(0, int((reset_time - datetime.now(timezone.utc)).total_seconds()))

    hours, remainder = divmod(remaining_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_remaining(usage_window: dict | None) -> str:
    if not usage_window or usage_window.get("utilization") is None:
        return "unknown"

    remaining = max(0.0, 100.0 - float(usage_window["utilization"]))
    return f"{remaining:.1f}%"


def print_usage_summary(usage: dict) -> None:
    five_hour = usage.get("five_hour")
    seven_day = usage.get("seven_day")

    five_hour_remaining = format_remaining(five_hour)
    reset_in = format_time_left(five_hour.get("resets_at") if five_hour else None)
    weekly_remaining = format_remaining(seven_day)
    weekly_reset_in = format_time_left(seven_day.get("resets_at") if seven_day else None)

    print(
        f"5 hour window context remaining: {five_hour_remaining} "
        f"(resets in {reset_in}) | weekly limit remaining: {weekly_remaining} "
        f"(resets in {weekly_reset_in})",
        flush=True,
    )


def main() -> None:
    token = get_access_token()

    while True:
        usage = fetch_usage(token)
        if usage:
            print_usage_summary(usage)
        time.sleep(30)


if __name__ == "__main__":
    main()
