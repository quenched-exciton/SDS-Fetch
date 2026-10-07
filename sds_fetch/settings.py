"""
settings.py - Remembers the window's choices between sessions.

The download folder, the preferred manufacturers, the "strict" and "overwrite"
tick boxes and the last list are saved in a small text file in your home folder
(on Windows: C:\\Users\\<you>\\.sds_fetch_settings.json). Delete that file to
start with an empty window.

Problems with the file (missing, damaged, not writable) are ignored: the
program then simply starts with empty fields.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path.home() / ".sds_fetch_settings.json"

DEFAULTS = {
    "download_folder": "",
    "preferred_manufacturers": "",
    "strict": False,
    "overwrite": False,
    "chemical_list": "",
}


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Return the saved settings, with DEFAULTS for anything missing."""
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings  # no file yet, or a damaged one
    if isinstance(saved, dict):
        for key, default in DEFAULTS.items():
            # Only accept a saved value of the expected type (text or True/False).
            if isinstance(saved.get(key), type(default)):
                settings[key] = saved[key]
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_FILE) -> None:
    """Save the settings. Errors are ignored (see the top of this file)."""
    try:
        path.write_text(json.dumps({key: settings.get(key, default) for key, default in DEFAULTS.items()},
                                   indent=2), encoding="utf-8")
    except OSError:
        pass
