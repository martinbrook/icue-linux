"""Settings panel generated from a widget's x-icue-property controls and
x-icue-groups, standing in for iCUE's own panel. Changes apply live."""
from __future__ import annotations

import json

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QFormLayout, QGroupBox,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)
from PySide6.QtCore import Qt


def _options(values) -> list[tuple[object, str]]:
    out = []
    for v in values or []:
        if isinstance(v, dict):
            out.append((v.get("key", v.get("value")), str(v.get("value", v.get("key")))))
        else:
            out.append((v, str(v)))
    return out


class SettingsPanel(QScrollArea):
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view, self.inst = view, view.inst
        self.setWindowTitle(f"{self.inst.title} — settings")
        self.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        by_name = {c["name"]: c for c in self.inst.controls}
        grouped = [g for g in self.inst.groups if g["properties"]]
        seen = {p for g in grouped for p in g["properties"]}
        rest = [n for n in by_name if n not in seen]
        if rest:
            grouped.append({"title": "Other", "info": "", "properties": rest})
        for g in grouped:
            box = QGroupBox(g["title"] or "Settings")
            if g.get("info"):
                box.setToolTip(g["info"])
            form = QFormLayout(box)
            for name in g["properties"]:
                c = by_name.get(name)
                if c:
                    form.addRow(str(c.get("label", name)), self._editor(c))
            lay.addWidget(box)
        lay.addStretch(1)
        self.setWidget(body)
        self.resize(420, 600)

    def _apply(self, name, value):
        self.view.push_values({name: value})

    def _editor(self, c):
        name, typ = c["name"], c["type"]
        cur = self.inst.values.get(name)
        if typ == "color":
            b = QPushButton(str(cur or ""))
            b.setStyleSheet(f"background:{cur};" if cur else "")

            def pick():
                col = QColorDialog.getColor(QColor(str(cur or "#ffffff")), self,
                                            options=QColorDialog.ColorDialogOption.ShowAlphaChannel)
                if col.isValid():
                    v = col.name(QColor.NameFormat.HexArgb if col.alpha() < 255 else QColor.NameFormat.HexRgb)
                    b.setText(v); b.setStyleSheet(f"background:{v};")
                    self._apply(name, v)
            b.clicked.connect(pick)
            return b
        if typ == "slider":
            lo, hi = float(c.get("min", 0)), float(c.get("max", 100))
            step = float(c.get("step", 1)) or 1
            scale = 1 if step >= 1 and lo.is_integer() else round(1 / step)
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(int(lo * scale), int(hi * scale))
            s.setSingleStep(int(step * scale))
            s.setValue(int(float(cur if cur is not None else lo) * scale))
            unit = str(c.get("unit-label", "") or "")
            lbl = QLabel(f"{cur}{unit}")
            row = QWidget(); h = QVBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(s); h.addWidget(lbl)

            def moved(v):
                val = v / scale if scale != 1 else v
                lbl.setText(f"{val}{unit}")
                self._apply(name, val)
            s.valueChanged.connect(moved)
            return row
        if typ == "switch":
            cb = QCheckBox()
            cb.setChecked(bool(cur))
            cb.toggled.connect(lambda v: self._apply(name, v))
            return cb
        if typ in ("combobox", "search-combobox", "tab-buttons", "sensors-combobox"):
            box = QComboBox()
            box.setEditable(typ == "search-combobox")
            if typ == "sensors-combobox":
                sens = self.inst.plugin_objs.get("Sensorsdataprovider")
                opts = [(s.id, f"{s.device}: {s.name}") for s in sens.sensors.values()] if sens else []
            else:
                opts = _options(c.get("values"))
            for key, label in opts:
                box.addItem(label, key)
            idx = next((i for i, (k, _) in enumerate(opts) if k == cur), -1)
            box.setCurrentIndex(idx)
            box.currentIndexChanged.connect(lambda i: self._apply(name, box.itemData(i)))
            return box
        # textfield, media-selector, sensors-factory and anything unknown:
        # edit as text (JSON for structured values).
        le = QLineEdit(cur if isinstance(cur, str) else json.dumps(cur))
        structured = not isinstance(cur, str) and typ != "textfield"

        def done():
            t = le.text()
            if structured:
                try:
                    t = json.loads(t)
                except ValueError:
                    return
            self._apply(name, t)
        le.editingFinished.connect(done)
        return le
