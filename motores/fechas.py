import os
from datetime import date, datetime, timedelta, timezone

TIMEZONE_OFFSET = timedelta(hours=int(os.environ.get("TZ_OFFSET", "-5")))


def now_local():
    return (datetime.now(timezone.utc) + TIMEZONE_OFFSET).replace(tzinfo=None)
