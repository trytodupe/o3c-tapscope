"""Per-machine settings, edited in the studio web UI and stored as JSON.

Everything the tool needs to find on this computer lives here: the osu! install
directory (Songs / Replays / Skins are derived from it), the key labels, each key's RT
bounds, and the device / tosu endpoints. The file is read once at startup, so changing
it in the UI means restarting the studio: the watcher, the renderer and the device
selection are all built from it.
"""

import json
import re
from pathlib import Path

from calibration import KEY_NAMES, STEP_UM

DEFAULT_PORT = 8770
DEFAULT_TOSU_URL = "http://127.0.0.1:24050"
DEFAULT_WINDOW_MIN = 10.0

KEY_COUNT = len(KEY_NAMES)


def default_keys():
    return [{"name": name, "rt_low": None, "rt_high": None} for name in KEY_NAMES.values()]


def defaults():
    return {
        "osu_root": "",
        "keys": default_keys(),
        "device_path": "",
        "tosu_url": DEFAULT_TOSU_URL,
        "skin": "",
        "port": DEFAULT_PORT,
        "window_min": DEFAULT_WINDOW_MIN,
    }


def _number(value):
    """A float or None; blank / unparsable input is treated as "unset"."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_keys(keys):
    result = default_keys()
    for index, key in enumerate(keys or []):
        if index >= KEY_COUNT or not isinstance(key, dict):
            break
        result[index]["name"] = str(key.get("name") or result[index]["name"]).strip() or result[index]["name"]
        result[index]["rt_low"] = _number(key.get("rt_low"))
        result[index]["rt_high"] = _number(key.get("rt_high"))
    return result


def _normalize_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _normalize_window(value):
    minutes = _number(value)
    return DEFAULT_WINDOW_MIN if minutes is None else max(0.0, minutes)


def coerce(payload):
    """A settings dict from an untrusted payload, with every field normalized."""
    settings = defaults()
    if not isinstance(payload, dict):
        return settings
    settings["osu_root"] = str(payload.get("osu_root") or "").strip()
    settings["device_path"] = str(payload.get("device_path") or "").strip()
    settings["tosu_url"] = str(payload.get("tosu_url") or DEFAULT_TOSU_URL).strip() or DEFAULT_TOSU_URL
    settings["skin"] = str(payload.get("skin") or "").strip()
    settings["port"] = _normalize_port(payload.get("port"))
    settings["window_min"] = _normalize_window(payload.get("window_min"))
    settings["keys"] = _normalize_keys(payload.get("keys"))
    return settings


def load(path):
    """Stored settings merged over the defaults; a missing file is not an error."""
    try:
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    settings = coerce(stored)
    if not settings["osu_root"]:
        settings["osu_root"] = detect_osu_root()
    return settings


def save(path, settings):
    normalized = coerce(settings)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return normalized


def detect_osu_root():
    """Best-effort osu! install folder from the ``osu!`` protocol association.

    osu!stable registers ``osu\\shell\\open\\command`` as ``"<dir>\\osu!.exe" "%1"``,
    which is the only reliable machine-wide pointer to the install directory.
    """
    try:
        import winreg
    except ImportError:
        return ""
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Classes\osu\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"osu\shell\open\command"),
    )
    for root, subkey in locations:
        try:
            with winreg.OpenKey(root, subkey) as handle:
                command = winreg.QueryValueEx(handle, "")[0]
        except OSError:
            continue
        match = re.search(r'"([^"]+\.exe)"', command, re.IGNORECASE)
        if match:
            folder = Path(match.group(1)).parent
            if folder.is_dir():
                return str(folder)
    return ""


def subdir(settings, name):
    """``<osu_root>/<name>`` as a Path, or None when no root is set."""
    root = settings.get("osu_root")
    return (Path(root) / name) if root else None


def calibration_data(settings):
    """The calibration-shaped dict ``build_calibration_payload`` consumes."""
    keys = []
    for index, key in enumerate(settings["keys"]):
        keys.append({
            "index": index,
            "name": key["name"],
            "rt_range_mm": [key["rt_low"], key["rt_high"]],
            "table": [],
        })
    return {"source": "settings", "step_um": STEP_UM, "keys": keys}
