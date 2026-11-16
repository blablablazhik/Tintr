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
@dataclass
class Regions:
    reg: np.ndarray  # int32 map: 0 = line / gap / unnumbered, 1..n = numbered regions (by size)
    n: int
    points: dict[int, tuple[int, int, float]]  # label position (x, y, radius) in full-res pixels


def find_regions(key_rgba: np.ndarray, gap_close_px: int = 3, min_share: float = 0.0004) -> Regions:
    lines = key_rgba[..., 3] > 32
    r = gap_close_px
    disk = np.hypot(*np.ogrid[-r:r + 1, -r:r + 1]) <= r
    closed = ndi.binary_dilation(lines, structure=disk) if r > 0 else lines
    lab, n = ndi.label(~closed)
    if n == 0:
        return Regions(np.zeros(lines.shape, np.int32), 0, {})
    sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    big = [i + 1 for i in np.argsort(-sizes) if sizes[i] >= min_share * lab.size]
    remap = np.zeros(n + 1, np.int32)
    for new, old in enumerate(big, 1):
        remap[old] = new
    reg = remap[lab]
    pts = {}
    for i, sl in enumerate(ndi.find_objects(reg), 1):
        if sl is None:
            continue
        m = reg[sl] == i
        dt = ndi.distance_transform_edt(np.pad(m, 1))[1:-1, 1:-1]
        y, x = np.unravel_index(dt.argmax(), dt.shape)
        pts[i] = (int(x + sl[1].start), int(y + sl[0].start), float(dt.max()))
    return Regions(reg, len(big), pts)


def save_regions(regions: Regions, path: Path, tag: str) -> None:
    """Cache a region map on disk (find_regions takes seconds on a 3K frame). `tag` = key pixel hash."""
    pts = np.array([(i, x, y, r) for i, (x, y, r) in regions.points.items()], np.float64).reshape(-1, 4)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez_compressed(tmp, reg=regions.reg.astype(np.int32), n=regions.n, pts=pts, tag=tag)
    tmp.replace(path)


def load_regions(path: Path, tag: str) -> Regions | None:
    try:
        with np.load(path) as d:
            if str(d["tag"]) != tag:
                return None
            pts = {int(i): (int(x), int(y), float(r)) for i, x, y, r in d["pts"]}
            return Regions(d["reg"], int(d["n"]), pts)
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


def annotate(base: Image.Image, regions: Regions, width: int = VLM_W) -> Image.Image:
    """Draw region numbers (yellow boxes) on `base` (full-res RGB), scaled to `width`."""
    W, H = base.size
    s = width / W
    im = base.resize((width, int(H * s)), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for i, (x, y, r) in regions.points.items():
        f = _font(int(max(16, min(34, r * s * 0.9))))
        cx, cy = x * s, y * s
        bb = d.textbbox((cx, cy), str(i), font=f, anchor="mm")
        d.rectangle((bb[0] - 3, bb[1] - 2, bb[2] + 3, bb[3] + 2), fill=(255, 235, 0), outline=(0, 0, 0))
        d.text((cx, cy), str(i), font=f, fill=(0, 0, 0), anchor="mm")
    return im


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
        ann = annotate(clean, regions)
        urls = self._upload_all([(clean.resize(ann.size, Image.LANCZOS), "clean.jpg"), (ann, "regions.jpg"),
                                 *self._ref_items(ref_path, pal)], label)
        return self._ask(PROMPT_ANCHOR % regions.n, urls, label, status)

    def color_follow(self, key_rgba, regions: Regions, anchor_rgba, anchor_regions: Regions,
                     anchor_mapping: dict, anchor_fill: Image.Image, ref_path: Path, pal: np.ndarray,
                     label: str = "", status=None):
        (status or (lambda _s: None))("завантаження")
        clean = _on_white(key_rgba)
        ann = annotate(clean, regions)
        a_clean = _on_white(anchor_rgba)
        a_ann = annotate(a_clean, anchor_regions)
        a_col = Image.new("RGBA", a_clean.size, (230, 230, 230, 255))
        a_col.alpha_composite(anchor_fill)
        a_col.alpha_composite(Image.fromarray(anchor_rgba, "RGBA"))
        a_col = a_col.convert("RGB").resize(a_ann.size, Image.LANCZOS)
        urls = self._upload_all([(a_ann, "anchor_regions.jpg"), (a_col, "anchor_color.jpg"),
                                 (clean.resize(ann.size, Image.LANCZOS), "clean.jpg"), (ann, "regions.jpg"),
                                 *self._ref_items(ref_path, pal)], label)
        prompt = PROMPT_FOLLOW % (describe_mapping(anchor_mapping), regions.n)
        return self._ask(prompt, urls, label, status)
