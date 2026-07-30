import time

CONFIG_CACHE: dict = {"data": None, "loaded_at": 0}
CONFIG_CACHE_SECONDS: int = 30

RIFA_CACHE: dict = {"data": None, "loaded_at": 0}
RIFA_CACHE_SECONDS: int = 30

DASHBOARD_CACHE: dict = {"data": None, "loaded_at": 0}
DASHBOARD_CACHE_SECONDS: int = 30


def invalidate_rifa_cache() -> None:
    RIFA_CACHE["data"] = None
    RIFA_CACHE["loaded_at"] = 0


def invalidate_dashboard_cache() -> None:
    DASHBOARD_CACHE["data"] = None
    DASHBOARD_CACHE["loaded_at"] = 0


def invalidate_config_cache() -> None:
    CONFIG_CACHE["data"] = None
    CONFIG_CACHE["loaded_at"] = 0
    invalidate_rifa_cache()
