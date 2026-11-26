"""All TVPaint access (pytvpaint over the tvpaint-rpc plugin).

TVPaint RPC is not thread-safe: call these functions from ONE worker thread only.

Hot paths (export, insert) use raw George calls with ids cached in the manifest. The pytvpaint
object API re-reads the project/clip on every call, iterates instances one RPC at a time and
toggles the visibility of every layer of the clip per render — that made export/insert slow.

Frames: values stored in the manifest are pytvpaint frames (= raw TVPaint image + project start
frame). Raw George calls take raw images, so convert with `_raw()` / `_ptv()`.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tintr.naming import color_layer_name, fs_safe, is_excluded

Log = Callable[[str], None]


# pytvpaint connects at import time and retries for 60 s — disable that, we connect explicitly.
os.environ.setdefault("PYTVPAINT_WS_STARTUP_CONNECT", "0")

_start_frame = 0  # project start frame, refreshed by current_project()


def _raw(frame: int) -> int:
    return frame - _start_frame


def _ptv(raw: int) -> int:
    return raw + _start_frame


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
    start_frame: int = 0


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
    global _start_frame
    from pytvpaint.project import Project

    p = Project.current_project()
    _start_frame = p.start_frame
    return ProjectInfo(p.name, Path(p.path), p.width, p.height, _start_frame)


def scan(log: Log, skip_ids: set[int] = frozenset()) -> list[FoundLayer]:
    """Full scan of every clip (slow on big projects) — only on first open or explicit rescan.

    Takes every animation layer without the `_cc-` tag, except Tintr's colour layers (by name or `skip_ids`)."""
    from pytvpaint.project import Project

    p = Project.current_project()
    found: list[FoundLayer] = []
    for pos, clip in enumerate(p.clips):
        for layer in clip.get_layers():
            if not layer.is_anim_layer or layer.id in skip_ids or is_excluded(layer.name):
                continue
            insts = [(i.start, i.length) for i in layer.instances]
            found.append(FoundLayer(clip.id, clip.name, pos, layer.id, layer.name, insts))
            log(f"  clip #{pos + 1} '{clip.name}' → layer '{layer.name}': {len(insts)} keys")
    return found


# ------------------------------------------------------------------ raw George helpers
def _select_clip(clip_id: int) -> None:
    from pytvpaint import george

    if george.tv_clip_current_id() != clip_id:
        george.tv_clip_select(clip_id)


def _clip_layer_ids() -> list[int]:
    """Layer ids of the current clip by stack position (one cheap call per layer)."""
    from pytvpaint import george

    ids: list[int] = []
    while True:
        try:
            ids.append(george.tv_layer_get_id(len(ids)))
        except Exception:  # noqa: BLE001 — GeorgeError past the last layer
            return ids


def layer_name(clip_id: int, layer_id: int) -> str | None:
    """Name of a cached layer, or None if it no longer exists. 2 RPC calls."""
    from pytvpaint import george

    try:
        _select_clip(clip_id)
        return george.tv_layer_info(layer_id).name
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ export
def export_clip_keys(clip_id: int, layer_ids: list[int], out_dir: Path) -> dict[int, list[tuple[int, int, Path]]]:
    """Export the key images of several layers of ONE clip with a single tv_ClipSaveStructure.

    Returns {layer_id: [(start, length, file), ...]} — instances re-read from TVPaint, so timing
    edits made since the scan are picked up.
    """
    from pytvpaint import george

    _select_clip(clip_id)
    ids = _clip_layer_ids()
    want = set(layer_ids)
    vis = {i: george.tv_layer_display_get(i) for i in ids}
    alpha = george.tv_alpha_save_mode_get()
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "structure.json"
    try:
        for i in ids:
            if vis[i] != (i in want):
                george.tv_layer_display_set(i, i in want)
        george.tv_alpha_save_mode_set(george.AlphaSaveMode.NO_PREMULTIPLY)
        george.tv_clip_save_structure_json(js, george.SaveFormat.PNG, fill_background=False,
                                           visible_layers_only=True, all_images=False)
    finally:
        george.tv_alpha_save_mode_set(alpha)
        for i in ids:
            if george.tv_layer_display_get(i) != vis[i]:
                george.tv_layer_display_set(i, vis[i])
    if not js.exists():
        raise FileNotFoundError("TVPaint did not write the clip structure")
    data = json.loads(js.read_text(encoding="utf-8"))
    out: dict[int, list[tuple[int, int, Path]]] = {}
    for lay in data["project"]["clip"]["layers"]:
        pos = int(lay["position"])
        lid = ids[pos] if 0 <= pos < len(ids) else None
        if lid not in want:
            continue
        links = sorted(lay.get("link", []), key=lambda d: d["instance-index"])
        end = int(lay["end"])
        insts = []
        for n, d in enumerate(links):
            s = int(d["instance-index"])
            nxt = int(links[n + 1]["instance-index"]) if n + 1 < len(links) else end + 1
            insts.append((_ptv(s), nxt - s, out_dir / d["file"]))
        out[lid] = insts
    return out


