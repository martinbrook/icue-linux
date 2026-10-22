"""Load an iCUE widget package and parse its declarative parts.

Everything here follows the public iCUE Widget Specification (API 1.4.0):
manifest.json, <title>, <link rel=icon>, x-icue-* meta tags, the
x-icue-groups JSON block and translation.json.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


@dataclass
class Control:
    name: str                      # content="..." -> JS global name
    type: str                      # data-type
    attrs: dict[str, str]          # every data-* attribute except data-type (raw expressions)


@dataclass
class PluginRef:
    module: str                    # widgetbuilder.sensorsdataprovider
    plugin: str                    # Sensors
    version: str                   # 1.0

    @property
    def key(self) -> str:
        """Name used for window.plugins.<key>, plugin<key>Events, plugin<key>_initialized."""
        short = self.module.split(".")[-1]
        return short[:1].upper() + short[1:]


@dataclass
class Widget:
    root: Path
    manifest: dict
    title_expr: str = ""
    icon: str | None = None
    group_expr: str | None = None
    preview: str | None = None
    controls: list[Control] = field(default_factory=list)
    groups_raw: list = field(default_factory=list)
    translations: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.manifest.get("id", self.root.name)

    @property
    def index(self) -> Path:
        return self.root / "index.html"

    @property
    def plugins(self) -> list[PluginRef]:
        refs = []
        for spec in self.manifest.get("required_plugins", []) or []:
            parts = spec.split(":")
            parts += [""] * (3 - len(parts))
            refs.append(PluginRef(parts[0], parts[1], parts[2]))
        return refs

    @property
    def modules(self) -> list[Path]:
        return [self.root / m for m in self.manifest.get("modules", []) or []]

    @property
    def interactive(self) -> bool:
        return bool(self.manifest.get("interactive", False))


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[Control] = []
        self.title = ""
        self.icon = None
        self.group = None
        self.preview = None
        self.groups_json = ""
        self._in_title = False
        self._in_groups = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "link" and "icon" in a.get("rel", "").split():
            self.icon = a.get("href")
        elif tag == "meta":
            name = a.get("name", "")
            if name == "x-icue-property":
                attrs_d = {k[5:]: v for k, v in a.items()
                           if k.startswith("data-") and k != "data-type"}
                self.controls.append(Control(a.get("content", ""), a.get("data-type", ""), attrs_d))
            elif name == "x-icue-widget-group":
                self.group = a.get("content")
            elif name == "x-icue-widget-preview":
                self.preview = a.get("content")
        elif tag == "script" and a.get("id") == "x-icue-groups":
            self._in_groups = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_groups = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_groups:
            self.groups_json += data


def _loose_json(text: str):
    """x-icue-groups examples in the docs contain trailing commas and missing
    commas between keys; accept what iCUE evidently accepts."""
    text = text.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r",\s*([\]}])", r"\1", text)                 # trailing commas
        fixed = re.sub(r'(\]|"|\d|true|false)\s*\n(\s*")', r"\1,\n\2", fixed)  # missing commas
        return json.loads(fixed)


def unpack(package: Path, dest_root: Path | None = None) -> Path:
    """Unpack a .icuewidget (zip) and return the folder containing manifest.json."""
    dest_root = dest_root or Path(tempfile.mkdtemp(prefix="icuewidget-"))
    with zipfile.ZipFile(package) as z:
        for member in z.namelist():                     # refuse path traversal
            target = (dest_root / member).resolve()
            if not str(target).startswith(str(dest_root.resolve())):
                raise ValueError(f"unsafe path in package: {member}")
        z.extractall(dest_root)
    manifests = sorted(dest_root.rglob("manifest.json"), key=lambda p: len(p.parts))
    if not manifests:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise FileNotFoundError("no manifest.json in package")
    return manifests[0].parent


def load(path: str | Path) -> Widget:
    path = Path(path).expanduser()
    if path.is_file() and path.suffix in (".icuewidget", ".zip"):
        path = unpack(path)
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    html = (path / "index.html").read_text(encoding="utf-8")
    head = re.split(r"<body[\s>]", html, maxsplit=1, flags=re.I)[0]
    p = _HeadParser()
    p.feed(head)
    tr_path = path / "translation.json"
    translations = json.loads(tr_path.read_text(encoding="utf-8")) if tr_path.exists() else {}
    return Widget(
        root=path,
        manifest=manifest,
        title_expr=p.title.strip(),
        icon=p.icon,
        group_expr=p.group,
        preview=p.preview,
        controls=p.controls,
        groups_raw=_loose_json(p.groups_json),
        translations=translations,
    )
