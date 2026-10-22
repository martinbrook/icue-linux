# icue-linux

Run Corsair iCUE HTML widgets (Widget API 1.4.0) on Linux, on the Xeneon Edge or any screen.

This is a clean-room host built from Corsair/Elgato's **public** widget documentation
(docs.elgato.com/icue/widgets). No iCUE code or binaries are used or needed.

## How it works

iCUE renders widgets in QtWebEngine and talks to them through injected globals and
Qt signal/slot plugin objects. icue-linux does the same with PySide6:

| iCUE contract | icue-linux |
|---|---|
| `manifest.json`, `<title>`, `x-icue-*` meta tags, `x-icue-groups` | parsed in `widget.py` |
| JS expressions in `data-*` (`tr()`, `iCUE`, manifest `modules`, blocking plugin calls) | evaluated in QJSEngine (`expressions.py`) |
| injected property globals, `iCUE`, `device`, `uniqueId`, `iCUE_initialized`, `icueEvents` | `shim.js`, injected at DocumentCreation |
| `window.plugins.*`, `plugin<Key>Events`, `plugin<Key>_initialized`, `asyncResponse` | QObjects on QWebChannel, fronted by synchronous stubs that queue until the channel is up |
| `…Block` (synchronous) plugin methods in page JS | served from a snapshot in the shim |
| runtime `tr()` returning a Promise | `shim.js` |
| settings panel | generated Qt panel (`preview --settings`), persisted per instance |

### Plugins

| Plugin | Linux backend |
|---|---|
| Sensors (`widgetbuilder.sensorsdataprovider`) | hwmon (CPU/GPU temps, fans, volts, power), `/proc` CPU & memory load, amdgpu `gpu_busy_percent`, `nvidia-smi` if present |
| Media (`widgetbuilder.mediadataprovider`) | MPRIS via `playerctl` |
| Link (`widgetbuilder.linkprovider`) | opens http(s)/mailto in the default browser |
| FPS (`widgetbuilder.fpsdataprovider`) | newest MangoHud CSV log (enable MangoHud logging) |
| Device Action, Stream Deck, anything else | present and initialised, but silent |

## Install

```sh
pip install PySide6          # 6.9+; includes QtWebEngine and QtWebChannel (Python 3.10+)
sudo apt install playerctl   # optional, for the Media plugin
```

## Use

```sh
# try a widget in a window (sizes: small 840x344, medium, large, xl 2536x696, full, or WxH)
python -m icuehost preview path/to/widget-folder-or.icuewidget --size small --settings

# see what the host made of a widget: controls, evaluated defaults, groups
python -m icuehost info path/to/widget

# list screens, then put a layout on the Edge full-screen
python -m icuehost screens
python -m icuehost run examples/layout.toml
```

`--lang de` and `--temp-unit °F` set what `iCUE.iCUELanguage` and
`iCUE.defaultTemperatureUnit()` report.

Settings and the per-instance `uniqueId` live in `~/.config/icue-linux/instances/`;
widget localStorage persists in `~/.config/icue-linux/webengine/`.

### Touch on the Edge

The Edge's touch panel is a standard HID device. Map it to the Edge output so touches land
there rather than on your main monitor:

- GNOME / KDE (Wayland): Settings → touchscreen → map to output
- X11: `xinput map-to-output "<touch device name>" <output>` (names from `xinput list` / `xrandr`)

Widgets with `"interactive": false` in their manifest ignore input, as on iCUE.

## Getting widgets

Copy an installed widget folder from a Windows machine running iCUE, or use the
`.icuewidget` file from the marketplace or from the widget's author. Widgets that bundle
Corsair's `common/plugins/*ApiWrapper.js` work unchanged as long as those files are in the
widget's own folder (Corsair's guidance anyway).

## Testing

```sh
QT_QPA_PLATFORM=offscreen python tests/smoke_test.py
```

This loads `examples/sysinfo-widget` against the fake sysfs tree in `tests/fakesys`. It checks
the event order, async plugin calls, signal delivery, live property updates, `tr()`,
localStorage and the `iCUE`/`device` globals from inside the page.

## Known gaps

- Corsair's built-in widgets may use undocumented internals; third-party widgets written to
  the spec are the target.
- The Edge's real device UUID isn't visible on Linux; `device.deviceId` is a fixed synthetic id.
- The XENEON EDGE "Custom Style" toggle (default colour scheme) isn't emulated; custom values
  always apply.
- The `media-selector` and `sensors-factory` controls are edited as text/JSON in the
  generated settings panel.
- The medium (1688x344) and large (840x696) slot sizes are derived from the documented small
  and XL sizes. Check them on the device.
