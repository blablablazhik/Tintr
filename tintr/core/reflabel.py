"""Character reference with its parts labelled (callouts on both sides).

A VLM names the parts of the reference, gives each one its palette colour and a rough point on it
(on a coordinate grid). Code then moves every point into the middle of the nearest patch of that colour,
so a label never points at a line or at a neighbouring part, and draws the callouts itself.
The labelled reference is what the follow keys are coloured with: the model finds the parts of a new
frame by the labels instead of by the anchor's region numbers.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi

from .palette import srgb_to_lab

GRID_W = 1024      # longest side of the gridded image the VLM sees
GRID_MARGIN = 44   # px around it for the tick numbers
SNAP_DE = 14       # a reference pixel "has" a palette colour within this ΔE
OUT_H = 1100       # height of the drawn character in the labelled image (px)

PROMPT = """You are an animation colour designer preparing a colour model sheet for ink-and-paint artists.

Image 1: the character colour reference. A coordinate grid is drawn over it: x goes from 0 (left) to 1000
(right), y from 0 (top) to 1000 (bottom); the numbers are on the edges.
Image 2: the allowed colour palette; each swatch is labelled C1, C2, ...

Label every distinct paintable part of the character that a colourist has to tell apart, as on a model
sheet: hair, face skin, ear, eye white, mouth / teeth, eyebrows if coloured, every piece of clothing
(collar, shirt, tie, sleeve, skirt, trousers, sock, shoe ...), hands, accessories and props.
- Name each part in English, 1-3 lowercase words, the way a colourist would (e.g. "hair", "face skin",
  "shirt", "shoe sole").
- Parts that look alike but have a different colour are separate parts. Paired parts (two hands, two
  legs, two shoes) may be labelled once on each side.
- For each part give a point INSIDE it, in the middle of an area of the part's colour, away from lines
  and from other parts, and the palette colour of that part.
- 8 to 25 parts. Do not label the background.

Answer with ONLY a JSON object, no markdown, exactly in this form:
{"parts": [{"name": "hair", "color": "C3", "x": 512, "y": 140}, ...]}"""


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


def grid_image(rgb: np.ndarray, box: tuple[int, int, int, int]) -> Image.Image:
    """The character cropped and enlarged, a light grid every 100 units and the numbers in the margin."""
    x0, y0, x1, y1 = box
    crop = Image.fromarray(rgb[y0:y1, x0:x1])
    s = GRID_W / max(crop.size)
    crop = crop.resize((max(1, round(crop.width * s)), max(1, round(crop.height * s))), Image.LANCZOS)
    w, h = crop.size
    m = GRID_MARGIN
    im = Image.new("RGB", (w + 2 * m, h + 2 * m), "white")
    im.paste(crop, (m, m))
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    f = _font(15)
    for v in range(0, 1001, 100):
        x, y = m + v * w / 1000, m + v * h / 1000
        d.line([(x, m), (x, m + h)], fill=(0, 120, 255, 70 if v % 500 else 120), width=1)
        d.line([(m, y), (m + w, y)], fill=(0, 120, 255, 70 if v % 500 else 120), width=1)
        for tx, ty in ((x, m / 2), (x, m + h + m / 2)):
            d.text((tx, ty), str(v), font=f, fill=(0, 90, 200, 255), anchor="mm")
        for tx, ty in ((m / 2, y), (m + w + m / 2, y)):
            d.text((tx, ty), str(v), font=f, fill=(0, 90, 200, 255), anchor="mm")
    im = im.convert("RGBA")
    im.alpha_composite(over)
    return im.convert("RGB")


def snap(rgb: np.ndarray, mask: np.ndarray, box, pal: np.ndarray, parts: list) -> list[dict]:
    """VLM answer (grid units) → labels in reference pixels, each moved into its colour's patch."""
    x0, y0, x1, y1 = box
    st = max(1, int(np.ceil(max(x1 - x0, y1 - y0) / 800)))  # work on ≤800 px: plenty for placing a dot
    sub = rgb[y0:y1:st, x0:x1:st]
    inside = mask[y0:y1:st, x0:x1:st]
    h, w = inside.shape
    lab = srgb_to_lab(sub.reshape(-1, 3)).reshape(h, w, 3).astype(np.float32)
    best = np.full((h, w), np.inf, np.float32)
    near = np.zeros((h, w), np.int16)
    for i, c in enumerate(srgb_to_lab(pal.reshape(-1, 3).astype(np.uint8)).astype(np.float32)):
        di = np.linalg.norm(lab - c, axis=2)
        upd = di < best
        best[upd], near[upd] = di[upd], i
    flat = best < SNAP_DE
    edt: dict[int, np.ndarray] = {}
    radius = 0.07 * max(w, h)
    yy, xx = np.mgrid[0:h, 0:w]
    out = []
    for p in parts:
        try:
            name = str(p["name"]).strip()[:40]
            px, py = float(p["x"]) / 1000 * w, float(p["y"]) / 1000 * h
        except (KeyError, TypeError, ValueError):
            continue
        if not name:
            continue
        c = str(p.get("color", "")).upper()
        ci = int(c[1:]) - 1 if c.startswith("C") and c[1:].isdigit() and 1 <= int(c[1:]) <= len(pal) else -1
        px, py = min(max(px, 0), w - 1), min(max(py, 0), h - 1)
        if ci >= 0:
            if ci not in edt:
                edt[ci] = ndi.distance_transform_edt((near == ci) & flat & inside)
            e = edt[ci]
            d = np.hypot(xx - px, yy - py)
            # deep inside the patch (up to 12 px counts), close to the model's point
            score = np.minimum(e, 12 / st) - 0.12 * d
            score[(e < 1.5) | (d > radius)] = -np.inf
            if np.isfinite(score).any():
                py, px = np.unravel_index(int(np.argmax(score)), score.shape)
        out.append({"name": name, "color": c if ci >= 0 else "", "x": int(px * st) + x0, "y": int(py * st) + y0})
    return out


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
    band = max(10, int(0.04 * (y1 - y0)))

    def left_side(x: int, y: int) -> bool:
        # left of the character's middle at that height (a head turned aside is not "right of the sheet")
        xs = np.nonzero(mask[max(0, y - band):y + band].any(axis=0))[0]
        mid = (xs.min() + xs.max()) / 2 if len(xs) else (x0 + x1) / 2
        return x < mid

    pts = [(lb["name"], col + (lb["x"] - x0) * s, top + (lb["y"] - y0) * s, left_side(lb["x"], lb["y"])) for lb in labels]
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
