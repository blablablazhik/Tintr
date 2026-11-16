"""All TVPaint access (pytvpaint over the tvpaint-rpc plugin).

TVPaint RPC is not thread-safe: call these functions from ONE worker thread only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tintr.naming import color_layer_name, fs_safe, is_tagged

Log = Callable[[str], None]


# pytvpaint connects at import time and retries for 60 s — disable that, we connect explicitly.
os.environ.setdefault("PYTVPAINT_WS_STARTUP_CONNECT", "0")


def connect(port: int) -> None:
    """Point pytvpaint at the given port and (re)connect."""
    import pytvpaint.george.client as c

    cl = c.rpc_client
    url = f"{c._rpc_host}:{port}"
    try:
        if cl.is_connected and cl.url == url:
            return
        if cl.is_connected:
            cl.disconnect()
        cl.url = url
        cl.timeout = 900  # socket timeout: saving a multi-GB project can take minutes
        cl.connect()
    except Exception as e:  # noqa: BLE001
        raise ConnectionError(
            f"Cannot connect to TVPaint on port {port}. Is TVPaint running with the tvpaint-rpc plugin? ({e})"
        ) from e


@dataclass
class ProjectInfo:
    name: str
    path: Path
    width: int
    height: int


@dataclass
class FoundLayer:
    clip_id: int
    clip_name: str
    clip_pos: int
    layer_id: int
    layer_name: str
    instances: list[tuple[int, int]]  # (start, length)

    @property
    def clip_folder(self) -> str:
        return f"c{self.clip_pos + 1:02d}_{fs_safe(self.clip_name)}"

    @property
    def layer_folder(self) -> str:
        return f"L{self.layer_id}_{fs_safe(self.layer_name)}"


def current_project() -> ProjectInfo:
    from pytvpaint.project import Project

    p = Project.current_project()
    return ProjectInfo(p.name, Path(p.path), p.width, p.height)


def scan(log: Log, only_clip: str | None = None) -> list[FoundLayer]:
    from pytvpaint.project import Project

    p = Project.current_project()
    found: list[FoundLayer] = []
    for pos, clip in enumerate(p.clips):
        if only_clip and clip.name != only_clip:
            continue
        for layer in clip.get_layers():
            if not (layer.is_anim_layer and is_tagged(layer.name)):
                continue
            insts = [(i.start, i.length) for i in layer.instances]
            found.append(FoundLayer(clip.id, clip.name, pos, layer.id, layer.name, insts))
            log(f"  clip #{pos + 1} '{clip.name}' → layer '{layer.name}': {len(insts)} keys")
    return found


def _clip_layer(clip_id: int, layer_id: int):
    from pytvpaint.clip import Clip
    from pytvpaint.layer import Layer
    from pytvpaint.project import Project

    clip = Clip(clip_id, Project.current_project())
    clip.make_current()
    return clip, Layer(layer_id, clip)


def export_key(clip_id: int, layer_id: int, frame: int, out: Path) -> Path:
    """Render a single frame of a single layer (full canvas, straight alpha)."""
    from pytvpaint import george

    clip, layer = _clip_layer(clip_id, layer_id)
    clip.current_frame = frame  # render_frame treats frame=0 as "current", so set it explicitly
    out.parent.mkdir(parents=True, exist_ok=True)
    layer.render_frame(out, frame=frame, alpha_mode=george.AlphaSaveMode.NO_PREMULTIPLY)
    if not out.exists():
        raise FileNotFoundError(f"TVPaint did not write {out}")
    return out


def export_layer_keys(clip_id: int, layer_id: int, frames: list[int], tmp_dir: Path) -> dict[int, Path]:
    """Render all instances of a layer in one TVPaint call (≈4x faster than frame by frame).

    Returns {frame: file}. Frames missing from the result should be rendered with export_key().
    """
    import re as _re

    from pytvpaint import george

    clip, layer = _clip_layer(clip_id, layer_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for f in tmp_dir.glob("k.*.png"):
        f.unlink()
    try:
        layer.render_instances(
            tmp_dir / "k.#.png",
            alpha_mode=george.AlphaSaveMode.NO_PREMULTIPLY,
            background_mode=george.BackgroundMode.NONE,
        )
    except Exception:  # noqa: BLE001 — fall back to per-frame export
        return {}
    out: dict[int, Path] = {}
    for f in tmp_dir.glob("k.*.png"):
        m = _re.fullmatch(r"k\.(-?\d+)\.png", f.name)
        if m and int(m.group(1)) in frames:
            out[int(m.group(1))] = f
    return out


def ensure_color_layer(clip_id: int, layer_id: int, existing_id: int | None, log: Log) -> int:
    """Create (or recreate) '<name>_color' directly below the tagged layer. Returns its id."""
    from pytvpaint.layer import Layer

    clip, src = _clip_layer(clip_id, layer_id)
    name = color_layer_name(src.name)
    for lay in clip.get_layers():
        if lay.id == existing_id or lay.name == name:
            log(f"  removing previous colour layer '{lay.name}'")
            lay.remove()
    clip.make_current()
    new = Layer.new_anim_layer(name, clip)
    new.position = src.position + 1
    log(f"  created layer '{new.name}' under '{src.name}'")
    return new.id


def load_frame(clip_id: int, layer_id: int, start: int, length: int, image: Path) -> None:
    from pytvpaint import george

    clip, layer = _clip_layer(clip_id, layer_id)
    clip.current_frame = start
    layer.make_current()
    if layer.get_instance(start) is None:
        layer.add_instance(start, nb_frames=length)
    clip.current_frame = start
    layer.make_current()
    george.tv_load_image(Path(image).as_posix(), False)
    inst = layer.get_instance(start, strict=True)
    if inst is not None and inst.length != length:
        inst.length = length


def save_project(path: Path | None) -> Path:
    from pytvpaint.project import Project

    p = Project.current_project()
    target = Path(path) if path else Path(p.path)
    p.save(target)
    return target
