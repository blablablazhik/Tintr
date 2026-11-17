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


def view(base: Image.Image, box: tuple[int, int, int, int] | None = None,
         width: int = VLM_W) -> tuple[Image.Image, float, int, int]:
    """Crop `base` to `box` (None = whole frame) and scale it for the VLM.
    Whole frame: `width` wide. Crop: its larger side = `width` (at most MAX_UPSCALE x).
    Returns (image, scale, x0, y0)."""
    x0, y0 = (box[0], box[1]) if box else (0, 0)
    im = base.crop(box) if box else base
    W, H = im.size
    s = min(width / max(W, H), MAX_UPSCALE) if box else width / W
    return im.resize((max(1, round(W * s)), max(1, round(H * s))), Image.LANCZOS), s, x0, y0


def _overlaps(b, placed, gap: int = 2) -> bool:
    return any(b[0] < p[2] + gap and b[2] + gap > p[0] and b[1] < p[3] + gap and b[3] + gap > p[1] for p in placed)


def annotate(base: Image.Image, regions: Regions, width: int = VLM_W,
             box: tuple[int, int, int, int] | None = None, ui: float = 1.0) -> Image.Image:
    """Draw region numbers (yellow boxes) on `base` (full-res RGB), cropped to `box` and scaled (see view()).
    A number that doesn't fit inside its region, or would cover another one, is moved aside and
    pointed at its region with a leader line and a dot. `ui` scales labels (a bigger image that should
    look the same when fitted on screen, e.g. the zoomable preview)."""
    im, s, ox, oy = view(base, box, width)
    W, H = im.size
    d = ImageDraw.Draw(im)
    placed: list[tuple] = []  # label boxes and dots already on the image
    labels = []  # (i, font, centre, anchor point or None)
    # small regions first: they have the fewest good spots
    for i, (x, y, r) in sorted(regions.points.items(), key=lambda t: t[1][2]):
        f = _font(int(max(14 * ui, min(34 * ui, r * s * 0.9))))
        px, py, rs = (x - ox) * s, (y - oy) * s, r * s
        bb = d.textbbox((0, 0), str(i), font=f, anchor="mm")
        hw, hh = (bb[2] - bb[0]) / 2 + 3 * ui, (bb[3] - bb[1]) / 2 + 2 * ui
        fits = rs >= 0.8 * max(hw, hh)
        spot = None
        start = 0.0 if fits else rs + max(hw, hh) + 4 * ui
        for k in range(40):
            dist = start + k * 6 * ui
            angles = [0.0] if dist == 0 else [a * np.pi / 6 for a in range(12)]
            for a in angles:
                cx, cy = px + dist * np.cos(a), py - dist * np.sin(a)
                b = (cx - hw, cy - hh, cx + hw, cy + hh)
                if b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H or _overlaps(b, placed, round(2 * ui)):
                    continue
                spot = (cx, cy, b)
                break
            if spot:
                break
        if spot is None:  # crowded everywhere: put it on the region anyway
            spot = (px, py, (px - hw, py - hh, px + hw, py + hh))
        cx, cy, b = spot
        leader = np.hypot(cx - px, cy - py) > max(rs, 4 * ui)
        placed.append(b)
        if leader:
            placed.append((px - 4 * ui, py - 4 * ui, px + 4 * ui, py + 4 * ui))
        labels.append((i, f, (cx, cy), b, (px, py) if leader else None))
    for _, _, (cx, cy), _, pt in labels:
        if pt:
            d.line((pt, (cx, cy)), fill=(0, 0, 0), width=round(3 * ui))
            d.line((pt, (cx, cy)), fill=(255, 235, 0), width=max(1, round(ui)))
            rd = 3 * ui
            d.ellipse((pt[0] - rd, pt[1] - rd, pt[0] + rd, pt[1] + rd), fill=(255, 235, 0), outline=(0, 0, 0), width=max(1, round(ui)))
    for i, f, (cx, cy), b, _ in labels:
        d.rectangle(b, fill=(255, 235, 0), outline=(0, 0, 0), width=max(1, round(ui)))
        d.text((cx, cy), str(i), font=f, fill=(0, 0, 0), anchor="mm")
    return im


