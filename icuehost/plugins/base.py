"""Common plumbing for iCUE-compatible plugin objects.

Each plugin is a QObject published on a QWebChannel under its window.plugins
key (e.g. "Sensorsdataprovider"). Widgets call its slots and .connect() to
its signals exactly as they would under iCUE.
"""
from __future__ import annotations

from PySide6.QtCore import QMetaMethod, QObject, QTimer, Signal
from ..qtfix import emit


class PluginBase(QObject):
    #: module name from the manifest, e.g. "widgetbuilder.sensorsdataprovider"
    MODULE = ""
    #: JS source for synchronous ("...Block") methods, evaluated in the page.
    #: Each entry is the body of `function(ctx, ...args)`; ctx is blocking_context().
    BLOCKING_JS: dict[str, str] = {}

    asyncResponse = Signal(int, "QVariant")

    def respond(self, request_id: int, value) -> None:
        """Emit asyncResponse on the next event-loop turn, like iCUE does."""
        QTimer.singleShot(0, lambda: emit(self.asyncResponse, int(request_id), value))

    def blocking_context(self) -> dict:
        return {}

    def start(self) -> None:
        """Begin polling / subscribing. Called once by the host."""

    def describe(self) -> dict:
        """Method/signal/property names, used by the in-page stub that exists
        before the QWebChannel connection is up."""
        mo = self.metaObject()
        methods, signals = set(), set()
        for i in range(QObject.staticMetaObject.methodCount(), mo.methodCount()):
            m = mo.method(i)
            name = bytes(m.name()).decode()
            if m.methodType() == QMetaMethod.MethodType.Signal:
                signals.add(name)
            elif m.methodType() in (QMetaMethod.MethodType.Slot, QMetaMethod.MethodType.Method):
                methods.add(name)
        props = [mo.property(i).name()
                 for i in range(QObject.staticMetaObject.propertyCount(), mo.propertyCount())]
        return {"methods": sorted(methods | set(self.BLOCKING_JS)),
                "signals": sorted(signals),
                "properties": props,
                "blocking": self.BLOCKING_JS}
