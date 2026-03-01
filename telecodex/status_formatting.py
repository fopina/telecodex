from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def format_rate_limit_bucket(bucket: Any) -> str:
    if not isinstance(bucket, dict):
        return 'n/a'
    used_percent = bucket.get('usedPercent')
    resets_at = bucket.get('resetsAt')
    used_percent_display = f'{used_percent}%' if isinstance(used_percent, (int, float)) else 'n/a'
    reset_display = format_utc_timestamp(resets_at)
    return f'{used_percent_display} - {reset_display}'


def should_render_rate_limit(values: Any) -> bool:
    if not isinstance(values, dict):
        return True
    primary = values.get('primary')
    secondary = values.get('secondary')
    primary_used = primary.get('usedPercent') if isinstance(primary, dict) else None
    secondary_used = secondary.get('usedPercent') if isinstance(secondary, dict) else None
    return not (primary_used == 0 and secondary_used == 0)


def format_limit_name(limit_id: Any) -> str:
    return 'Global' if limit_id is None else str(limit_id)


def format_token_usage(usage: Any) -> str:
    if not isinstance(usage, dict):
        return 'n/a'
    total_tokens = usage.get('total_tokens')
    input_tokens = usage.get('input_tokens')
    output_tokens = usage.get('output_tokens')
    return f'total=`{total_tokens}` input=`{input_tokens}` output=`{output_tokens}`'


def format_utc_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return 'n/a'
    dt = datetime.fromtimestamp(value, tz=timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
