from datetime import datetime, timedelta, timezone


class AnonymousDailyLimitError(Exception):
    pass


def anonymous_day_start(now: datetime) -> datetime:
    local = now.astimezone(timezone(timedelta(hours=8)))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
