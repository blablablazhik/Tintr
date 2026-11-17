"""VLM paint-bucket colouring (Gemini via fal `openrouter/router/vision`).

The VLM never draws. It sees the line-art with numbered regions plus the reference and the
palette, and answers which palette colour each region gets. Code fills the regions flat.
An approved "anchor" frame of the layer is sent with every later frame so that the same
part gets the same colour on every key.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import fal_client
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

ENDPOINT = "openrouter/router/vision"


class Cancelled(RuntimeError):
    pass


class AuthError(RuntimeError):
    pass


def _is_auth_error(e: Exception) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)
    msg = str(e).lower()
    return status in (401, 403) or "unauthorized" in msg or "invalid key" in msg or "authentication" in msg
VLM_W = 1536
BG = "BG"


# ------------------------------------------------------------------ regions
# v1: size threshold relative to the whole frame (small faces / eyes were dropped and bled colour)
# v2: multi-scale gap closing (trapped-ball style), absolute size threshold, fragments merged by contact
REGIONS_VER = 2


@dataclass
class Regions:
    reg: np.ndarray  # int32 map: 0 = line / gap / unnumbered, 1..n = numbered regions (by size)
    n: int
    points: dict[int, tuple[int, int, float]]  # label position (x, y, radius) in full-res pixels
    ver: int = REGIONS_VER  # algorithm version the map was made with (old maps keep old VLM answers valid)


def lines_box(key_rgba: np.ndarray, pad: float = 0.04) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1) of the drawing plus `pad` (share of its larger side, or pixels if >= 1)."""
    lines = key_rgba[..., 3] > 32
    ys, xs = np.flatnonzero(lines.any(1)), np.flatnonzero(lines.any(0))
    if not len(ys):
        return None
    H, W = lines.shape
    p = int(pad if pad >= 1 else pad * max(xs[-1] - xs[0], ys[-1] - ys[0]))
    return max(0, xs[0] - p), max(0, ys[0] - p), min(W, xs[-1] + 1 + p), min(H, ys[-1] + 1 + p)


def _disk(r: int) -> np.ndarray:
    return np.hypot(*np.ogrid[-r:r + 1, -r:r + 1]) <= r


def _regrow(lab: np.ndarray, free: np.ndarray, iters: int) -> None:
    """Grow labels into unlabelled free pixels, never across lines (geodesic dilation)."""
    for _ in range(iters):
        g = ndi.grey_dilation(lab, size=3)
        m = (lab == 0) & free & (g > 0)
        if not m.any():
            return
        lab[m] = g[m]


def _contacts(lab: np.ndarray) -> dict[int, dict[int, int]]:
    """Direct (4-neighbour) contact length between differently labelled pixels: {a: {b: n}}."""
    out: dict[int, dict[int, int]] = {}
    for a, b in ((lab[:, :-1], lab[:, 1:]), (lab[:-1, :], lab[1:, :])):
        m = (a != b) & (a > 0) & (b > 0)
        if not m.any():
            continue
        pa, pb = a[m].astype(np.int64), b[m].astype(np.int64)
        u, c = np.unique(np.concatenate([pa << 31 | pb, pb << 31 | pa]), return_counts=True)
        for k, n in zip(u.tolist(), c.tolist()):
            d = out.setdefault(k >> 31, {})
            d[k & 0x7FFFFFFF] = d.get(k & 0x7FFFFFFF, 0) + n
    return out


