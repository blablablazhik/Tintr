"""manifest.json — pipeline state for one TVPaint project, enables resume without paying twice.

Layout next to the project:
    <project_dir>/<project>_tintr/
        manifest.json
        <clip_folder>/<layer_folder>/
            keys/     exported key frames (RGBA line-art)
            regions/  key frames with numbered regions (what the VLM sees) + <key>.npz region maps
            color/    flat colour fills (what goes into TVPaint)
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from tintr.naming import fs_safe

# key status
EXPORTED = "exported"
COLORED = "colored"
INJECTED = "injected"
FAILED = "failed"


@dataclass
class KeyFrame:
    idx: int
    start: int
    length: int
    hash: str = ""
    dup_of: int | None = None  # idx of the master key with identical pixels
    key_file: str = ""  # paths relative to the layer dir
    regions_file: str = ""
    color_file: str = ""
    status: str = EXPORTED
    cost: float = 0.0
    error: str = ""
    # {"<region>": {"part": str, "color": "C<n>" | "BG"}}
    vlm_mapping: dict = field(default_factory=dict)
    vlm_anchor_ver: int = 0  # anchor version this key was coloured with
    # hand edits over the flat fill (drawing tools): survive refills and region recolouring
    paint_file: str = ""  # RGBA, hard-edged strokes (alpha 255 = paint)
    erase_file: str = ""  # RGBA, alpha 255 = erased to transparent


@dataclass
class LayerEntry:
    clip_id: int
    clip_name: str
    clip_pos: int
    layer_id: int
    layer_name: str
    clip_folder: str
    layer_folder: str
    ref_path: str = ""
    color_layer_id: int | None = None
    enabled: bool = True
    # anchor: key approved by the user, colour ground truth for the other keys of the layer
    anchor_idx: int | None = None
    anchor_approved: bool = False
    anchor_version: int = 0
    anchor_approved_mapping: dict = field(default_factory=dict)
    missing: bool = False  # cached layer not found in TVPaint any more (rescan needed)
    extra_colors: list = field(default_factory=list)  # "#RRGGBB" added by the user, after the ref palette
    # the reference with its parts labelled by the VLM: [{"name", "color", "x", "y"}] in reference pixels,
    # the drawn image (relative to the manifest root) and the reference mtime it was made from
    ref_labels: list = field(default_factory=list)
    ref_labeled_file: str = ""
    ref_labels_mtime: float = 0.0
    keys: list[KeyFrame] = field(default_factory=list)

    @property
    def uid(self) -> str:
        return f"{self.clip_id}:{self.layer_id}"

    def masters(self) -> list[KeyFrame]:
        return [k for k in self.keys if k.dup_of is None]

    def key(self, idx: int) -> KeyFrame:
        return next(k for k in self.keys if k.idx == idx)


def _known(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


class Manifest:
    def __init__(self, project_path: Path):
        self.project_path = Path(project_path)
        # versions saved by Tintr (<name>_tintr_v01.tvpp) share the original project's folder
        stem = re.sub(r"_tintr_v\d+$", "", self.project_path.stem)
        self.root = self.project_path.parent / f"{fs_safe(stem)}_tintr"
        self.file = self.root / "manifest.json"
        self.layers: dict[str, LayerEntry] = {}
        self.scanned_at = ""  # ISO time of the last full project scan
        self._lock = threading.RLock()

    # ---------- paths ----------
    def layer_dir(self, e: LayerEntry) -> Path:
        return self.root / e.clip_folder / e.layer_folder

    def sub(self, e: LayerEntry, kind: str) -> Path:
        d = self.layer_dir(e) / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def abs(self, e: LayerEntry, rel: str) -> Path:
        return self.layer_dir(e) / rel

    # ---------- io ----------
    @classmethod
    def load(cls, project_path: Path) -> "Manifest":
        m = cls(project_path)
        if m.file.exists():
            data = json.loads(m.file.read_text(encoding="utf-8"))
            m.scanned_at = data.get("scanned_at", "")
            for uid, le in data.get("layers", {}).items():
                keys = []
                for kd in le.pop("keys", []):
                    k = KeyFrame(**_known(KeyFrame, kd))
                    if k.status not in (EXPORTED, COLORED, INJECTED, FAILED):
                        k.status = EXPORTED  # state from removed modes
                    keys.append(k)
                m.layers[uid] = LayerEntry(**_known(LayerEntry, le), keys=keys)
        return m

    def save(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            data = {
                "project": str(self.project_path),
                "scanned_at": self.scanned_at,
                "layers": {uid: asdict(e) for uid, e in self.layers.items()},
            }
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.file)
