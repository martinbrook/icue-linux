"""Evaluate the ECMAScript expressions iCUE allows in data-* attributes,
<title>, x-icue-widget-group and x-icue-groups titles.

Like iCUE (which is Qt-based) we use QJSEngine. Available in scope:
  tr('key')         synchronous translation
  iCUE              the global object (language, fpsLimit, isPreview, defaultTemperatureUnit())
  plugins.<Key>     plugin objects; only blocking methods are meaningful here
  <ModuleBasename>  exports of each manifest "modules" entry
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Slot
from PySide6.QtQml import QJSEngine, QJSValue

from .widget import Widget


def translate(translations: dict, lang: str, key: str) -> str:
    for code in (lang, lang.split("_")[0], "en"):
        t = translations.get(code, {}).get("translation", {})
        if key in t:
            return t[key]
    return key


class _Tr(QObject):
    def __init__(self, translations, lang):
        super().__init__()
        self.t, self.lang = translations, lang

    @Slot(str, result=str)
    def tr(self, key):
        return translate(self.t, self.lang, key)


class ExpressionEngine:
    def __init__(self, widget: Widget, icue_globals: dict, plugins: dict[str, QObject]):
        self.engine = QJSEngine()
        self.engine.installExtensions(QJSEngine.Extension.ConsoleExtension)
        g = self.engine.globalObject()
        self._tr = _Tr(widget.translations, icue_globals["iCUELanguage"])
        QJSEngine.setObjectOwnership(self._tr, QJSEngine.ObjectOwnership.CppOwnership)
        g.setProperty("_trObj", self.engine.newQObject(self._tr))
        self.engine.evaluate("var tr = function(k){ return _trObj.tr(String(k)); };")
        icue = dict(icue_globals)
        unit = icue.pop("temperatureUnit", "°C")
        self.engine.evaluate(
            f"var iCUE = {json.dumps(icue)}; iCUE.defaultTemperatureUnit = function(){{return {json.dumps(unit)};}};")
        pl = self.engine.newObject()
        self._keep = []
        for key, obj in plugins.items():
            # Keep C++ ownership: these objects outlive this engine.
            QJSEngine.setObjectOwnership(obj, QJSEngine.ObjectOwnership.CppOwnership)
            pl.setProperty(key, self.engine.newQObject(obj))
            self._keep.append(obj)
        g.setProperty("plugins", pl)
        for mod in widget.modules:
            ns = self.engine.importModule(str(Path(mod).resolve()))
            if ns.isError():
                print(f"[icuehost] module {mod.name}: {ns.toString()}")
                continue
            g.setProperty(mod.name.split(".")[0], ns)

    def eval(self, expr: str | None, default=None):
        if expr is None or expr == "":
            return default
        v: QJSValue = self.engine.evaluate(expr)
        if v.isError():
            # Plain literals that aren't valid JS (e.g. data-label="Font Size")
            return expr
        if v.isUndefined():
            return default
        return v.toVariant()

    def eval_title(self, text: str) -> str:
        """<title> and group names may be a bare tr('x') call or plain text."""
        t = text.strip()
        if t.startswith("tr(") or t.startswith("'") or t.startswith('"'):
            return str(self.eval(t, t))
        return t