def find_regions(key_rgba: np.ndarray, gap_close_px: int = 3, min_px: int | None = None) -> Regions:
    """Paint-bucket regions of a line-art key.

    Multi-scale gap closing, like trapped-ball fill (BasicPBC / linefiller): a big "ball" first so small
    gaps in the lines don't leak, then smaller ones so narrow areas the big ball can't enter (eye whites,
    lens rims, strands of hair) still get their own region. After each pass the regions are regrown up to
    the lines. Later fragments that touch an earlier region directly were cut off only by the closing
    and are merged back into it. Pieces below `min_px` (scales with resolution) join a region they touch,
    else stay unnumbered (fill() gives them the nearest colour)."""
    lines_full = key_rgba[..., 3] > 32
    H, W = lines_full.shape
    if min_px is None:
        min_px = max(10, round(30 * (max(H, W) / 1920) ** 2))
    r = gap_close_px
    x0, y0, x1, y1 = lines_box(key_rgba, pad=r + 2) or (0, 0, W, H)
    lines = lines_full[y0:y1, x0:x1]
    free = ~lines
    lab = np.zeros(lines.shape, np.int32)
    stage = [0]  # pass index of every label (index = label)
    n = 0
    for si, rr in enumerate(sorted({r, r // 2, 0}, reverse=True)):
        barrier = ndi.binary_dilation(lines, structure=_disk(rr)) if rr > 0 else lines
        l2, k = ndi.label(free & (lab == 0) & ~barrier)
        if k:
            lab[l2 > 0] = l2[l2 > 0] + n
            n += k
            stage += [si] * k
        _regrow(lab, free, rr + 1)
    if n == 0:
        return Regions(np.zeros((H, W), np.int32), 0, {})

    # fragments of later passes → the earlier region they touch most
    stage_a = np.array(stage)
    parent = list(range(n + 1))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    con = _contacts(lab)
    for f in sorted((i for i in range(1, n + 1) if stage_a[i] > 0), key=lambda i: stage_a[i]):
        nb = {j: c for j, c in con.get(f, {}).items() if stage_a[j] < stage_a[f]}
        if nb:
            parent[root(f)] = root(max(nb, key=nb.get))
    lab = np.array([root(i) for i in range(n + 1)], np.int32)[lab]

    # tiny pieces → a region they touch directly, else unnumbered
    sizes = np.bincount(lab.ravel(), minlength=n + 1).astype(np.int64)
    small = [i for i in range(1, n + 1) if 0 < sizes[i] < min_px]
    if small:
        con = _contacts(lab)
        merge = np.arange(n + 1, dtype=np.int32)
        for f in small:
            nb = {j: c for j, c in con.get(f, {}).items() if sizes[j] >= min_px}
            merge[f] = max(nb, key=nb.get) if nb else 0
        lab = merge[lab]
        sizes = np.bincount(lab.ravel(), minlength=n + 1).astype(np.int64)

    # everything outside the box is the outer background: same label as the box border
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    border = border[border > 0]
    outer = int(np.bincount(border).argmax()) if len(border) else 0
    if outer:
        sizes[outer] += H * W - lab.size
    sizes[0] = 0
    big = [i for i in np.argsort(-sizes, kind="stable") if i > 0 and sizes[i] >= min_px]
    remap = np.zeros(n + 1, np.int32)
    for new, old in enumerate(big, 1):
        remap[old] = new
    rc = remap[lab]
    reg = np.zeros((H, W), np.int32)
    if outer and remap[outer]:
        reg[:] = remap[outer]
    reg[y0:y1, x0:x1] = rc
    pts = {}
    for i, sl in enumerate(ndi.find_objects(rc), 1):
        if sl is None:
            continue
        m = rc[sl] == i
        dt = ndi.distance_transform_edt(np.pad(m, 1))[1:-1, 1:-1]
        y, x = np.unravel_index(dt.argmax(), dt.shape)
        pts[i] = (int(x + sl[1].start + x0), int(y + sl[0].start + y0), float(dt.max()))
    return Regions(reg, len(big), pts)


def save_regions(regions: Regions, path: Path, tag: str) -> None:
    """Cache a region map on disk (find_regions takes seconds on a 3K frame). `tag` = key pixel hash."""
    pts = np.array([(i, x, y, r) for i, (x, y, r) in regions.points.items()], np.float64).reshape(-1, 4)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez_compressed(tmp, reg=regions.reg.astype(np.int32), n=regions.n, pts=pts, tag=tag, ver=regions.ver)
    tmp.replace(path)


def load_regions(path: Path, tag: str) -> Regions | None:
    try:
        with np.load(path) as d:
            if str(d["tag"]) != tag:
                return None
            pts = {int(i): (int(x), int(y), float(r)) for i, x, y, r in d["pts"]}
            ver = int(d["ver"]) if "ver" in d.files else 1
            return Regions(d["reg"], int(d["n"]), pts, ver)
    except Exception:  # noqa: BLE001 — missing / stale / corrupt cache
        return None


def _font(sz: int):
    for f in ("arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(f, sz)
        except OSError:
            pass
    return ImageFont.load_default()


def _on_white(key_rgba: np.ndarray) -> Image.Image:
    im = Image.new("RGBA", (key_rgba.shape[1], key_rgba.shape[0]), "white")
    im.alpha_composite(Image.fromarray(key_rgba, "RGBA"))
    return im.convert("RGB")


MAX_UPSCALE = 3.0


LABEL_MARGIN = 90  # px each side of a cropped VLM view, for the callout columns (at ui=1)


def view(base: Image.Image, box: tuple[int, int, int, int] | None = None,
         width: int = VLM_W, margin: int = 0) -> tuple[Image.Image, float, float, int]:
    """Crop `base` to `box` (None = whole frame) and scale it for the VLM.
    Whole frame: `width` wide. Crop: its larger side = `width` (at most MAX_UPSCALE x), plus `margin`
    white px left and right (room for callout numbers). Returns (image, scale, x0, y0) with
    image x = (frame x - x0) * scale."""
    x0, y0 = (box[0], box[1]) if box else (0, 0)
    im = base.crop(box) if box else base
    W, H = im.size
    s = min(width / max(W, H), MAX_UPSCALE) if box else width / W
    im = im.resize((max(1, round(W * s)), max(1, round(H * s))), Image.LANCZOS)
    if margin:
        pad = Image.new("RGB", (im.width + 2 * margin, im.height), "white")
        pad.paste(im, (margin, 0))
        im, x0 = pad, x0 - margin / s
    return im, s, x0, y0


def _overlaps(b, placed, gap: float = 2) -> bool:
    return any(b[0] < p[2] + gap and b[2] + gap > p[0] and b[1] < p[3] + gap and b[3] + gap > p[1] for p in placed)


def _ccw(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _seg_cross(p1, p2, q1, q2) -> bool:
    """Segments p1-p2 and q1-q2 cross (touching at an end does not count)."""
    d1, d2, d3, d4 = _ccw(q1, q2, p1), _ccw(q1, q2, p2), _ccw(p1, p2, q1), _ccw(p1, p2, q2)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def _seg_hits_box(p1, p2, b, gap: float = 0) -> bool:
    """Segment p1-p2 passes through rectangle b (x0, y0, x1, y1) grown by `gap`."""
    x0, y0, x1, y1 = b[0] - gap, b[1] - gap, b[2] + gap, b[3] + gap
    inside = lambda q: x0 <= q[0] <= x1 and y0 <= q[1] <= y1  # noqa: E731
    if inside(p1) or inside(p2):
        return True
    c = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return any(_seg_cross(p1, p2, c[k], c[(k + 1) % 4]) for k in range(4))


def mark_colors(n: int) -> np.ndarray:
    """Distinct identification colours for regions 1..n (index 0 unused): golden-ratio hues."""
    out = np.zeros((n + 1, 3), np.uint8)
    for i in range(1, n + 1):
        h = (i * 0.61803398875) % 1.0
        s, v = (0.75, 0.95) if i % 2 else (0.55, 0.85)
        k = np.array([5, 3, 1]) + h * 6
        rgb = v - v * s * np.clip(np.minimum(k % 6, 4 - k % 6), 0, 1)
        out[i] = (rgb * 255).round()
    return out


def _background_ids(regions: Regions) -> np.ndarray:
    """Bool per region id: True for the outer background (regions touching the frame edge)."""
    reg = regions.reg
    edge = np.concatenate([reg[0], reg[-1], reg[:, 0], reg[:, -1]])
    bg = np.zeros(regions.n + 1, bool)
    bg[0] = True
    bg[np.unique(edge[edge > 0])] = True
    return bg


def _tint(base: Image.Image, regions: Regions, cols: np.ndarray, alpha: float = 0.38) -> Image.Image:
    """Tint every region except the outer background with its identification colour (Set-of-Mark)."""
    reg = regions.reg
    m = (~_background_ids(regions))[reg]
    a = np.array(base, np.float32)
    a[m] = a[m] * (1 - alpha) + cols[reg[m]].astype(np.float32) * alpha
    return Image.fromarray(a.round().astype(np.uint8), "RGB")


def _callout_slots(regions: Regions, bg: np.ndarray, big: list[int], s: float, ox: float, oy: float, size,
                   hwm: float, hhm: float, gap: float, taken: list, inner_cost: float) -> tuple[np.ndarray, np.ndarray]:
    """Free spots for callout numbers: on the background just outside the character's silhouette, or
    (at an extra cost, for regions deep inside the figure) well inside a big region that has its own
    number. Thinned to one per label-sized cell, never on another number.
    Returns (n, 2) label centres in image px and the extra cost of each spot."""
    W, H = size
    x1, y1 = ox + W / s, oy + H / s
    rim = Image.fromarray(regions.reg.astype(np.int32), "I").crop((round(ox), round(oy), round(x1), round(y1)))
    reg = np.array(rim.resize((W, H), Image.NEAREST))
    fg = ~bg[reg] | (reg == 0)  # character + its lines
    rr = float(np.hypot(hwm, hhm) + gap)
    d_out = ndi.distance_transform_edt(~fg)
    out_band = (d_out >= rr) & (d_out <= rr + 4 * hhm)  # background right next to the silhouette
    d_in = ndi.distance_transform_edt(np.isin(reg, big))  # big regions, away from their lines
    inner = d_in >= rr + gap
    cw, ch = 2 * hwm + gap, 2 * hhm + gap
    pts, extra = [], []
    for mask, add in ((out_band, 0.0), (inner, inner_cost)):
        ys, xs = np.nonzero(mask)
        if not len(xs):
            continue
        cell = (ys // ch).astype(np.int64) * 100000 + (xs // cw).astype(np.int64)
        _, first = np.unique(cell, return_index=True)
        pts += list(zip(xs[first], ys[first]))
        extra += [add] * len(first)
    kept: list[tuple] = []
    out, cost = [], []
    for (x, y), add in zip(pts, extra):
        b = (x - hwm, y - hhm, x + hwm, y + hhm)
        if b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H or _overlaps(b, kept, gap) or _overlaps(b, taken, 2 * hhm + gap):
            continue
        kept.append(b)
        out.append((x, y))
        cost.append(add)
    return np.array(out, float).reshape(-1, 2), np.array(cost, float)


def annotate(base: Image.Image, regions: Regions, width: int = VLM_W,
             box: tuple[int, int, int, int] | None = None, ui: float = 1.0, margin: int = 0) -> Image.Image:
    """Set-of-Mark view of `base` (full-res RGB), cropped to `box` and scaled (see view()).

    Every region is tinted with its own identification colour. A region big enough for its number gets
    it inside, in a box of that colour. Smaller regions get a callout: the number stands in a column left
    or right of the drawing, on the background, joined by a line of the region colour to a dot inside
    the region. Numbers are assigned to column slots by minimum total line length, which guarantees that
    callout lines never cross. `ui` scales labels (a bigger image that should look the same when fitted
    on screen, e.g. the zoomable preview); `margin` adds white columns for the callouts (cropped views)."""
    cols = mark_colors(regions.n)
    bg = _background_ids(regions)
    im, s, ox, oy = view(_tint(base, regions, cols), box, width, margin)
    W, H = im.size
    d = ImageDraw.Draw(im)
    gap = 2 * ui

    labels = []  # (i, font, (cx, cy), box, (px, py) dot or None)
    boxes: list[tuple] = []
    callouts = []  # (i, font, hw, hh, px, py)
    for i, (x, y, r) in sorted(regions.points.items(), key=lambda t: -t[1][2]):  # big first: inside
        f = _font(int(max(14 * ui, min(34 * ui, r * s * 0.9))))
        px, py, rs = (x - ox) * s, (y - oy) * s, r * s
        bb = d.textbbox((0, 0), str(i), font=f, anchor="mm")
        hw, hh = (bb[2] - bb[0]) / 2 + 3 * ui, (bb[3] - bb[1]) / 2 + 2 * ui
        b = (px - hw, py - hh, px + hw, py + hh)
        if (rs >= 0.8 * max(hw, hh) or bg[i]) and not _overlaps(b, boxes, gap):
            boxes.append(b)
            labels.append((i, f, (px, py), b, None))
        else:
            fc = _font(int(14 * ui))  # callout numbers: uniform small size
            bb = d.textbbox((0, 0), str(i), font=fc, anchor="mm")
            callouts.append((i, fc, (bb[2] - bb[0]) / 2 + 3 * ui, (bb[3] - bb[1]) / 2 + 2 * ui, px, py))

    if callouts:
        hwm = max(c[2] for c in callouts)
        hhm = max(c[3] for c in callouts)
        big = [lab[0] for lab in labels if not bg[lab[0]]]
        slots, slot_extra = _callout_slots(regions, bg, big, s, ox, oy, (W, H), hwm, hhm, gap, boxes, 160 * ui)
        from scipy.optimize import linear_sum_assignment
        got: dict[int, int] = {}
        if len(slots) >= len(callouts):
            P = np.array([(c[4], c[5]) for c in callouts])
            cost = np.hypot(slots[None, :, 0] - P[:, 0, None], slots[None, :, 1] - P[:, 1, None]) + slot_extra[None, :]
            # a line through another number or another callout's dot is confusing: heavy penalty
            near = 0.5 * float(np.hypot(W, H))
            dots = [(x - 5 * ui, y - 5 * ui, x + 5 * ui, y + 5 * ui) for x, y in P]
            for n, (px, py) in enumerate(P):
                for j in np.flatnonzero(cost[n] < near):
                    sx, sy = slots[j]
                    obst = boxes + [o for k, o in enumerate(dots) if k != n]
                    hits = sum(_seg_hits_box((px, py), (sx, sy), o, gap) for o in obst)
                    cost[n, j] += hits * near
                cost[n, cost[n] >= near] += 10 * near
            ri, si = linear_sum_assignment(cost)
            got = dict(zip(ri.tolist(), si.tolist()))
            # untangle: two crossing lines swap their slots (shorter in total), until none cross
            for _ in range(200):
                swapped = False
                keys = list(got)
                for a_ in range(len(keys)):
                    for b_ in range(a_ + 1, len(keys)):
                        u, v = keys[a_], keys[b_]
                        if _seg_cross(P[u], slots[got[u]], P[v], slots[got[v]]):
                            got[u], got[v] = got[v], got[u]
                            swapped = True
                if not swapped:
                    break
        for n, (i, fc, hw, hh, px, py) in enumerate(callouts):
            if n not in got:  # no free slot left: number on the region itself
                labels.append((i, fc, (px, py), (px - hw, py - hh, px + hw, py + hh), None))
                continue
            cx, cy = (float(v) for v in slots[got[n]])
            b = (cx - hw, cy - hh, cx + hw, cy + hh)
            # the line ends at the box edge facing the region
            end = (min(max(px, b[0]), b[2]), min(max(py, b[1]), b[3]))
            labels.append((i, fc, (cx, cy), b, (px, py), end))

    lw = max(1, round(ui))
    for lab in labels:  # callout lines under all number boxes
        if lab[4] is None:
            continue
        i, (px, py), end = lab[0], lab[4], lab[5]
        c = tuple(int(v) for v in cols[i])
        d.line(((px, py), end), fill=(0, 0, 0), width=max(2, round(3 * ui)))
        d.line(((px, py), end), fill=c, width=max(1, round(1.5 * ui)))
        rd = 3.5 * ui
        d.ellipse((px - rd, py - rd, px + rd, py + rd), fill=c, outline=(0, 0, 0), width=lw)
    for lab in labels:
        i, f, (cx, cy), b = lab[:4]
        c = cols[i].astype(float)
        inkc = (0, 0, 0) if c @ (0.299, 0.587, 0.114) > 140 else (255, 255, 255)  # readable on the box colour
        d.rectangle(b, fill=tuple(int(v) for v in c), outline=(0, 0, 0), width=lw)
        d.text((cx, cy), str(i), font=f, fill=inkc, anchor="mm")
    return im


# ---- zoomed details of dense spots (anchor). Tested on the Anim_2 anchor with Opus 5.5: details cut tight
# fixed the mouth but lost context (ear read as hair, tie end as shirt); with >= 1/3 of the figure around them
# and a matching frame on the main view: 40.7/42 vs 37.3 without details.
FRAME_COLS = [(230, 30, 30), (20, 90, 230), (200, 0, 200)]
FRAME_NAMES = ["red", "blue", "magenta"]
DETAIL_W = 1024


def detail_boxes(regions: Regions, box: tuple[int, int, int, int], max_n: int = 3) -> list[tuple[int, int, int, int]]:
    """Areas (full-res px) to show zoomed: clusters of >= 2 regions too small for their number in the main view,
    each grown to >= 35% of the figure with room around it, largest clusters first."""
    fig = max(box[2] - box[0], box[3] - box[1])
    s = min(VLM_W / fig, MAX_UPSCALE)
    small = [i for i, (x, y, r) in regions.points.items() if r * s < 22]
    if len(small) < 2:
        return []
    pts = np.array([regions.points[i][:2] for i in small], float)
    link = 0.045 * fig
    lab = -np.ones(len(small), int)
    n_cl = 0
    for i in range(len(small)):  # single-linkage clusters
        if lab[i] >= 0:
            continue
        stack, lab[i] = [i], n_cl
        while stack:
            j = stack.pop()
            near = np.flatnonzero((np.hypot(*(pts - pts[j]).T) < link) & (lab < 0))
            lab[near] = n_cl
            stack += near.tolist()
        n_cl += 1
    sl = ndi.find_objects(regions.reg)
    H, W = regions.reg.shape
    found = []
    for k in range(n_cl):
        mem = [small[i] for i in np.flatnonzero(lab == k)]
        if len(mem) < 2:
            continue
        y0 = min(sl[i - 1][0].start for i in mem); y1 = max(sl[i - 1][0].stop for i in mem)
        x0 = min(sl[i - 1][1].start for i in mem); x1 = max(sl[i - 1][1].stop for i in mem)
        pad = 0.15 * max(x1 - x0, y1 - y0) + 12
        found.append((len(mem), (int(max(0, x0 - pad)), int(max(0, y0 - pad)), int(x1 + pad), int(y1 + pad))))
    out = []
    for _, b in sorted(found, reverse=True)[:max_n]:  # biggest clusters first; keep what surrounds the spot
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        half = max(0.35 * fig, 1.4 * max(b[2] - b[0], b[3] - b[1])) / 2
        out.append((int(max(0, cx - half)), int(max(0, cy - half)), int(min(W, cx + half)), int(min(H, cy + half))))
    return out


def framed_annotate(base: Image.Image, regions: Regions, box, details: list) -> Image.Image:
    """The main numbered view with a coloured frame around each detail area."""
    im = annotate(base, regions, box=box, margin=LABEL_MARGIN)
    _, s, ox, oy = view(base, box, margin=LABEL_MARGIN)
    d = ImageDraw.Draw(im)
    for b, c in zip(details, FRAME_COLS):
        d.rectangle(((b[0] - ox) * s, (b[1] - oy) * s, (b[2] - ox) * s, (b[3] - oy) * s), outline=c, width=5)
    return im


def detail_image(base: Image.Image, regions: Regions, b, color) -> Image.Image:
    """Zoomed detail: only regions whose number point lies inside it are numbered; border in the frame colour."""
    from PIL import ImageOps
    sub = Regions(regions.reg, regions.n, {i: p for i, p in regions.points.items() if b[0] <= p[0] < b[2] and b[1] <= p[1] < b[3]})
    return ImageOps.expand(annotate(base, sub, width=DETAIL_W, box=b, margin=LABEL_MARGIN // 2), border=10, fill=color)


PREVIEW_W = 3072  # numbered preview in the UI: sharp when zoomed in, same look as the VLM image when fitted
PREVIEW_VER = "3"  # bump when the look of the marks changes: older previews are redrawn on connect


def save_preview(im: Image.Image, path: Path) -> None:
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("tintr-preview", PREVIEW_VER)
    im.save(path, pnginfo=info)


def preview_current(path: Path, frame_w: int) -> bool:
    try:
        with Image.open(path) as im:
            return im.width == max(VLM_W, min(PREVIEW_W, frame_w)) and im.text.get("tintr-preview") == PREVIEW_VER
    except OSError:
        return False


def preview(base: Image.Image, regions: Regions) -> Image.Image:
    """Whole-frame numbered regions for the UI (click coordinates map to the frame)."""
    w = max(VLM_W, min(PREVIEW_W, base.width))
    return annotate(base, regions, width=w, ui=w / VLM_W)


def palette_card(pal: np.ndarray) -> Image.Image:
    sw, pad = 120, 20
    im = Image.new("RGB", (pad + len(pal) * (sw + pad), sw + 70), (128, 128, 128))
    d = ImageDraw.Draw(im)
    for i, c in enumerate(pal, 1):
        x = pad + (i - 1) * (sw + pad)
        d.rectangle((x, pad, x + sw, pad + sw - 20), fill=tuple(int(v) for v in c), outline=(0, 0, 0))
        d.text((x + sw / 2, pad + sw + 10), f"C{i}", font=_font(28), fill=(0, 0, 0), anchor="mm")
    return im


def fill(regions: Regions, mapping: dict, pal: np.ndarray) -> Image.Image:
    """Flat fill from {"<region>": {"part":..., "color": "C3"|"BG"}}; gaps/lines take the nearest region."""
    reg = regions.reg
    H, W = reg.shape
    col = np.zeros((H, W, 3), np.uint8)
    alpha = np.zeros((H, W), np.uint8)
    known = reg > 0
    for i in range(1, regions.n + 1):
        c = str((mapping.get(str(i)) or {}).get("color", BG)).upper()
        if c.startswith("C") and c[1:].isdigit() and 1 <= int(c[1:]) <= len(pal):
            m = reg == i
            col[m] = pal[int(c[1:]) - 1]
            alpha[m] = 255
    if (~known).any() and known.any():
        _, (iy, ix) = ndi.distance_transform_edt(~known, return_indices=True)
        miss = ~known
        col[miss] = col[iy[miss], ix[miss]]
        alpha[miss] = alpha[iy[miss], ix[miss]]
    return Image.fromarray(np.dstack([col, alpha]), "RGBA")


# ------------------------------------------------------------------ prompts
_MARKS = """MARKS: each region is tinted with its own identification colour and has a number in a box of that
same colour. A number placed outside its (small) region is joined to it by a line of that colour ending
in a dot INSIDE the region; follow the line to the dot. The tints are ONLY for telling regions apart,
they are NOT the colours of the character: take the real colours from the reference and the palette.
"""

# colourist's rules for the anchor (where the model decides every part alone)
_CRAFT = """
Ink-and-paint rules (read the frame like a colourist):
- One part is often split into several regions by lines (an ear, a glasses rim, a sleeve, a collar): every
  piece gets the colour of that part.
- A part covered by an arm, a hand or hair continues on the other side: a piece of the tie seen below or
  beside an arm is still the tie, not the shirt.
- An open mouth usually has upper/lower teeth (white), a tongue and a dark mouth cavity; check which
  number sits in which of them.
- Before answering, go through the small regions one by one: where exactly is the dot/number, what
  surrounds it, and which part of the character is it.
"""

_RULES = _MARKS + """Use "BG" for regions that are empty background outside the character (they stay transparent).
Small details not in the palette (mouth interior, eye details): pick the closest sensible palette colour.
Answer with ONLY a JSON object, no markdown, exactly in this form:
{"regions": {"1": {"part": "<short name>", "color": "C3"}, "2": {"part": "background", "color": "BG"}, ...}}
Include every number from 1 to %d."""

def anchor_prompt(n_regions: int, n_details: int = 0) -> str:
    """Anchor prompt; with n_details zoomed details (Images 3..) framed in matching colours on Image 2."""
    imgs = ["Image 1: the line-art frame to colour (clean).",
            "Image 2: the SAME frame with every paintable region marked (see MARKS below)."
            + (" Coloured rectangles show where the zoomed details below come from." if n_details else "")]
    for z in range(n_details):
        imgs.append(f"Image {3 + z}: ZOOMED detail of the area inside the {FRAME_NAMES[z]} rectangle of Image 2 (its border "
                    "has the same colour), with the same numbers. Use it to read small regions exactly; use Image 2 to "
                    "see what surrounds the area (which part of the body it belongs to).")
    k = 3 + n_details
    imgs.append(f"Image {k}: the character colour reference (colour model sheet).")
    imgs.append(f"Image {k + 1}: the allowed colour palette; each swatch is labelled C1, C2, ...")
    return ("You are a professional animation ink-and-paint artist doing paint-bucket colouring.\n\n" + "\n".join(imgs)
            + "\n\nFor EVERY numbered region in Image 2 decide which part of the character it is (skin, hair, hoodie,\n"
              "inner shirt, background, ...) and assign exactly one palette colour that this part has in the reference.\n"
            + _CRAFT + _RULES % n_regions)

PROMPT_FOLLOW = """You are a professional animation ink-and-paint artist doing paint-bucket colouring of an animation.
An earlier key frame of the SAME character in the SAME shot was coloured and APPROVED by the art director.
Colour the new key frame so every part gets exactly the same colour as in the approved frame.

Image 1: APPROVED frame with its regions marked (see MARKS below).
Image 2: APPROVED frame as coloured (the ground truth for colours).
Image 3: NEW frame to colour (clean line-art).
Image 4: NEW frame with every paintable region marked (see MARKS below).
Image 5: the character colour reference (colour model sheet).
Image 6: the allowed colour palette; each swatch is labelled C1, C2, ...

Approved frame region assignments (region: part = colour):
%s

For EVERY numbered region of the NEW frame (Image 4) find the matching part in the approved frame and use
the same colour. Numbers of the new frame are NOT the same as numbers of the approved frame; match by
what the part is, its position and shape. Only if a part does not exist in the approved frame, use Image 5.
""" + _RULES


def describe_mapping(mapping: dict) -> str:
    def k(x):
        return int(x) if str(x).isdigit() else 0
    return "\n".join(f"{r}: {v.get('part', '?')} = {v.get('color', '?')}" for r, v in sorted(mapping.items(), key=lambda t: k(t[0])))


def parse_mapping(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError(f"VLM returned no JSON: {text[:300]!r}")
    data = json.loads(m.group(0))
    return data.get("regions", data)


# ------------------------------------------------------------------ client
# max_tokens includes the hidden reasoning tokens: with reasoning a model can spend most of a small budget
# thinking and the JSON gets cut off (gemini-3.8-flash: 7.2k of 8k on 42 regions). A cut-off answer is retried
# with twice the budget, up to the cap.
MAX_TOKENS = 8000
MAX_TOKENS_REASONING = 32000
MAX_TOKENS_CAP = 64000
URL_TTL = 12 * 3600  # reuse uploaded reference/palette URLs across sessions for this long
_REASONING_MANDATORY: set[str] = set()  # models fal refuses to run with reasoning off


class VLMColorizer:
    def __init__(self, api_key: str, model: str, cancel: threading.Event | None = None,
                 reasoning: bool = False, log: Callable[[str], None] | None = None,
                 url_cache: Path | None = None):
        if not api_key:
            raise AuthError("fal.ai API key is not set (Settings).")
        self.client = fal_client.SyncClient(key=api_key, default_timeout=180.0)
        self.model = model
        self.reasoning = reasoning
        self.last: dict = {}  # metrics of the last successful request (time, tokens, cost)
        self.cancel = cancel or threading.Event()
        self.log = log or (lambda _s: None)
        self._url_cache_file = url_cache
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6)
        self._load_url_cache()

    def _load_url_cache(self) -> None:
        if not (self._url_cache_file and self._url_cache_file.exists()):
            return
        try:
            data = json.loads(self._url_cache_file.read_text(encoding="utf-8"))
            now = time.time()
            self._cache = {k: v["url"] for k, v in data.items() if now - v.get("t", 0) < URL_TTL}
        except Exception:  # noqa: BLE001
            self._cache = {}

    def _store_url(self, key: str, url: str) -> None:
        if not self._url_cache_file:
            return
        try:
            f = self._url_cache_file
            data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            data[key] = {"url": url, "t": time.time()}
            f.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001 — the cache is an optimisation only
            pass

    def check_key(self) -> None:
        """Cheap auth check: upload a 1x1 png to fal storage (free)."""
        try:
            self._upload(Image.new("RGB", (1, 1), "white"), "tintr_check.png")
        except Exception as e:  # noqa: BLE001
            if _is_auth_error(e):
                raise AuthError(f"fal.ai rejected the API key: {e}") from e
            raise

    def _upload(self, im: Image.Image, name: str, cache_key: str | None = None) -> str:
        if cache_key:
            with self._lock:
                if cache_key in self._cache:
                    return self._cache[cache_key]
        buf = io.BytesIO()
        if name.endswith(".jpg"):  # line-art on white: several times smaller than PNG
            im.convert("RGB").save(buf, "JPEG", quality=92)
            mime = "image/jpeg"
        else:
            im.save(buf, "PNG")
            mime = "image/png"
        url = self.client.upload(buf.getvalue(), mime, file_name=name)
        if cache_key:
            with self._lock:
                self._cache[cache_key] = url
                self._store_url(cache_key, url)
        return url

    def _upload_all(self, items: list, label: str = "") -> list[str]:
        """Upload in parallel; an item is (image, name[, cache_key]) or an already known URL."""
        t = time.time()
        futs = [None if isinstance(it, str) else self._pool.submit(self._upload, *it) for it in items]
        urls = [it if f is None else f.result() for it, f in zip(items, futs)]
        self.log(f"    {label}: upload {time.time() - t:.1f}s")
        return urls

    def _ask(self, prompt: str, urls: list[str], label: str = "", status=None) -> tuple[dict, float, str]:
        status = status or (lambda _s: None)
        last = None
        cost_total = 0.0
        reasoning = self.reasoning or self.model in _REASONING_MANDATORY
        budget = MAX_TOKENS_REASONING if reasoning else MAX_TOKENS
        for attempt in range(4):
            if self.cancel.is_set():
                raise Cancelled()
            t0 = time.time()
            started: list[float] = []
            queued: list[int] = []

            def on_update(st, _started=started, _queued=queued):
                if isinstance(st, fal_client.Queued) and st.position > 0 and st.position not in _queued:
                    _queued.append(st.position)
                    self.log(f"    {label}: у черзі fal #{st.position}")
                    status(f"черга fal #{st.position}")
                elif isinstance(st, fal_client.InProgress) and not _started:
                    _started.append(time.time())
                    status("генерується")

            tok = None
            try:
                res = self.client.subscribe(ENDPOINT, arguments={
                    "model": self.model, "prompt": prompt, "image_urls": urls,
                    "temperature": 0, "max_tokens": budget, "reasoning": reasoning,
                }, client_timeout=600, on_queue_update=on_update)
                usage = res.get("usage") or {}
                cost_total += float(usage.get("cost") or 0)
                now = time.time()
                q = (started[0] - t0) if started else 0.0
                tok = usage.get("completion_tokens")
                self.log(f"    {label}: VLM {now - t0:.1f}s (черга {q:.1f}s, генерація {now - t0 - q:.1f}s"
                         + (f", {tok} tok" if tok else "") + (", reasoning" if reasoning else "") + ")")
                self.last = {"seconds": now - t0, "queue": q, "tokens": tok, "prompt_tokens": usage.get("prompt_tokens"),
                             "reasoning": reasoning, "cost": cost_total, "attempts": attempt + 1}
                return parse_mapping(res.get("output", "")), cost_total, res.get("output", "")
            except ValueError as e:  # unparsable answer
                last = e
                if tok and tok >= budget - 64 and budget < MAX_TOKENS_CAP:  # cut off by the token limit
                    budget = min(MAX_TOKENS_CAP, budget * 2)
                    self.log(f"    {label}: відповідь обрізано лімітом токенів — повтор з лімітом {budget}")
                elif not reasoning:  # garbled answer: retry with reasoning on
                    reasoning = True
                    budget = max(budget, MAX_TOKENS_REASONING)
                    self.log(f"    {label}: відповідь не розібрано — повтор з reasoning")
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_auth_error(e):
                    raise AuthError(str(e)) from e
                if not reasoning and "reasoning is mandatory" in str(e).lower():
                    _REASONING_MANDATORY.add(self.model)  # e.g. gemini-3.8-flash: retry at once, remember
                    reasoning = True
                    budget = max(budget, MAX_TOKENS_REASONING)
                    self.log(f"    {label}: модель {self.model} працює лише з reasoning — повтор")
                    continue
                time.sleep(3 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _ref_items(self, ref_path: Path, pal: np.ndarray) -> list:
        """Reference + palette upload items; URLs already uploaded (also in earlier sessions) are reused."""
        key = f"{ref_path}:{ref_path.stat().st_mtime}"
        out = []
        for ck, name in (("ref:" + key, "ref.png"),
                         ("pal:" + key + ":" + hashlib.sha1(pal.tobytes()).hexdigest()[:12], "palette.png")):
            with self._lock:
                url = self._cache.get(ck)
            if url:
                out.append(url)
            elif name == "ref.png":
                ref = Image.open(ref_path).convert("RGB")
                ref.thumbnail((1024, 1024))
                out.append((ref, name, ck))
            else:
                out.append((palette_card(pal), name, ck))
        return out

    def color_anchor(self, key_rgba, regions: Regions, ref_path: Path, pal: np.ndarray, label: str = "",
                     status=None):
        (status or (lambda _s: None))("завантаження")
        clean = _on_white(key_rgba)
        box = lines_box(key_rgba)  # the VLM sees the drawing cropped and enlarged, not the whole frame
        details = detail_boxes(regions, box) if box else []  # dense spots, zoomed with their surroundings
        ann = framed_annotate(clean, regions, box, details)
        items = [(view(clean, box, margin=LABEL_MARGIN)[0], "clean.jpg"), (ann, "regions.jpg")]
        items += [(detail_image(clean, regions, b, c), f"detail{n}.jpg") for n, (b, c) in enumerate(zip(details, FRAME_COLS), 1)]
        urls = self._upload_all(items + self._ref_items(ref_path, pal), label)
        return self._ask(anchor_prompt(regions.n, len(details)), urls, label, status)

    def color_follow(self, key_rgba, regions: Regions, anchor_rgba, anchor_regions: Regions,
                     anchor_mapping: dict, anchor_fill: Image.Image, ref_path: Path, pal: np.ndarray,
                     label: str = "", status=None):
        (status or (lambda _s: None))("завантаження")
        clean = _on_white(key_rgba)
        box = lines_box(key_rgba)
        ann = annotate(clean, regions, box=box, margin=LABEL_MARGIN)
        a_clean = _on_white(anchor_rgba)
        a_box = lines_box(anchor_rgba)
        a_ann = annotate(a_clean, anchor_regions, box=a_box, margin=LABEL_MARGIN)
        a_col = Image.new("RGBA", a_clean.size, (230, 230, 230, 255))
        a_col.alpha_composite(anchor_fill)
        a_col.alpha_composite(Image.fromarray(anchor_rgba, "RGBA"))
        a_col = view(a_col.convert("RGB"), a_box, margin=LABEL_MARGIN)[0]
        urls = self._upload_all([(a_ann, "anchor_regions.jpg"), (a_col, "anchor_color.jpg"),
                                 (view(clean, box, margin=LABEL_MARGIN)[0], "clean.jpg"), (ann, "regions.jpg"),
                                 *self._ref_items(ref_path, pal)], label)
        prompt = PROMPT_FOLLOW % (describe_mapping(anchor_mapping), regions.n)
        return self._ask(prompt, urls, label, status)
