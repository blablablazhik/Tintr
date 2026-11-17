"""Character reference with its parts labelled (callouts on both sides).

Code splits the reference into flat colour patches (per palette colour, outlines and anti-aliasing
between them) and numbers them the same way as the regions of a frame. The VLM only names the numbered
patches, which it does reliably (it can not point at a part by coordinates). Code then places one label
per part (per side for paired parts) in the middle of its biggest patch and draws the callouts.
The labelled reference is what the follow keys are coloured with: the model finds the parts of a new
frame by the labels instead of by the anchor's region numbers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

from .palette import srgb_to_lab

FLAT_DE = 14       # a reference pixel "has" a palette colour within this ΔE
MAX_PATCHES = 60   # numbered patches shown to the model, the biggest
OUT_H = 1100       # height of the drawn character in the labelled image (px)
SKIP = "skip"

PROMPT = """You are an animation colour designer preparing a colour model sheet for ink-and-paint artists.

Image 1: the character colour reference (clean).
Image 2: the SAME reference with its flat colour patches marked (see MARKS below).

For EVERY numbered patch say which part of the character it is, the way a colourist names it on a model
sheet: hair, face skin, ear, neck, eye white, mouth, collar, shirt, tie, sleeve, hand, skirt, trousers,
sock, shoe, accessories and props by their names.
- English, 1-3 lowercase words.
- Patches of one part (split by lines, or seen on both sides of an arm) get the same name; paired parts
  (two hands, two shoes) too.
- Parts that look alike but are different things get different names ("face skin" / "hand" / "leg").
- Use "%s" for anything that is not a paintable part: outlines, shading or texture specks, background.

MARKS: each patch is tinted with its own identification colour and has a number in a box of that same
colour. A number placed outside its (small) patch is joined to it by a line of that colour ending in a dot
INSIDE the patch; follow the line to the dot. The tints are only for telling patches apart; the real
colours are in Image 1.

Answer with ONLY a JSON object, no markdown, exactly in this form:
{"regions": {"1": {"part": "hair"}, "2": {"part": "%s"}, ...}}
Include every number from 1 to %%d.""" % (SKIP, SKIP)


def _font(sz: int):
    for f in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, sz)
        except OSError:
            pass
    return ImageFont.load_default()


def load_rgb(ref_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """The reference on white + the mask of the character (non-transparent, not paper white)."""
    im = Image.open(ref_path).convert("RGBA")
    a = np.array(im)[..., 3]
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    rgb = np.array(bg.convert("RGB"))
    mask = (a > 16) & (rgb.min(axis=2) < 245)
    return rgb, mask


def character_box(mask: np.ndarray, pad: float = 0.03) -> tuple[int, int, int, int]:
    H, W = mask.shape
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return 0, 0, W, H
    p = int(pad * max(xs.max() - xs.min(), ys.max() - ys.min()))
    return (int(max(0, xs.min() - p)), int(max(0, ys.min() - p)),
            int(min(W, xs.max() + 1 + p)), int(min(H, ys.max() + 1 + p)))


def patches(rgb: np.ndarray, mask: np.ndarray, pal: np.ndarray):
    """Flat patches of the character, numbered by size (a vlm.Regions), and the palette index of each."""
    from .vlm import Regions
    lab = srgb_to_lab(rgb.reshape(-1, 3)).reshape(*rgb.shape[:2], 3).astype(np.float32)
    best = np.full(rgb.shape[:2], np.inf, np.float32)
    near = np.zeros(rgb.shape[:2], np.int16)
    for i, c in enumerate(srgb_to_lab(pal.reshape(-1, 3).astype(np.uint8)).astype(np.float32)):
        di = np.linalg.norm(lab - c, axis=2)
        upd = di < best
        best[upd], near[upd] = di[upd], i
    flat = (best < FLAT_DE) & mask
    min_px = max(40, int(0.00015 * mask.sum()))
    # opening drops thin structures (anti-aliasing along the outlines); for a near-black colour, which the
    # outlines themselves match, a wider one, scaled to the reference size
    r_ink = max(3, round(6 * max(rgb.shape[:2]) / 1600))
    pal_l = srgb_to_lab(pal.reshape(-1, 3).astype(np.uint8))[:, 0]
    comps = []  # (area, colour, label map of that colour, label)
    for i in range(len(pal)):
        r = r_ink if pal_l[i] < 30 else 3
        disk = np.hypot(*np.mgrid[-r:r + 1, -r:r + 1]) <= r + 0.2
        m = ndi.binary_opening(flat & (near == i), structure=disk)
        lb, k = ndi.label(m)
        if not k:
            continue
        areas = np.bincount(lb.ravel(), minlength=k + 1)
        for j in range(1, k + 1):
            if areas[j] >= min_px:
                comps.append((int(areas[j]), i, lb, j))
    comps.sort(key=lambda t: -t[0])
    comps = comps[:MAX_PATCHES]
    reg = np.zeros(rgb.shape[:2], np.int32)
    colour = {}
    for n, (_, i, lb, j) in enumerate(comps, 1):
        reg[lb == j] = n
        colour[n] = i
    pts = {}
    for n, sl in enumerate(ndi.find_objects(reg), 1):
        if sl is None:
            continue
        dt = ndi.distance_transform_edt(np.pad(reg[sl] == n, 1))[1:-1, 1:-1]
        y, x = np.unravel_index(dt.argmax(), dt.shape)
        pts[n] = (int(x + sl[1].start), int(y + sl[0].start), float(dt.max()))
    return Regions(reg, len(comps), pts), colour


def labels_from(regions, colour: dict, mask: np.ndarray, names: dict) -> list[dict]:
    """One label per named part in its biggest patch; a paired part also on the other side."""
    by_name: dict[str, list[int]] = {}
    for n in range(1, regions.n + 1):
        name = str((names.get(str(n)) or {}).get("part", "")).strip().lower()[:40]
        if name and name != SKIP and n in regions.points:
            by_name.setdefault(name, []).append(n)
    areas = np.bincount(regions.reg.ravel(), minlength=regions.n + 1)
    out = []
    for name, ns in by_name.items():
        ns.sort(key=lambda n: -areas[n])
        pick = [ns[0]]
        side = _left(mask, *regions.points[ns[0]][:2])
        other = [n for n in ns[1:] if _left(mask, *regions.points[n][:2]) != side and areas[n] >= 0.3 * areas[ns[0]]]
        pick += other[:1]
        for n in pick:
            x, y, _ = regions.points[n]
            out.append({"name": name, "color": f"C{colour[n] + 1}", "x": x, "y": y})
    return out


def _left(mask: np.ndarray, x: int, y: int) -> bool:
    """Left of the character's middle at that height (a head turned aside is not "right of the sheet")."""
    band = max(10, int(0.02 * mask.shape[0]))
    xs = np.nonzero(mask[max(0, y - band):y + band].any(axis=0))[0]
    return x < ((xs.min() + xs.max()) / 2 if len(xs) else mask.shape[1] / 2)


