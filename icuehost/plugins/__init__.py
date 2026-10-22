from .base import PluginBase
from .others import REGISTRY as _OTHERS, StubPlugin
from .sensors import SensorsPlugin
from .llm import LlmMonitorPlugin


class PluginHub:
    """One shared instance per plugin type, published to every widget page."""

    def __init__(self, temperature_unit: str = "°C"):
        self._factories = {"widgetbuilder.sensorsdataprovider":
                           lambda: SensorsPlugin(temperature_unit=temperature_unit),
                           "icuelinux.llmmonitor": LlmMonitorPlugin, **_OTHERS}
        self._instances: dict[str, PluginBase] = {}

    def get(self, module: str) -> PluginBase:
        if module not in self._instances:
            inst = self._factories.get(module, StubPlugin)()
            inst.start()
            self._instances[module] = inst
        return self._instances[module]


__all__ = ["PluginHub", "PluginBase", "SensorsPlugin"]
