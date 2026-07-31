CONFIG_CACHE: dict = {"data": None, "loaded_at": 0}
CONFIG_CACHE_SECONDS: int = 30

RIFA_CACHE: dict = {"data": None, "loaded_at": 0}
RIFA_CACHE_SECONDS: int = 30

DASHBOARD_CACHE: dict = {"data": None, "loaded_at": 0}
DASHBOARD_CACHE_SECONDS: int = 30


def invalidate_rifa_cache() -> None:
    """Clear cached rifa data (30s TTL)."""
    RIFA_CACHE["data"] = None
    RIFA_CACHE["loaded_at"] = 0


def invalidate_dashboard_cache() -> None:
    """Clear cached dashboard stats (30s TTL)."""
    DASHBOARD_CACHE["data"] = None
    DASHBOARD_CACHE["loaded_at"] = 0


def invalidate_config_cache() -> None:
    """Clear config cache + cascading rifa cache."""
    CONFIG_CACHE["data"] = None
    CONFIG_CACHE["loaded_at"] = 0
    invalidate_rifa_cache()
