"""widgetbuilder.sensorsdataprovider:Sensors:1.0 backed by Linux sysfs/procfs.

Sources:
  /sys/class/hwmon/*        temperatures, fans, voltages, currents, power
  /sys/class/drm/card*/device/gpu_busy_percent   AMD GPU load
  /proc/stat, /proc/meminfo CPU load, memory load
  nvidia-smi (optional)     NVIDIA temperature / load / power

Set ICUE_LINUX_SYSFS_ROOT to point at a fake tree for testing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QTimer, Signal, Slot

from .base import PluginBase
from ..qtfix import emit

ROOT = Path(os.environ.get("ICUE_LINUX_SYSFS_ROOT", "/"))

CPU_CHIPS = {"k10temp", "coretemp", "zenpower", "cpu_thermal", "k8temp"}
GPU_CHIPS = {"amdgpu", "nouveau", "radeon", "i915", "xe"}


@dataclass
class Sensor:
    id: str
    name: str
    device: str
    type: str
    kind: str
    units: str
    value: str = ""
    connected: bool = True


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _fmt(v: float) -> str:
    return f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"


class SensorsPlugin(PluginBase):
    MODULE = "widgetbuilder.sensorsdataprovider"

    BLOCKING_JS = {
        # Same priority rules as documented for getDefaultSensorId.
        "getDefaultSensorIdBlock": """
            const [type, kind] = args;
            const s = ctx.sensors;
            let hit = s.find(x => x.type === type && (!kind || x.kind === kind))
                   || s.find(x => x.type === type) || s[0];
            return hit ? hit.id : "";
        """,
    }

    sensorAdded = Signal(str)
    sensorRemoved = Signal(str)
    sensorDataChanged = Signal(str)
    sensorValueChanged = Signal(str, str)
    sensorUnitsChanged = Signal(str, str)

    def __init__(self, temperature_unit: str = "°C", interval_ms: int = 1000, parent=None):
        super().__init__(parent)
        self.fahrenheit = temperature_unit == "°F"
        self.sensors: dict[str, Sensor] = {}
        self._cpu_prev: tuple[int, int] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)
        self.poll(notify=False)

    def start(self) -> None:
        self._timer.start()

    # ---------------------------------------------------------------- scanning
    def _scan(self) -> list[tuple[Sensor, float]]:
        out: list[tuple[Sensor, float]] = []
        out += self._scan_hwmon()
        out += self._scan_proc()
        out += self._scan_drm()
        out += self._scan_nvidia()
        return out

    def _scan_hwmon(self):
        base = ROOT / "sys/class/hwmon"
        res = []
        if not base.is_dir():
            return res
        for hw in sorted(base.iterdir()):
            chip = _read(hw / "name") or hw.name
            dev = "CPU" if chip in CPU_CHIPS else "GPU" if chip in GPU_CHIPS else chip
            for f in sorted(hw.glob("*_input")):
                stem = f.name[:-6]                       # temp1, fan2, in0 ...
                prefix = stem.rstrip("0123456789")
                raw = _read(f)
                if raw is None or not raw.lstrip("-").isdigit():
                    continue
                label = _read(hw / f"{stem}_label") or stem
                v = int(raw)
                sid = f"hwmon:{chip}:{hw.name}:{stem}"
                if prefix == "temp":
                    kind = "default"
                    if chip in CPU_CHIPS:
                        kind = "package" if label.lower() in ("tctl", "tdie", "package id 0") else (
                            "core" if label.lower().startswith(("core", "tccd")) else "cpu-temp")
                    elif chip in GPU_CHIPS:
                        kind = "gpu-temp"
                    s = Sensor(sid, label, dev, "temperature", kind, self._tunit())
                    val = v / 1000
                    if self.fahrenheit:
                        val = val * 9 / 5 + 32
                elif prefix == "fan":
                    s, val = Sensor(sid, label, dev, "fan", "default", "RPM"), v
                elif prefix == "in":
                    s, val = Sensor(sid, label, dev, "voltage", "voltage-in", "V"), v / 1000
                elif prefix == "curr":
                    s, val = Sensor(sid, label, dev, "current", "default", "A"), v / 1000
                elif prefix == "power":
                    s, val = Sensor(sid, label, dev, "power", "total-power-draw", "W"), v / 1e6
                else:
                    continue
                res.append((s, val))
            for f in sorted(hw.glob("power*_average")):   # amdgpu reports average, not input
                raw = _read(f)
                if raw and raw.isdigit():
                    stem = f.name.split("_")[0]
                    label = _read(hw / f"{stem}_label") or "Power"
                    res.append((Sensor(f"hwmon:{chip}:{hw.name}:{stem}", label, dev, "power",
                                       "total-power-draw", "W"), int(raw) / 1e6))
        return res

    def _scan_proc(self):
        res = []
        stat = _read(ROOT / "proc/stat")
        if stat:
            fields = [int(x) for x in stat.splitlines()[0].split()[1:]]
            idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
            load = 0.0
            if self._cpu_prev:
                di, dt = idle - self._cpu_prev[0], total - self._cpu_prev[1]
                load = 100 * (1 - di / dt) if dt > 0 else 0.0
            self._cpu_prev = (idle, total)
            res.append((Sensor("proc:cpu:load", "CPU Load", "CPU", "load", "default", "%"), load))
        mem = _read(ROOT / "proc/meminfo")
        if mem:
            kv = {l.split(":")[0]: int(l.split()[1]) for l in mem.splitlines() if l.split()[1:2]}
            if kv.get("MemTotal"):
                used = 100 * (1 - kv.get("MemAvailable", 0) / kv["MemTotal"])
                res.append((Sensor("proc:mem:load", "Memory Load", "Memory", "load",
                                   "memory-load", "%"), used))
        return res

    def _scan_drm(self):
        res = []
        for card in sorted((ROOT / "sys/class/drm").glob("card[0-9]*")):
            raw = _read(card / "device/gpu_busy_percent")
            if raw and raw.isdigit():
                res.append((Sensor(f"drm:{card.name}:load", "GPU Load", "GPU", "load",
                                   "gpu-load", "%"), int(raw)))
        return res

    def _scan_nvidia(self):
        if not shutil.which("nvidia-smi") or str(ROOT) != "/":
            return []
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw",
                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=1).stdout
        except (OSError, subprocess.TimeoutExpired):
            return []
        res = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            i, name, temp, util, pwr = parts
            for key, label, typ, kind, unit, val in (
                    ("temp", "GPU Temperature", "temperature", "gpu-temp", self._tunit(), temp),
                    ("load", "GPU Load", "load", "gpu-load", "%", util),
                    ("power", "GPU Power", "power", "total-power-draw", "W", pwr)):
                try:
                    v = float(val)
                except ValueError:
                    continue
                if typ == "temperature" and self.fahrenheit:
                    v = v * 9 / 5 + 32
                res.append((Sensor(f"nvidia:{i}:{key}", label, name, typ, kind, unit), v))
        return res

    def _tunit(self) -> str:
        return "°F" if self.fahrenheit else "°C"

    # ----------------------------------------------------------------- polling
    def poll(self, notify: bool = True) -> None:
        seen = set()
        for s, val in self._scan():
            s.value = _fmt(val)
            seen.add(s.id)
            old = self.sensors.get(s.id)
            self.sensors[s.id] = s
            if not notify:
                continue
            if old is None:
                emit(self.sensorAdded, s.id)
            elif old.units != s.units:
                emit(self.sensorUnitsChanged, s.id, s.units)
            if old is None or old.value != s.value:
                emit(self.sensorValueChanged, s.id, s.value)
                emit(self.sensorDataChanged, s.id)
        for gone in set(self.sensors) - seen:
            del self.sensors[gone]
            if notify:
                emit(self.sensorRemoved, gone)

    def blocking_context(self) -> dict:
        return {"sensors": [{"id": s.id, "type": s.type, "kind": s.kind}
                            for s in self.sensors.values()]}

    def _get(self, sid: str, attr: str, default=""):
        s = self.sensors.get(sid)
        return getattr(s, attr) if s else default

    # --------------------------------------------------------------- async API
    @Slot(int, str)
    def getSensorValue(self, rid, sid): self.respond(rid, self._get(sid, "value"))

    @Slot(int, str)
    def getSensorUnits(self, rid, sid): self.respond(rid, self._get(sid, "units"))

    @Slot(int, str)
    def getSensorName(self, rid, sid): self.respond(rid, self._get(sid, "name"))

    @Slot(int, str)
    def getSensorDeviceName(self, rid, sid): self.respond(rid, self._get(sid, "device"))

    @Slot(int, str)
    def getSensorType(self, rid, sid): self.respond(rid, self._get(sid, "type"))

    @Slot(int, str)
    def getSensorKind(self, rid, sid): self.respond(rid, self._get(sid, "kind", "invalid"))

    @Slot(int)
    def getAllSensorIds(self, rid): self.respond(rid, list(self.sensors))

    @Slot(int, str)
    def sensorIsConnected(self, rid, sid): self.respond(rid, sid in self.sensors)

    @Slot(int, str, str)
    def getDefaultSensorId(self, rid, sensor_type, kind=""):
        self.respond(rid, self.default_sensor(sensor_type, kind))

    # Synchronous variant for the expression engine (QJSEngine) only; in the
    # page it is served by BLOCKING_JS because QWebChannel calls are async.
    @Slot(str, result=str)
    @Slot(str, str, result=str)
    def getDefaultSensorIdBlock(self, sensor_type, kind=""):
        return self.default_sensor(sensor_type, kind)

    def default_sensor(self, sensor_type: str, kind: str = "") -> str:
        ss = list(self.sensors.values())
        for pred in (lambda s: s.type == sensor_type and (not kind or s.kind == kind),
                     lambda s: s.type == sensor_type,
                     lambda s: True):
            for s in ss:
                if pred(s):
                    return s.id
        return ""
