# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import json
import os
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULTS: Dict = {
    "port": 8765,
    "solver_threads": 8,
    "contribute": True,
    "extra_origins": [],
}


def _warn(message: str) -> None:
    print(f"[config] {message}", flush=True)


def _load_file() -> Dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _warn(f"could not read config.json ({e}); using defaults")
        return {}
    if not isinstance(data, dict):
        _warn("config.json must be a JSON object; using defaults")
        return {}
    for key in data:
        if key not in DEFAULTS and not key.startswith("_"):
            _warn(f"unknown setting '{key}' ignored")
    return data


_raw = _load_file()


def _int(key: str, low: int, high: int) -> int:
    value = _raw.get(key, DEFAULTS[key])
    try:
        value = int(value)
    except (TypeError, ValueError):
        _warn(f"'{key}' must be a whole number; using {DEFAULTS[key]}")
        return DEFAULTS[key]
    if not low <= value <= high:
        _warn(f"'{key}' must be between {low} and {high}; using {DEFAULTS[key]}")
        return DEFAULTS[key]
    return value


def _bool(key: str) -> bool:
    value = _raw.get(key, DEFAULTS[key])
    if isinstance(value, bool):
        return value
    _warn(f"'{key}' must be true or false; using {DEFAULTS[key]}")
    return DEFAULTS[key]


def _str_list(key: str) -> List[str]:
    value = _raw.get(key, DEFAULTS[key])
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    _warn(f"'{key}' must be a list of strings; using none")
    return []


PORT: int = _int("port", 1, 65535)
SOLVER_THREADS: int = _int("solver_threads", 1, 256)
CONTRIBUTE: bool = _bool("contribute")
EXTRA_ORIGINS: List[str] = _str_list("extra_origins")

CONTRIBUTE_URL: str = _raw.get("_contribute_url") or "https://api.skyshards.com/greenhouse/cache/contribute"
