"""Tag detection and filesystem-safe naming.

Tintr colours every animation layer WITHOUT the `_cc-` / `_сс-` tag: the tag marks a layer to skip
(line-only, effects, BG…). The `<name>_color` layers Tintr creates are skipped too.

NOTE: the tvpaint-rpc plugin degrades non-ASCII characters to '?', so a layer named
"Anim_1_сс-" (Cyrillic) arrives as "Anim_1_??-". We accept Cyrillic, Latin and the
degraded '??' form, and we never write such names back to TVPaint.
"""

from __future__ import annotations

import re

TAG_RE = re.compile(r"_(сс|cc|\?\?)-", re.IGNORECASE)
_BAD_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def is_tagged(layer_name: str) -> bool:
    return bool(TAG_RE.search(layer_name))


_COLOR_RE = re.compile(r"_color(\s*\d+)?$", re.IGNORECASE)


def is_excluded(layer_name: str) -> bool:
    """Layers the scan skips: tagged `_cc-` ("don't colour") or a colour layer made by Tintr."""
    return is_tagged(layer_name) or bool(_COLOR_RE.search(layer_name.strip()))


def normalize_tag(name: str) -> str:
    """Replace any tag form with the latin `_cc-` (safe for FS and George)."""
    return TAG_RE.sub("_cc-", name)


def fs_safe(name: str) -> str:
    s = _BAD_FS.sub("_", normalize_tag(name)).strip(" .")
    return s or "unnamed"


def color_layer_name(layer_name: str) -> str:
    """Name of the colour layer created under a coloured layer, ASCII only."""
    base = TAG_RE.split(layer_name)[0] if TAG_RE.search(layer_name) else layer_name
    base = base.encode("ascii", "replace").decode("ascii").replace("?", "_").rstrip("_ -") or "layer"
    return f"{base}_color"


def key_basename(clip_folder: str, layer_folder: str, idx: int, start: int) -> str:
    return f"{clip_folder}__{layer_folder}__k{idx:03d}_f{start:04d}"
