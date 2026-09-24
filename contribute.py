# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Dict

import config

ENABLED = config.CONTRIBUTE
URL = config.CONTRIBUTE_URL
TIMEOUT_SECONDS = 20.0


def _user_agent() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        version = "dev"
    return f"SkyShards-LocalSolver/{version} (+https://github.com/Campionnn/SkyShards-Solver)"


USER_AGENT = _user_agent()

FULL_GRID_CELLS = 100


def worth_offering(params: Dict, result: Dict) -> bool:
    if not ENABLED or not isinstance(result, dict):
        return False
    if result.get("status") not in ("OPTIMAL", "FEASIBLE", "CANCELLED"):
        return False
    if not result.get("mutations"):
        return False
    if params.get("locks"):
        return False
    targets = params.get("targets") or []
    cells = params.get("cells") or []
    if len(cells) == FULL_GRID_CELLS:
        return True
    return len(targets) == 1 and targets[0].get("mutation") == "gloomgourd" and bool(targets[0].get("maximize"))


def offer(params: Dict, result: Dict) -> None:
    if not worth_offering(params, result):
        return
    body = {
        "params": {k: v for k, v in params.items() if k in ("cells", "targets", "priorities", "effect_weights", "buff_crops")},
        "placements": [
            {"crop": p["crop"], "position": p["position"], "size": p["size"]}
            for p in result.get("placements", []) if not p.get("locked")
        ],
        "mutations": [
            {"mutation": m["mutation"], "position": m["position"], "size": m["size"]}
            for m in result.get("mutations", [])
        ],
    }
    threading.Thread(target=_send, args=(body,), daemon=True).start()


def _send(body: Dict) -> None:
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    req = urllib.request.Request(URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        print(f"[contribute] server refused ({e.code})", flush=True)
        return
    except Exception as e:
        print(f"[contribute] could not reach the public server: {e}", flush=True)
        return
    if payload.get("accepted"):
        print(f"[contribute] accepted: your layout ({payload.get('submitted_score')}) beat the server's "
              f"({payload.get('server_score')}) - thanks!", flush=True)
    else:
        print(f"[contribute] not stored: {payload.get('reason', 'unknown')}", flush=True)
