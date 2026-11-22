"""icuelinux.ecoflow:EcoFlow:1.0 — EcoFlow battery status from Home Assistant.

Reads every device of HA's EcoFlow Cloud integration (ecoflow_cloud, HACS)
with one /api/template call and emits statusUpdated(json):

  {"ok": true, "error": "", "devices": [{
      "name", "model", "online", "level", "limit", "minLevel", "inW", "outW",
      "chargeMin", "dischargeMin",
      "packs": [{"label": "Main"|"Extra N", "level"}]   # only with extra batteries
  }]}

Numbers are null when HA reports unknown/unavailable. The HA URL and a
long-lived token live in ~/.config/icue-linux/homeassistant.json (mode 0600)
as {"url": "https://ha.example:8123", "token": "..."}; the token never
reaches the widget.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QTimer, Signal, Slot

from .base import PluginBase
from ..qtfix import emit

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "icue-linux"
HA_FILE = CONFIG_DIR / "homeassistant.json"

# one row per entity: [device id, device name, model, object id, state]
TEMPLATE = """{%- set ns = namespace(rows=[]) -%}
{%- for e in integration_entities('ecoflow_cloud') -%}
{%- set d = device_id(e) -%}
{%- if d -%}{%- set ns.rows = ns.rows + [[d, device_attr(d, 'name_by_user') or device_attr(d, 'name'),
    device_attr(d, 'model'), e.split('.', 1)[1], states(e)]] -%}{%- endif -%}
{%- endfor -%}{{ ns.rows | tojson }}"""

_SLAVE = re.compile(r"^slave_(\d+)_battery_level$")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None                            # "unknown", "unavailable", missing


def parse(rows: list) -> list[dict]:
    """Group template rows into devices. Sensor keys are object ids minus the
    device's common prefix (sensor.delta_battery_level -> battery_level)."""
    by_dev: dict[str, dict] = {}
    for dev_id, name, model, obj, state in rows:
        d = by_dev.setdefault(dev_id, {"name": name or "EcoFlow", "model": model or "", "objs": {}})
        d["objs"][obj] = state
    devices = []
    for d in by_dev.values():
        prefix = os.path.commonprefix(list(d["objs"]))
        prefix = prefix[:prefix.rfind("_") + 1]               # whole words only
        s = {obj[len(prefix):]: v for obj, v in d["objs"].items()}
        if "battery_level" not in s:
            continue                                          # not a battery (smart plug, meter...)
        slaves = sorted((int(m.group(1)), k) for k in s if (m := _SLAVE.match(k)))
        packs = []
        if slaves:
            packs = [{"label": "Main", "level": _num(s.get("main_battery_level"))}]
            packs += [{"label": f"Extra {n}", "level": _num(s[k])} for n, k in slaves]
        devices.append({"name": d["name"], "model": d["model"].replace("_", " "),
                        "online": (s["status"] == "online") if "status" in s else None,
                        "level": _num(s["battery_level"]), "limit": _num(s.get("max_charge_level")),
                        "minLevel": _num(s.get("min_discharge_level")),
                        "inW": _num(s.get("total_in_power")), "outW": _num(s.get("total_out_power")),
                        "chargeMin": _num(s.get("charge_remaining_time")),
                        "dischargeMin": _num(s.get("discharge_remaining_time")), "packs": packs})
    return sorted(devices, key=lambda d: d["name"].lower())


def fetch() -> dict:
    """One poll of Home Assistant (blocking; run on a worker thread)."""
    try:
        cfg = json.loads(HA_FILE.read_text())
        req = urllib.request.Request(cfg["url"].rstrip("/") + "/api/template",
                                     data=json.dumps({"template": TEMPLATE}).encode(),
                                     headers={"Authorization": f"Bearer {cfg['token']}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            devices = parse(json.loads(r.read()))
    except FileNotFoundError:
        return {"ok": False, "error": f"set up Home Assistant access in {HA_FILE} (see README)", "devices": []}
    except urllib.error.HTTPError as e:
        msg = "Home Assistant rejected the token" if e.code == 401 else f"Home Assistant error {e.code}"
        return {"ok": False, "error": msg, "devices": []}
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            return {"ok": False, "error": f"Home Assistant certificate: {e.reason.verify_message}", "devices": []}
        return {"ok": False, "error": "Home Assistant unreachable", "devices": []}
    except (OSError, ValueError, KeyError, TypeError):
        return {"ok": False, "error": "unexpected reply from Home Assistant", "devices": []}
    if not devices:
        return {"ok": False, "error": "no EcoFlow batteries in Home Assistant (EcoFlow Cloud integration)",
                "devices": []}
    return {"ok": True, "error": "", "devices": devices}


class EcoflowPlugin(PluginBase):
    MODULE = "icuelinux.ecoflow"

    statusUpdated = Signal(str)               # JSON, see module docstring
    _fetched = Signal(object)                 # result dict, from the worker thread

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = ""
        self._busy = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self._fetched.connect(self._deliver)

    @Slot(int)
    def watch(self, interval_ms: int = 10000):
        """Start (or retune) polling; HA isn't contacted until a widget asks."""
        self.timer.start(max(5000, int(interval_ms)))
        if self.status:                       # a widget (re)loading gets the current state now
            QTimer.singleShot(0, lambda: emit(self.statusUpdated, self.status))
        QTimer.singleShot(0, self.poll)

    @Slot(int)
    def getStatus(self, rid: int):
        self.respond(rid, self.status)

    def poll(self):
        if self._busy:                        # previous request still in flight
            return
        self._busy = True
        threading.Thread(target=lambda: emit(self._fetched, fetch()), daemon=True).start()

    def _deliver(self, result: dict):
        self._busy = False
        status = json.dumps(result)
        if status != self.status:
            self.status = status
            emit(self.statusUpdated, status)
