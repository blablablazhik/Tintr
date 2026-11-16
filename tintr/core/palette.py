"""Flat colour palette of the character reference (the only colours Tintr ever paints with)."""

from __future__ import annotations

import warnings
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.cluster.vq import kmeans2


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


@lru_cache(maxsize=16)
def _palette_cached(path: str, mtime: float, k: int) -> np.ndarray:
    im = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    rgb = np.array(bg.convert("RGB"))
    # only pixels inside flat areas (skip outlines, anti-aliasing, texture edges)
    f = rgb.astype(np.float32)
    var = sum(ndi.uniform_filter(f[..., i] ** 2, 5) - ndi.uniform_filter(f[..., i], 5) ** 2 for i in range(3))
    px = rgb[var < 60]
    if len(px) > 200_000:
        px = px[np.random.default_rng(0).choice(len(px), 200_000, replace=False)]
    lab = srgb_to_lab(px)
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        _, lbl = kmeans2(lab, k, seed=0, minit="++")
    counts = np.bincount(lbl, minlength=k)
    cols = [np.median(px[lbl == i], axis=0) for i in np.argsort(-counts) if counts[i] >= 0.0004 * len(px)]
    keep: list[np.ndarray] = []  # merge near-identical colours (ΔE < 6)
    for c in cols:
        if all(np.linalg.norm(srgb_to_lab(c[None])[0] - srgb_to_lab(q[None])[0]) > 6 for q in keep):
            keep.append(c)
    return np.array(keep).round().astype(np.uint8)


def reference_palette(ref_path: Path, k: int = 24) -> np.ndarray:
    p = Path(ref_path)
    return _palette_cached(str(p), p.stat().st_mtime, k)
