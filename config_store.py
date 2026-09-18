import json
import os
import logging
import threading
import tempfile
from copy import deepcopy

logger = logging.getLogger(__name__)

# Docker keeps its persistent volume at /data.  Native Windows runs should
# keep the same layout inside the EasyProxy checkout instead of writing to
# the drive root (C:\\data).
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_DIR = (
    os.path.join(_PROJECT_DIR, "data") if os.name == "nt" else "/data"
)
_CONFIG_DIR = os.environ.get("CONFIG_DIR") or _DEFAULT_CONFIG_DIR
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
DEFAULT_RECORDINGS_DIR = os.path.join(_CONFIG_DIR, "recordings")

# These values were previously injected into every saved configuration.
# Keep them only for one-time migration of an untouched legacy config.
_LEGACY_WARP_EXCLUDE_DOMAINS = [
    "strem.fun", "*.strem.fun", "torrentio.strem.fun",
    "real-debrid.com", "*.real-debrid.com", "realdebrid.com",
    "*.realdebrid.com", "api.real-debrid.com",
    "premiumize.me", "*.premiumize.me", "www.premiumize.me",
    "alldebrid.com", "*.alldebrid.com", "api.alldebrid.com",
    "debrid-link.com", "*.debrid-link.com", "debridlink.com",
    "*.debridlink.com", "api.debrid-link.com",
    "torbox.app", "*.torbox.app", "api.torbox.app",
    "offcloud.com", "*.offcloud.com", "api.offcloud.com",
    "put.io", "*.put.io", "api.put.io",
]

DEFAULT_CONFIG = {
    "enable_warp": False,
    "warp_license_key": "",
    "warp_exclude_domains": [],
    "warp_exclude_domains_custom": [],
    "global_proxies": [],
    "transport_routes": [],
    "extractor_proxies": {},
    "warp_off_extractors": [],
    "proxy_off_extractors": [],
    "proxy_exclude_domains": [],
    # Force the highest video variant (no adaptive bitrate). Can be enabled per
    # extractor, for every MPD source, for every HLS source, or per request
    # with &max_res=true.
    "max_res_extractors": [],
    "max_res_mpd": False,
    "max_res_hls": False,
    "dvr_enabled": False,
    "recordings_dir": DEFAULT_RECORDINGS_DIR,
    "max_recording_duration": 28800,
    "recordings_retention_days": 7,
    "proxy_test_timeout": 10,
    "proxy_test_concurrency": None,
    "log_level": "WARNING",
}

_lock = threading.RLock()
_config_data = None


def _atomic_write(path, payload):
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=_CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load():
    global _config_data
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            migrated = False
            # Only remove the exact legacy defaults that EasyProxy itself used
            # to force into every configuration. Different/custom values are
            # left untouched.
            if data.get("warp_exclude_domains") == _LEGACY_WARP_EXCLUDE_DOMAINS:
                data["warp_exclude_domains"] = []
                migrated = True
            if data.get("warp_off_extractors") == ["cinejoy"]:
                data["warp_off_extractors"] = []
                migrated = True

            merged = deepcopy(DEFAULT_CONFIG)
            merged.update(data)
            _config_data = merged

            if migrated:
                _atomic_write(_CONFIG_FILE, json.dumps(_config_data, indent=2))
                logger.info("Migrated legacy forced WARP exclusions from %s", _CONFIG_FILE)

            logger.debug("Loaded config from %s", _CONFIG_FILE)
            return
        except Exception as e:
            logger.warning("Failed to load config.json: %s", e)
    _config_data = deepcopy(DEFAULT_CONFIG)
    _save()


def _save():
    if _config_data is not None:
        _atomic_write(_CONFIG_FILE, json.dumps(_config_data, indent=2))


def _commit(data):
    """Persist before publishing in memory; keep the preceding complete config."""
    global _config_data
    payload = json.dumps(data, indent=2, allow_nan=False)
    if data == _config_data:
        return
    if os.path.exists(_CONFIG_FILE):
        with open(_CONFIG_FILE, encoding="utf-8") as stream:
            previous = stream.read()
        _atomic_write(_CONFIG_FILE + ".previous", previous)
    _atomic_write(_CONFIG_FILE, payload)
    _config_data = deepcopy(data)


def get(key, default=None):
    with _lock:
        if _config_data is None:
            _load()
        return deepcopy(_config_data.get(key, default))


def get_all():
    with _lock:
        if _config_data is None:
            _load()
        return deepcopy(_config_data)


def get_previous():
    with _lock:
        with open(_CONFIG_FILE + ".previous", encoding="utf-8") as stream:
            return json.load(stream)


def set(key, value):
    update({key: value})


def update(values: dict):
    with _lock:
        data = get_all()
        data.update(deepcopy(values))
        _commit(data)


def replace_all(data: dict):
    """Replace entire config with new data (merged with defaults)."""
    with _lock:
        get_all()
        merged = deepcopy(DEFAULT_CONFIG)
        merged.update(deepcopy(data))
        _commit(merged)


def delete(key):
    with _lock:
        data = get_all()
        data.pop(key, None)
        _commit(data)


def validate_import(data):
    """Reject malformed backups before touching live settings or their backup."""
    if not isinstance(data, dict) or not data:
        raise ValueError("Il backup deve essere un oggetto JSON non vuoto.")
    if data.keys() - DEFAULT_CONFIG.keys():
        raise ValueError("Il backup contiene impostazioni non riconosciute da questa versione.")
    for key, value in data.items():
        default = DEFAULT_CONFIG[key]
        if key == "proxy_test_concurrency":
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Concorrenza proxy non valida.")
            continue
        if type(value) is not type(default):
            raise ValueError("Tipo di impostazione non valido: " + key)
        if isinstance(default, list) and key != "transport_routes":
            if any(not isinstance(item, str) for item in value):
                raise ValueError("Elenco non valido: " + key)
        if type(default) is int and (value < 0 or (key == "proxy_test_timeout" and value == 0)):
            raise ValueError("Valore numerico non valido: " + key)
    for route in data.get("transport_routes", []):
        if (not isinstance(route, dict) or not isinstance(route.get("url"), str)
                or not route["url"].strip()
                or (route.get("proxy") is not None and not isinstance(route["proxy"], str))
                or type(route.get("disable_ssl", False)) is not bool):
            raise ValueError("Regola di instradamento non valida.")
    for name, proxy in data.get("extractor_proxies", {}).items():
        if not isinstance(name, str) or not (isinstance(proxy, str) or
                (isinstance(proxy, list) and all(isinstance(item, str) for item in proxy)) or
                (isinstance(proxy, dict) and isinstance(proxy.get("file"), str))):
            raise ValueError("Configurazione proxy non valida.")
    if "log_level" in data and data["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Livello dei log non valido.")
    return data


_load()