def _clip_layer(clip_id: int, layer_id: int):
    from pytvpaint.clip import Clip
    from pytvpaint.layer import Layer
    from pytvpaint.project import Project

    clip = Clip(clip_id, Project.current_project())
    clip.make_current()
    return clip, Layer(layer_id, clip)


def export_layer_keys_slow(clip_id: int, layer_id: int, frames: list[int], tmp_dir: Path) -> dict[int, Path]:
    """Fallback: per-frame render through pytvpaint (≈15x slower than export_clip_keys)."""
    from pytvpaint import george

    clip, layer = _clip_layer(clip_id, layer_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    for f in frames:
        p = tmp_dir / f"k.{f}.png"
        clip.current_frame = f  # render_frame treats frame=0 as "current", so set it explicitly
        layer.render_frame(p, frame=f, alpha_mode=george.AlphaSaveMode.NO_PREMULTIPLY)
        if p.exists():
            out[f] = p
    return out


# ------------------------------------------------------------------ insert
def _kill_color_layer(src_name: str, existing_id: int | None, ids: list[int], log: Log) -> None:
    from pytvpaint import george

    name = color_layer_name(src_name)
    for lid in ids:
        cur = george.tv_layer_info(lid).name
        if lid == existing_id or cur == name:
            log(f"  removing previous colour layer '{cur}'")
            george.tv_layer_kill(lid)


def insert_color_layer(clip_id: int, src_layer_id: int, existing_id: int | None,
                       keys: list[tuple[int, int, Path]], tmp_dir: Path, size: tuple[int, int], log: Log) -> int:
    """Create '<name>_color' under the coloured layer from (start, length, file) keys. Returns its id.

    One tv_LoadSequence (preloaded, so the temp files can go) + tv_ExposureSet per key, set from the
    last key backwards so the shift of later images doesn't move keys already timed.
    """
    from PIL import Image
    from pytvpaint import george

    keys = sorted(keys)
    _select_clip(clip_id)
    src_name = george.tv_layer_info(src_layer_id).name
    _kill_color_layer(src_name, existing_id, [i for i in _clip_layer_ids() if i != src_layer_id], log)

    # sequence: one image per key; gaps between keys become transparent images
    seq: list[tuple[Path | None, int]] = []
    for n, (s, ln, f) in enumerate(keys):
        seq.append((f, ln))
        nxt = keys[n + 1][0] if n + 1 < len(keys) else None
        if nxt is not None and s + ln < nxt:
            seq.append((None, nxt - s - ln))
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    blank = None
    for i, (f, _) in enumerate(seq):
        dst = tmp_dir / f"c.{i:04d}.png"
        if f is None:
            if blank is None:
                blank = tmp_dir / "_blank.png"
                Image.new("RGBA", size, (0, 0, 0, 0)).save(blank)
            shutil.copyfile(blank, dst)
        else:
            shutil.copyfile(f, dst)

    start = _raw(keys[0][0])
    george.tv_layer_set(src_layer_id)
    george.tv_layer_image(start)
    george.tv_load_sequence(tmp_dir / "c.0000.png", preload=True)
    new_id = george.tv_layer_current_id()
    first = george.tv_layer_info(new_id).first_frame
    for i in range(len(seq) - 1, -1, -1):
        george.tv_layer_image(first + i)
        george.tv_exposure_set(first + i, seq[i][1])
    if first != start:
        george.tv_layer_shift(new_id, start)
    name = color_layer_name(src_name)
    george.tv_layer_rename(new_id, name)
    george.tv_layer_set(new_id)
    george.tv_layer_move(george.tv_layer_get_pos(src_layer_id) + 1)  # TVPaint puts it at value-1
    shutil.rmtree(tmp_dir, ignore_errors=True)

    info = george.tv_layer_info(new_id)
    expect_last = start + sum(ln for _, ln in seq) - 1
    if info.first_frame != start or info.last_frame != expect_last:
        raise RuntimeError(f"colour layer timing mismatch: {info.first_frame}-{info.last_frame}, "
                           f"expected {start}-{expect_last}")
    log(f"  created layer '{name}' under '{src_name}' ({len(keys)} keys)")
    return new_id


def save_project(path: Path | None) -> Path:
    from pytvpaint.project import Project

    p = Project.current_project()
    target = Path(path) if path else Path(p.path)
    p.save(target)
    return target
