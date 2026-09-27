# -*- coding: utf-8 -*-
"""时间与 staleness 工具。所有入库时间统一 UTC，展示层再转本地时区。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

DAY = 86400


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> float:
    return time.time()


def to_dt(v: datetime | float | int | str | None) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def ts(v: datetime | float | int | None) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return v.timestamp()


def day_start(dt: datetime | None = None) -> datetime:
    d = (dt or utcnow()).astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return d


def daterange(start: datetime, end: datetime):
    cur = day_start(start)
    last = day_start(end)
    while cur <= last:
        yield cur
        cur += timedelta(days=1)


def age_seconds(reference: datetime | float | None) -> float:
    if reference is None:
        return float("inf")
    return max(0.0, now_ts() - ts(reference))


def fmt_local(dt: datetime | int | float | str | None, tz_offset_hours: int = 8) -> str:
    """格式化为本地时间字符串（默认 +8 区）。

    同时兼容 datetime、UTC 时间戳（秒）、ISO 字符串，便于在 CLI / 页面里直接复用。
    """
    if dt is None:
        return "-"
    if isinstance(dt, (int, float)) and not isinstance(dt, bool):
        dt = datetime.fromtimestamp(float(dt), tz=timezone.utc)
    elif isinstance(dt, str):
        if dt.strip().isdigit():
            dt = datetime.fromtimestamp(float(dt), tz=timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except ValueError:
                return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=tz_offset_hours)))
    return local.strftime("%Y-%m-%d %H:%M:%S")
