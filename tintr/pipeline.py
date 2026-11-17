"""Pipeline orchestration — UI independent. TVPaint calls happen on the calling thread only.

connect (cached scan) → export keys (+ regions, free) → VLM colours the anchor → user approves →
VLM colours the other keys following the anchor → insert colour layers into TVPaint and save.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from tintr.config import Config, app_dir, get_api_key
from tintr.core import manifest as M
from tintr.core import vlm as V
from tintr.core.cost import CostTracker
from tintr.core.dedupe import assign_duplicates, pixel_hash
from tintr.core.palette import reference_palette
from tintr.core.vlm import AuthError, Cancelled
from tintr.naming import key_basename
from tintr.tvp import bridge

Log = Callable[[str], None]
Progress = Callable[[int, int, str], None]


class Pipeline:
    def __init__(self, cfg: Config, log: Log, progress: Progress | None = None):
        self.cfg = cfg
        self.log = log
        self.progress = progress or (lambda *_: None)
        self.cost = CostTracker()
        self.cancel = threading.Event()
        self.project: bridge.ProjectInfo | None = None
        self.manifest: M.Manifest | None = None
        self._reg_cache: dict = {}
        # live per-key status for the UI: (layer uid, key idx, text or "" when finished)
        self.key_status: Callable[[str, int, str], None] = lambda *_: None

    # ------------------------------------------------------------------ connect / scan
    def connect(self, rescan: bool = False) -> list[M.LayerEntry]:
        """Connect and load the layer list from manifest.json. The full project scan runs only on request."""
        t = time.time()
        bridge.connect(self.cfg.rpc_port)
        self.project = bridge.current_project()
        self.log(f"Project: {self.project.path}  ({self.project.width}x{self.project.height})")
        self.manifest = M.Manifest.load(self.project.path)
        if rescan:
            return self.rescan()
        if not self.manifest.layers:
            self.log("Цей проект ще не скановано — натисніть «Сканувати проект».")
            return []
        missing = []
        for e in self.manifest.layers.values():
            e.missing = bridge.layer_name(e.clip_id, e.layer_id) is None
            if e.missing:
                missing.append(e.layer_name)
        self._restore_outputs()
        self.manifest.save()
        when = self.manifest.scanned_at.replace("T", " ")[:16]
        self.log(f"З кешу: {len(self.manifest.layers)} шар(ів)" + (f", скан від {when}" if when else "")
                 + f" ({time.time() - t:.1f}s). Нові шари з тегом — «Пересканувати».")
        if missing:
            self.log("  ⚠ не знайдено в TVPaint: " + ", ".join(missing) + " — потрібне пересканування")
        return list(self.manifest.layers.values())

    def rescan(self) -> list[M.LayerEntry]:
        t = time.time()
        self.log("Scanning all clips for '_сс-' / '_cc-' layers…")
        found = bridge.scan(self.log)
        old = self.manifest.layers
        new: dict[str, M.LayerEntry] = {}
        for f in found:
            uid = f"{f.clip_id}:{f.layer_id}"
            prev = old.get(uid)
            e = M.LayerEntry(
                clip_id=f.clip_id, clip_name=f.clip_name, clip_pos=f.clip_pos,
                layer_id=f.layer_id, layer_name=f.layer_name,
                clip_folder=f.clip_folder, layer_folder=f.layer_folder,
            )
            if prev:  # keep everything the user already decided / paid for
                for attr in ("ref_path", "color_layer_id", "enabled", "anchor_idx", "anchor_approved",
                             "anchor_version", "anchor_approved_mapping"):
                    setattr(e, attr, getattr(prev, attr))
                e.keys = prev.keys
            self._merge_keys(e, f.instances)
            new[uid] = e
        self.manifest.layers = new
        self.manifest.scanned_at = datetime.now().isoformat(timespec="seconds")
        self._restore_outputs()
        self.manifest.save()
        self.log(f"Found {len(new)} tagged layer(s) in {time.time() - t:.1f}s.")
        return list(new.values())

    @staticmethod
    def _merge_keys(e: M.LayerEntry, instances: list[tuple[int, int]]) -> None:
        """Re-time the layer's keys from TVPaint instances, keeping per-key state matched by start frame."""
        prev = {k.start: k for k in e.keys}
        keys = []
        for i, (start, length) in enumerate(instances, 1):
            k = prev.get(start) or M.KeyFrame(idx=i, start=start, length=length, status="")
            k.idx, k.length = i, length
            keys.append(k)
        e.keys = keys

    def _refresh_previews(self) -> None:
        """Redraw numbered previews made at an older (lower) resolution, so they stay sharp when zoomed."""
        m = self.manifest
        for e in self.manifest.layers.values():
            for k in e.masters():
                if not (k.key_file and k.regions_file and m.abs(e, k.key_file).exists()):
                    continue
                p = m.abs(e, k.regions_file)
                try:
                    with Image.open(p) as im:
                        w = im.width
                    with Image.open(m.abs(e, k.key_file)) as im:
                        want = max(V.VLM_W, min(V.PREVIEW_W, im.width))
                except OSError:
                    continue
                if w != want:
                    V.preview(V._on_white(self._key_rgba(e, k)), self._regions(e, k)).save(p)

    def _restore_outputs(self) -> None:
        """Re-create colour files from saved VLM answers if they're missing (free, local)."""
        self._refresh_previews()
        for e in self.manifest.layers.values():
            if not (e.ref_path and Path(e.ref_path).exists()):
                continue
            for k in e.masters():
                if k.vlm_mapping and k.key_file and self.manifest.abs(e, k.key_file).exists():
                    if not (k.color_file and self.manifest.abs(e, k.color_file).exists()):
                        self._write_outputs(e, k)
        self.propagate_duplicates()

    def entries(self) -> list[M.LayerEntry]:
        return [e for e in self.manifest.layers.values() if e.enabled and not e.missing] if self.manifest else []

    # ---------------------------------------------------------------- export
    def export(self) -> None:
        """Export key frames — one TVPaint call per clip, then find regions locally (no requests)."""
        m = self.manifest
        entries = self.entries()
        by_clip: dict[int, list[M.LayerEntry]] = {}
        for e in entries:
            by_clip.setdefault(e.clip_id, []).append(e)
        t = time.time()
        for n, (clip_id, es) in enumerate(by_clip.items(), 1):
            if self.cancel.is_set():
                raise Cancelled()
            self.progress(n - 1, len(by_clip), f"Export clip #{es[0].clip_pos + 1} ({len(es)} layer(s))…")
            tmp = m.root / "_export_tmp"
            try:
                res = bridge.export_clip_keys(clip_id, [e.layer_id for e in es], tmp)
            except Exception as ex:  # noqa: BLE001
                self.log(f"  ⚠ clip export failed ({ex}) — slow per-frame fallback")
                res = {}
            for e in es:
                if e.layer_id in res:
                    self._merge_keys(e, [(s, ln) for s, ln, _ in res[e.layer_id]])
                    files = {s: f for s, _, f in res[e.layer_id]}
                else:
                    files = bridge.export_layer_keys_slow(e.clip_id, e.layer_id, [k.start for k in e.keys],
                                                          m.layer_dir(e) / "_render_tmp")
                self._store_keys(e, files)
                shutil.rmtree(m.layer_dir(e) / "_render_tmp", ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)
        self.log(f"  exported in {time.time() - t:.1f}s")
        for e in entries:
            assign_duplicates(e.keys)
            d = sum(1 for k in e.keys if k.dup_of is not None)
            self.log(f"  {e.layer_name}: {len(e.keys)} keys, {len(e.keys) - d} unique, {d} repeat(s)")
        m.save()
        self.prepare_regions(entries)

    def _store_keys(self, e: M.LayerEntry, files: dict[int, Path]) -> None:
        m = self.manifest
        for k in e.keys:
            base = key_basename(e.clip_folder, e.layer_folder, k.idx, k.start)
            out = m.sub(e, "keys") / f"{base}.png"
            src = files.get(k.start)
            if src is None or not Path(src).exists():
                k.status, k.error = M.FAILED, "TVPaint did not export this key"
                continue
            shutil.move(src, out)
            h = pixel_hash(out)
            if k.hash and k.hash != h:
                self.log(f"  key {k.idx} (frame {k.start}) changed since last run → will be recoloured")
                k.status, k.regions_file, k.color_file, k.vlm_mapping = M.EXPORTED, "", "", {}
                if e.anchor_idx == k.idx:
                    e.anchor_approved = False
            k.hash, k.key_file = h, f"keys/{base}.png"
            if not k.status or k.status == M.FAILED:
                k.status, k.error = M.EXPORTED, ""

    def prepare_regions(self, entries: list[M.LayerEntry] | None = None) -> None:
        """Find regions of every unique key ahead of colouring (cached on disk) — the paid step starts at once."""
        jobs = [(e, k) for e in (entries or self.entries()) for k in e.masters() if k.key_file]
        if not jobs:
            return
        t = time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(self._prepare_one, e, k) for e, k in jobs]
            for n, f in enumerate(as_completed(futs), 1):
                f.result()
                self.progress(n, len(jobs), f"Regions {n}/{len(jobs)}")
        self.propagate_duplicates()
        self.manifest.save()
        self.log(f"  regions ready for {len(jobs)} key(s) in {time.time() - t:.1f}s")

    def _prepare_one(self, e: M.LayerEntry, k: M.KeyFrame) -> None:
        regs = self._regions(e, k, current=not k.vlm_mapping)  # not coloured yet: newest segmentation
        base = Path(k.key_file).stem
        rel = f"regions/{base}_regions.png"
        if k.regions_file != rel or not self.manifest.abs(e, rel).exists():
            V.preview(V._on_white(self._key_rgba(e, k)), regs).save(self.manifest.sub(e, "regions") / f"{base}_regions.png")
            k.regions_file = rel

    # --------------------------------------------------------------- regions
    def _key_rgba(self, e: M.LayerEntry, k: M.KeyFrame) -> np.ndarray:
        return np.array(Image.open(self.manifest.abs(e, k.key_file)).convert("RGBA"))

    def _regions(self, e: M.LayerEntry, k: M.KeyFrame, current: bool = False) -> V.Regions:
        """Region map of a key. A map made by an older find_regions is kept while the key's saved VLM
        answer refers to its numbers; `current=True` (key without an answer) rebuilds it."""
        ck = (e.uid, k.idx, k.hash)
        regs = self._reg_cache.get(ck)
        if regs is None:
            regs = V.load_regions(self._regions_path(e, k), k.hash)
        if regs is None or (current and regs.ver != V.REGIONS_VER):
            self._commit_regions(e, k, V.find_regions(self._key_rgba(e, k)), redraw=regs is not None)
            return self._reg_cache[ck]
        self._reg_cache[ck] = regs
        return regs

    def _regions_path(self, e: M.LayerEntry, k: M.KeyFrame) -> Path:
        return self.manifest.sub(e, "regions") / f"{Path(k.key_file).stem}.npz"

    def _regions_for_colouring(self, e: M.LayerEntry, k: M.KeyFrame) -> tuple[V.Regions, bool]:
        """Newest-version regions for a paid colouring, NOT saved yet: the key's old answer refers to the
        old numbers until the new answer arrives (a failed/stopped request must leave both untouched)."""
        regs = self._regions(e, k)
        if regs.ver == V.REGIONS_VER:
            return regs, False
        return V.find_regions(self._key_rgba(e, k)), True

    def _commit_regions(self, e: M.LayerEntry, k: M.KeyFrame, regs: V.Regions, redraw: bool) -> None:
        V.save_regions(regs, self._regions_path(e, k), k.hash)
        self._reg_cache[(e.uid, k.idx, k.hash)] = regs
        if redraw:  # the numbered preview shows the old numbers: redraw it
            base = Path(k.key_file).stem
            V.preview(V._on_white(self._key_rgba(e, k)), regs).save(self.manifest.sub(e, "regions") / f"{base}_regions.png")
            k.regions_file = f"regions/{base}_regions.png"

    def _write_outputs(self, e: M.LayerEntry, k: M.KeyFrame) -> None:
        """Numbered-regions preview + flat colour fill from k.vlm_mapping."""
        m = self.manifest
        pal = reference_palette(Path(e.ref_path))
        regs = self._regions(e, k)
        base = Path(k.key_file).stem
        if not (k.regions_file and m.abs(e, k.regions_file).exists()):
            V.preview(V._on_white(self._key_rgba(e, k)), regs).save(m.sub(e, "regions") / f"{base}_regions.png")
            k.regions_file = f"regions/{base}_regions.png"
        V.fill(regs, k.vlm_mapping, pal).save(m.sub(e, "color") / f"{base}_color.png")
        k.color_file = f"color/{base}_color.png"
        k.status, k.error = M.COLORED, ""

    # --------------------------------------------------------------- colour (VLM)
    def anchor_key(self, e: M.LayerEntry) -> M.KeyFrame | None:
        masters = [k for k in e.masters() if k.key_file]
        if not masters:
            return None
        if e.anchor_idx is None or all(k.idx != e.anchor_idx for k in masters):
            e.anchor_idx = masters[0].idx
        return e.key(e.anchor_idx)

    def pending_anchors(self, only_uid: str | None = None, force: bool = False) -> list[tuple[M.LayerEntry, M.KeyFrame]]:
        out = []
        for e in self.entries():
            if only_uid and e.uid != only_uid:
                continue
            a = self.anchor_key(e)
            if a and (force or not a.vlm_mapping):
                out.append((e, a))
        return out

    def pending_follows(self, only_uid: str | None = None) -> list[tuple[M.LayerEntry, M.KeyFrame]]:
        """Non-anchor keys of layers whose anchor is approved and that weren't coloured with that anchor."""
        out = []
        for e in self.entries():
            if only_uid and e.uid != only_uid:
                continue
            a = self.anchor_key(e)
            if not (a and a.vlm_mapping and e.anchor_approved):
                continue
            for k in e.masters():
                if k.idx != a.idx and k.key_file and (not k.vlm_mapping or k.vlm_anchor_ver != e.anchor_version):
                    out.append((e, k))
        return out

    def estimate_follows(self) -> tuple[int, float]:
        n = len(self.pending_follows())
        return n, n * self.cfg.vlm_est_cost_follow

    def awaiting_approval(self) -> list[M.LayerEntry]:
        out = []
        for e in self.entries():
            a = self.anchor_key(e)
            if a and a.vlm_mapping and not e.anchor_approved:
                out.append(e)
        return out

    @staticmethod
    def has_ref(e: M.LayerEntry) -> bool:
        return bool(e.ref_path and Path(e.ref_path).exists())

    def _check_refs(self, todo: list, skip: bool) -> list:
        """One layer: a missing reference is an error. All layers: those layers are skipped (logged)."""
        missing = sorted({e.layer_name for e, _ in todo if not self.has_ref(e)})
        if missing and not skip:
            raise ValueError("Character reference missing for: " + ", ".join(missing))
        if missing:
            self.log("  пропущено (немає референсу): " + ", ".join(missing))
        return [(e, k) for e, k in todo if self.has_ref(e)]

    def colorize_anchors(self, only_uid: str | None = None, force: bool = False) -> None:
        """Colour only the anchor key: of one layer, or (only_uid=None) of every layer in every clip that
        has none yet — all requests run in parallel (cfg.concurrency)."""
        todo = self._check_refs(self.pending_anchors(only_uid, force), skip=only_uid is None)
        if not todo:
            self.log("Якорі вже розфарбовано.")
            return
        for e, a in todo:
            if force:
                e.anchor_approved = False
        self._colorize(todo)

    def colorize_follows(self, only_uid: str | None = None) -> None:
        """Colour the remaining keys of one layer, or (only_uid=None) of every layer with an approved anchor
        in every clip — all requests run in parallel (cfg.concurrency)."""
        todo = self._check_refs(self.pending_follows(only_uid), skip=only_uid is None)
        if not todo:
            waiting = [e.layer_name for e in self.awaiting_approval()]
            self.log(("Чекають затвердження якоря: " + ", ".join(waiting)) if waiting else "Нічого розфарбовувати.")
            self.propagate_duplicates()
            return
        self._colorize(todo)

    def _colorize(self, jobs: list[tuple[M.LayerEntry, M.KeyFrame]]) -> None:
        m = self.manifest
        url_cache = app_dir() / "upload_cache.json"
        c = self.cfg
        # anchor: strong reasoning model; followed keys: fast model (the approved anchor carries the knowledge)
        vlm_anchor = V.VLMColorizer(get_api_key(), c.vlm_model, self.cancel, reasoning=c.vlm_reasoning,
                                    log=self.log, url_cache=url_cache)
        follow_model = c.vlm_model_follow or c.vlm_model
        vlm_follow = V.VLMColorizer(get_api_key(), follow_model, self.cancel, reasoning=False,
                                    log=self.log, url_cache=url_cache)
        n_anchor = sum(1 for e, k in jobs if k.idx == e.anchor_idx)
        if n_anchor:
            self.log(f"Якір: {c.vlm_model}" + (" · reasoning" if c.vlm_reasoning else "") + f" · {n_anchor} шт.")
        if len(jobs) - n_anchor:
            self.log(f"Кадри за якорем: {follow_model} · {len(jobs) - n_anchor} шт.")

        for e, k in jobs:
            self.key_status(e.uid, k.idx, "у черзі")

        def run(e: M.LayerEntry, k: M.KeyFrame):
            label = f"{e.layer_name} #{k.idx}"
            t = time.time()
            status = lambda s, _u=e.uid, _i=k.idx: self.key_status(_u, _i, s)  # noqa: E731
            status("підготовка")
            pal = reference_palette(Path(e.ref_path))
            regs, fresh = self._regions_for_colouring(e, k)  # recolouring anyway: newest segmentation
            rgba = self._key_rgba(e, k)
            try:
                if k.idx == e.anchor_idx:
                    model = c.vlm_model
                    mapping, cost, _ = vlm_anchor.color_anchor(rgba, regs, Path(e.ref_path), pal, label, status)
                else:
                    model = follow_model
                    a = e.key(e.anchor_idx)
                    a_regs = self._regions(e, a)
                    a_fill = V.fill(a_regs, e.anchor_approved_mapping, pal)
                    mapping, cost, _ = vlm_follow.color_follow(rgba, regs, self._key_rgba(e, a), a_regs,
                                                               e.anchor_approved_mapping, a_fill, Path(e.ref_path),
                                                               pal, label, status)
                    k.vlm_anchor_ver = e.anchor_version
            finally:
                status("")
            if fresh:  # the answer refers to the new numbers: only now replace the saved region map
                self._commit_regions(e, k, regs, redraw=True)
            k.vlm_mapping ={str(r): mapping.get(str(r), {"part": "?", "color": V.BG}) for r in range(1, regs.n + 1)}
            k.cost = self.cost.record(project=str(self.project.path), clip=e.clip_name, layer=e.layer_name,
                                      frame=k.start, model=model, usd=cost)
            self._write_outputs(e, k)
            m.save()
            return time.time() - t

        done = 0
        with ThreadPoolExecutor(max_workers=max(1, int(self.cfg.concurrency))) as pool:
            futs = {pool.submit(run, e, k): (e, k) for e, k in jobs}
            for f in as_completed(futs):
                e, k = futs[f]
                done += 1
                try:
                    dt = f.result()
                    tag = "  ← ЯКІР: перевірте і затвердіть" if k.idx == e.anchor_idx else ""
                    self.log(f"  ✓ {e.layer_name} key {k.idx} (frame {k.start}) ${k.cost:.3f} · {dt:.0f}s{tag}")
                except Cancelled:
                    pass
                except AuthError as ex:
                    self.cancel.set()
                    self.log(f"  ✗ API key problem: {ex}")
                except Exception as ex:  # noqa: BLE001
                    k.status, k.error = M.FAILED, str(ex)[:500]
                    self.log(f"  ✗ {e.layer_name} key {k.idx}: {ex}")
                self.progress(done, len(jobs), f"Coloured {done}/{len(jobs)} · session ${self.cost.session:.2f}")
        m.save()
        self.propagate_duplicates()
        if self.cancel.is_set():
            raise Cancelled()

    def refill_all(self) -> None:
        """Repaint every key from its saved VLM answer (e.g. after changing the reference). No requests."""
        jobs = [(e, k) for e in self.entries() for k in e.masters() if k.vlm_mapping and k.key_file]
        for n, (e, k) in enumerate(jobs, 1):
            self.progress(n, len(jobs), f"Refill {e.layer_name} key {k.idx}")
            self._write_outputs(e, k)
        self.propagate_duplicates()
        self.manifest.save()

    # ---- user actions: anchor and region colours
    def set_anchor(self, e: M.LayerEntry, k: M.KeyFrame) -> None:
        master = e.key(k.dup_of) if k.dup_of is not None else k
        if e.anchor_idx != master.idx:
            e.anchor_idx, e.anchor_approved = master.idx, False
        self.manifest.save()

    def approve_anchor(self, e: M.LayerEntry) -> None:
        a = self.anchor_key(e)
        if not a or not a.vlm_mapping:
            raise ValueError("The anchor frame has not been coloured yet.")
        # version = hash of the approved colours: keys coloured with another anchor state get
        # recoloured, but reverting to an earlier approved state doesn't trigger paid calls again
        colors = {r: v.get("color") for r, v in a.vlm_mapping.items()}
        e.anchor_version = int(hashlib.sha1(json.dumps(colors, sort_keys=True).encode()).hexdigest()[:12], 16)
        e.anchor_approved_mapping = {r: dict(v) for r, v in a.vlm_mapping.items()}
        e.anchor_approved = True
        self.manifest.save()

    def region_at(self, e: M.LayerEntry, k: M.KeyFrame, x: int, y: int) -> int:
        regs = self._regions(e, k)
        H, W = regs.reg.shape
        if not (0 <= x < W and 0 <= y < H):
            return 0
        r = int(regs.reg[y, x])
        if r == 0:  # clicked on a line / gap: nearest region
            y0, y1, x0, x1 = max(0, y - 40), min(H, y + 41), max(0, x - 40), min(W, x + 41)
            win = regs.reg[y0:y1, x0:x1]
            if (win > 0).any():
                _, (iy, ix) = ndi.distance_transform_edt(win == 0, return_indices=True)
                r = int(win[iy[y - y0, x - x0], ix[y - y0, x - x0]])
        return r

    def set_region_color(self, e: M.LayerEntry, k: M.KeyFrame, region: int, color: str) -> None:
        master = e.key(k.dup_of) if k.dup_of is not None else k
        info = dict(master.vlm_mapping.get(str(region), {"part": "?"}))
        info["color"] = color
        master.vlm_mapping[str(region)] = info
        if master.idx == e.anchor_idx:
            e.anchor_approved = e.anchor_approved and master.vlm_mapping == e.anchor_approved_mapping
        self._write_outputs(e, master)
        self.propagate_duplicates()
        self.manifest.save()

    def propagate_duplicates(self) -> None:
        """Repeated keys reuse the master's result: copy the files under their own names (keeps order)."""
        m = self.manifest
        for e in self.entries():
            for k in e.keys:
                if k.dup_of is None or not k.key_file:
                    continue
                master = e.key(k.dup_of)
                base = Path(k.key_file).stem
                for kind, src_rel, attr, suffix in (("regions", master.regions_file, "regions_file", "_regions.png"),
                                                    ("color", master.color_file, "color_file", "_color.png")):
                    if src_rel and m.abs(e, src_rel).exists():
                        shutil.copyfile(m.abs(e, src_rel), m.sub(e, kind) / f"{base}{suffix}")
                        setattr(k, attr, f"{kind}/{base}{suffix}")
                if master.color_file and m.abs(e, master.color_file).exists():
                    k.vlm_mapping, k.status, k.cost = dict(master.vlm_mapping), M.COLORED, 0.0
        m.save()

    # ----------------------------------------------------------------- inject
    def is_ready(self, e: M.LayerEntry) -> bool:
        m = self.manifest
        return bool(e.keys) and all(k.color_file and m.abs(e, k.color_file).exists() for k in e.keys)

    def inject(self, uids: list[str] | None = None, save: str = "") -> Path | None:
        """Insert colour layers. `save`: "new" (next _tintr_vNN), "overwrite", "none"; "" = config default."""
        m = self.manifest
        pool = [e for e in self.entries() if uids is None or e.uid in uids]
        ready = [e for e in pool if self.is_ready(e)]
        skipped = [e.layer_name for e in pool if e not in ready]
        if skipped:
            self.log("  skipped (not all keys coloured): " + ", ".join(skipped))
        if not ready:
            raise ValueError("No layer is fully coloured yet.")
        t = time.time()
        size = (self.project.width, self.project.height)
        for n, e in enumerate(ready, 1):
            if self.cancel.is_set():
                raise Cancelled()
            self.progress(n - 1, len(ready), f"Insert {e.layer_name}…")
            keys = [(k.start, k.length, m.abs(e, k.color_file)) for k in e.keys]
            e.color_layer_id = bridge.insert_color_layer(e.clip_id, e.layer_id, e.color_layer_id, keys,
                                                         m.root / "_insert_tmp", size, self.log)
            for k in e.keys:
                k.status = M.INJECTED
            m.save()
        self.progress(len(ready), len(ready), "Inserted")
        self.log(f"  inserted {len(ready)} layer(s) in {time.time() - t:.1f}s")
        save = save or ("new" if self.cfg.save_as_new_version else "overwrite")
        if save == "none":
            self.log("Project not saved — review it in TVPaint and save there.")
            return None
        target = self._next_version_path() if save == "new" else None
        self.log(f"Saving project → {target or self.project.path} (this can take a while)…")
        t = time.time()
        saved = bridge.save_project(target)
        self.log(f"Saved: {saved} ({time.time() - t:.0f}s)")
        return saved

    def _next_version_path(self) -> Path:
        p = self.project.path
        stem = p.stem.split("_tintr_v")[0]
        i = 1
        while (p.parent / f"{stem}_tintr_v{i:02d}{p.suffix}").exists():
            i += 1
        return p.parent / f"{stem}_tintr_v{i:02d}{p.suffix}"
