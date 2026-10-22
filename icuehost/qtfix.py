"""Work around a reference-count bug in PySide6 6.12.0 on Python < 3.12.

There, every SignalInstance.emit() returns its bool result without taking a
reference, so each emit drops one reference from the shared `True` object.
After a few thousand emits True's count reaches zero, Python frees it and the
process aborts with "free(): invalid pointer" / "free(): invalid size"
(the crash seen after ~20-30 minutes on Ubuntu 22.04's Python 3.10).
Python 3.12+ makes True immortal, so the bug is harmless there; 6.9-6.11 are
not affected.

All host code emits through emit(), which measures the leak once and puts the
missing reference back. On unaffected versions it measures 0 and does nothing.
"""
from __future__ import annotations

import ctypes
import sys

_LEAK: int | None = None
_incref = ctypes.pythonapi.Py_IncRef
_incref.argtypes = [ctypes.py_object]
_incref.restype = None


def emit_leak() -> int:
    """References lost per emit() on this PySide6/Python combination."""
    global _LEAK
    if _LEAK is None:
        from PySide6.QtCore import QObject, Signal

        class _Probe(QObject):
            s = Signal()

        probe = _Probe()
        n = 8
        before = sys.getrefcount(True)
        for _ in range(n):
            probe.s.emit()
        lost = before - sys.getrefcount(True)
        for _ in range(max(0, lost)):       # repair what the probe itself lost
            _incref(True)
        _LEAK = max(0, round(lost / n))
    return _LEAK


def emit(signal, *args):
    """signal.emit(*args), compensating for the PySide6 6.12.0 refcount bug."""
    result = signal.emit(*args)
    leak = _LEAK if _LEAK is not None else emit_leak()
    for _ in range(leak):
        _incref(result)
    return result
