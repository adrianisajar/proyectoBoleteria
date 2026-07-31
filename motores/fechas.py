import os
from datetime import UTC, datetime, timedelta

TIMEZONE_OFFSET = timedelta(hours=int(os.environ.get("TZ_OFFSET", "-5")))


def now_local() -> datetime:
    """Return current local naive datetime (UTC + TZ_OFFSET)."""
    return (datetime.now(UTC) + TIMEZONE_OFFSET).replace(tzinfo=None)