PREVIEW_W = 3072  # numbered preview in the UI: sharp when zoomed in, same look as the VLM image when fitted


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
_RULES = """Use "BG" for regions that are empty background outside the character (they stay transparent).
Small details not in the palette (mouth interior, eye details): pick the closest sensible palette colour.
Answer with ONLY a JSON object, no markdown, exactly in this form:
{"regions": {"1": {"part": "<short name>", "color": "C3"}, "2": {"part": "background", "color": "BG"}, ...}}
Include every number from 1 to %d."""

PROMPT_ANCHOR = """You are a professional animation ink-and-paint artist doing paint-bucket colouring.

Image 1: the line-art frame to colour (clean).
Image 2: the SAME frame with every paintable region labelled by a number in a yellow box.
Image 3: the character colour reference (colour model sheet).
Image 4: the allowed colour palette; each swatch is labelled C1, C2, ...

For EVERY numbered region in Image 2 decide which part of the character it is (skin, hair, hoodie,
inner shirt, background, ...) and assign exactly one palette colour that this part has in Image 3.
""" + _RULES

PROMPT_FOLLOW = """You are a professional animation ink-and-paint artist doing paint-bucket colouring of an animation.
An earlier key frame of the SAME character in the SAME shot was coloured and APPROVED by the art director.
Colour the new key frame so every part gets exactly the same colour as in the approved frame.

Image 1: APPROVED frame with its numbered regions.
Image 2: APPROVED frame as coloured (the ground truth for colours).
Image 3: NEW frame to colour (clean line-art).
Image 4: NEW frame with every paintable region labelled by a number in a yellow box.
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

            try:
                res = self.client.subscribe(ENDPOINT, arguments={
                    "model": self.model, "prompt": prompt, "image_urls": urls,
                    "temperature": 0, "max_tokens": 8000, "reasoning": reasoning,
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
            except ValueError as e:  # unparsable answer: retry, with reasoning on
                last = e
                if not reasoning:
                    reasoning = True
                    self.log(f"    {label}: відповідь не розібрано — повтор з reasoning")
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_auth_error(e):
                    raise AuthError(str(e)) from e
                if not reasoning and "reasoning is mandatory" in str(e).lower():
                    _REASONING_MANDATORY.add(self.model)  # e.g. gemini-3.8-flash: retry at once, remember
                    reasoning = True
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
        ann = annotate(clean, regions, box=box)
        urls = self._upload_all([(view(clean, box)[0], "clean.jpg"), (ann, "regions.jpg"),
                                 *self._ref_items(ref_path, pal)], label)
        return self._ask(PROMPT_ANCHOR % regions.n, urls, label, status)

    def color_follow(self, key_rgba, regions: Regions, anchor_rgba, anchor_regions: Regions,
                     anchor_mapping: dict, anchor_fill: Image.Image, ref_path: Path, pal: np.ndarray,
                     label: str = "", status=None):
        (status or (lambda _s: None))("завантаження")
        clean = _on_white(key_rgba)
        box = lines_box(key_rgba)
        ann = annotate(clean, regions, box=box)
        a_clean = _on_white(anchor_rgba)
        a_box = lines_box(anchor_rgba)
        a_ann = annotate(a_clean, anchor_regions, box=a_box)
        a_col = Image.new("RGBA", a_clean.size, (230, 230, 230, 255))
        a_col.alpha_composite(anchor_fill)
        a_col.alpha_composite(Image.fromarray(anchor_rgba, "RGBA"))
        a_col = view(a_col.convert("RGB"), a_box)[0]
        urls = self._upload_all([(a_ann, "anchor_regions.jpg"), (a_col, "anchor_color.jpg"),
                                 (view(clean, box)[0], "clean.jpg"), (ann, "regions.jpg"),
                                 *self._ref_items(ref_path, pal)], label)
        prompt = PROMPT_FOLLOW % (describe_mapping(anchor_mapping), regions.n)
        return self._ask(prompt, urls, label, status)
