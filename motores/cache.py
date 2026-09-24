import threading

CONFIG_CACHE: dict = {"data": None, "loaded_at": 0}
CONFIG_CACHE_SECONDS: int = 5
CONFIG_LOCK = threading.Lock()

RIFA_CACHE: dict = {"data": None, "loaded_at": 0}
RIFA_CACHE_SECONDS: int = 5
RIFA_LOCK = threading.Lock()

DASHBOARD_CACHE: dict = {"data": None, "loaded_at": 0}
DASHBOARD_CACHE_SECONDS: int = 10
DASHBOARD_LOCK = threading.Lock()

VENDOR_PANEL_CACHE: dict = {"data": None, "loaded_at": 0}
VENDOR_PANEL_CACHE_SECONDS: int = 10
VENDOR_PANEL_LOCK = threading.Lock()

GLOBAL_COUNTS_CACHE: dict = {"data": None, "loaded_at": 0, "valor": None}
GLOBAL_COUNTS_LOCK = threading.Lock()


def invalidate_rifa_cache() -> None:
    """Clear cached rifa data (30s TTL)."""
    with RIFA_LOCK:
        RIFA_CACHE["data"] = None
        RIFA_CACHE["loaded_at"] = 0


def invalidate_dashboard_cache() -> None:
    """Clear cached dashboard stats + global ticket counts (30s TTL)."""
    with DASHBOARD_LOCK:
        DASHBOARD_CACHE["data"] = None
        DASHBOARD_CACHE["loaded_at"] = 0
    with GLOBAL_COUNTS_LOCK:
        GLOBAL_COUNTS_CACHE["data"] = None
        GLOBAL_COUNTS_CACHE["loaded_at"] = 0
        GLOBAL_COUNTS_CACHE["valor"] = None
    invalidate_vendor_panel_cache()


def invalidate_vendor_panel_cache() -> None:
    """Clear cached vendor panel data (30s TTL)."""
    with VENDOR_PANEL_LOCK:
        VENDOR_PANEL_CACHE["data"] = None
        VENDOR_PANEL_CACHE["loaded_at"] = 0


def invalidate_config_cache() -> None:
    """Clear config cache + cascading rifa cache."""
    with CONFIG_LOCK:
        CONFIG_CACHE["data"] = None
        CONFIG_CACHE["loaded_at"] = 0
    invalidate_rifa_cache()


def invalidate_all_caches() -> None:
    """Clear all application caches at once."""
    invalidate_config_cache()
    invalidate_dashboard_cache()
