"""Pixel-exact duplicate detection between exported key frames."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image


def pixel_hash(path: Path) -> str:
    """SHA-1 of raw RGBA pixels; fully transparent pixels are normalised so their RGB doesn't matter."""
    arr = np.array(Image.open(path).convert("RGBA"))
    arr[arr[..., 3] == 0] = 0
    h = hashlib.sha1()
    h.update(f"{arr.shape}".encode())
    h.update(arr.tobytes())
    return h.hexdigest()


def assign_duplicates(keys) -> None:
    """Set `dup_of` on KeyFrame objects (first occurrence is the master)."""
    first: dict[str, int] = {}
    for k in sorted(keys, key=lambda k: k.idx):
        if k.hash in first:
            k.dup_of = first[k.hash]
        else:
            k.dup_of = None
            first[k.hash] = k.idx