def render(rgb: np.ndarray, mask: np.ndarray, labels: list[dict]) -> Image.Image:
    """The character with callouts: names in columns left and right, leader lines to the points."""
    x0, y0, x1, y1 = character_box(mask, pad=0.02)
    s = OUT_H / max(1, y1 - y0)
    crop = Image.fromarray(rgb[y0:y1, x0:x1])
    crop = crop.resize((max(1, round(crop.width * s)), max(1, round(crop.height * s))), Image.LANCZOS)
    font = _font(26)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    col = max([probe.textlength(lb["name"], font=font) for lb in labels] + [120]) + 60
    W, H = crop.width, crop.height
    top = 20
    img = Image.new("RGB", (int(W + 2 * col), H + 2 * top), "white")
    img.paste(crop, (int(col), top))
    d = ImageDraw.Draw(img)
    pts = [(lb["name"], col + (lb["x"] - x0) * s, top + (lb["y"] - y0) * s, _left(mask, lb["x"], lb["y"])) for lb in labels]
    for left in (True, False):
        side = sorted([p for p in pts if p[3] == left], key=lambda p: p[2])
        last = -1e9
        rows = []
        for name, px, py, _ in side:  # keep the names apart, then pull them up if the column runs off the bottom
            ty = max(py, last + 36)
            rows.append([name, px, py, ty])
            last = ty
        over = last - (H + top - 10)
        if over > 0:
            for r in rows:
                r[3] -= over
            for i in range(len(rows) - 2, -1, -1):
                rows[i][3] = min(rows[i][3], rows[i + 1][3] - 36)
        for name, px, py, ty in rows:
            tw = d.textlength(name, font=font)
            if left:
                tx, lx = col - 30 - tw, col - 22
            else:
                tx, lx = col + W + 30, col + W + 22
            for wdt, c in ((5, "white"), (2, "black")):
                d.line([(lx, ty), (px, py)], fill=c, width=wdt)
            d.ellipse((px - 6, py - 6, px + 6, py + 6), fill="black", outline="white", width=2)
            d.text((tx, ty), name, font=font, fill="black", anchor="lm")
    return img
