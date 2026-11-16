"""Pipeline orchestration — UI independent. TVPaint calls happen on the calling thread only.

scan → export keys → VLM colours the anchor → user approves → VLM colours the other keys
following the anchor → inject colour layers into TVPaint and save.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from tintr.config import Config, get_api_key
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

    # ------------------------------------------------------------------ scan
    def connect_and_scan(self) -> list[M.LayerEntry]:
        bridge.connect(self.cfg.rpc_port)
        self.project = bridge.current_project()
        self.log(f"Project: {self.project.path}  ({self.project.width}x{self.project.height})")
        self.manifest = M.Manifest.load(self.project.path)
        self.log("Scanning clips for '_сс-' / '_cc-' layers…")
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
            prev_keys = {k.start: k for k in prev.keys} if prev else {}
            for i, (start, length) in enumerate(f.instances, 1):
                pk = prev_keys.get(start)
                if pk:
                    pk.idx, pk.length = i, length
                    e.keys.append(pk)
                else:
                    e.keys.append(M.KeyFrame(idx=i, start=start, length=length, status=""))
            new[uid] = e
        self.manifest.layers = new
        self._restore_outputs()
        self.manifest.save()
        self.log(f"Found {len(new)} tagged layer(s).")
        return list(new.values())

    def _restore_outputs(self) -> None:
        """Re-create colour files from saved VLM answers if they're missing (free, local)."""
        for e in self.manifest.layers.values():
            if not (e.ref_path and Path(e.ref_path).exists()):
                continue
            for k in e.masters():
                if k.vlm_mapping and k.key_file and self.manifest.abs(e, k.key_file).exists():
                    if not (k.color_file and self.manifest.abs(e, k.color_file).exists()):
                        self._write_outputs(e, k)
        self.propagate_duplicates()

    def entries(self) -> list[M.LayerEntry]:
        return [e for e in self.manifest.layers.values() if e.enabled] if self.manifest else []

    # ---------------------------------------------------------------- export
    def export(self) -> None:
        m = self.manifest
        entries = self.entries()
        total = sum(len(e.keys) for e in entries)
        n = 0
        for e in entries:
            if self.cancel.is_set():
                raise Cancelled()
            self.progress(n, total, f"Rendering keys of {e.layer_name}…")
            tmp = m.layer_dir(e) / "_render_tmp"
            batch = bridge.export_layer_keys(e.clip_id, e.layer_id, [k.start for k in e.keys], tmp)
            for k in e.keys:
                if self.cancel.is_set():
                    raise Cancelled()
                n += 1
                base = key_basename(e.clip_folder, e.layer_folder, k.idx, k.start)
                out = m.sub(e, "keys") / f"{base}.png"
                self.progress(n, total, f"Export {e.layer_name} frame {k.start}")
                if k.start in batch:
                    shutil.move(batch[k.start], out)
                else:
                    bridge.export_key(e.clip_id, e.layer_id, k.start, out)
                h = pixel_hash(out)
                if k.hash and k.hash != h:
                    self.log(f"  key {k.idx} (frame {k.start}) changed since last run → will be recoloured")
                    k.status, k.regions_file, k.color_file, k.vlm_mapping = M.EXPORTED, "", "", {}
                    if e.anchor_idx == k.idx:
                        e.anchor_approved = False
                k.hash, k.key_file = h, f"keys/{base}.png"
                if not k.status:
                    k.status = M.EXPORTED
            shutil.rmtree(tmp, ignore_errors=True)
        for e in entries:
            assign_duplicates(e.keys)
            d = sum(1 for k in e.keys if k.dup_of is not None)
            self.log(f"  {e.layer_name}: {len(e.keys)} keys, {len(e.keys) - d} unique, {d} repeat(s)")
        m.save()

    # --------------------------------------------------------------- regions
    def _key_rgba(self, e: M.LayerEntry, k: M.KeyFrame) -> np.ndarray:
        return np.array(Image.open(self.manifest.abs(e, k.key_file)).convert("RGBA"))

    def _regions(self, e: M.LayerEntry, k: M.KeyFrame) -> V.Regions:
        ck = (e.uid, k.idx, k.hash)
        if ck not in self._reg_cache:
            self._reg_cache[ck] = V.find_regions(self._key_rgba(e, k))
        return self._reg_cache[ck]

    def _write_outputs(self, e: M.LayerEntry, k: M.KeyFrame) -> None:
        """Numbered-regions preview + flat colour fill from k.vlm_mapping."""
        m = self.manifest
        pal = reference_palette(Path(e.ref_path))
        regs = self._regions(e, k)
        base = Path(k.key_file).stem
        V.annotate(V._on_white(self._key_rgba(e, k)), regs).save(m.sub(e, "regions") / f"{base}_regions.png")
        V.fill(regs, k.vlm_mapping, pal).save(m.sub(e, "color") / f"{base}_color.png")
        k.regions_file = f"regions/{base}_regions.png"
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

    def pending(self) -> list[tuple[M.LayerEntry, M.KeyFrame]]:
        """Keys a VLM call would be made for now: anchors first, other keys only after approval."""
        out = []
        for e in self.entries():
            a = self.anchor_key(e)
            if a is None:
                continue
            if not a.vlm_mapping:
                out.append((e, a))
                continue
            if not e.anchor_approved:
                continue
            for k in e.masters():
                if k.idx != a.idx and k.key_file and (not k.vlm_mapping or k.vlm_anchor_ver != e.anchor_version):
                    out.append((e, k))
        return out

    def estimate(self) -> tuple[int, float]:
        n = len(self.pending())
        return n, n * self.cfg.vlm_est_cost

    def awaiting_approval(self) -> list[M.LayerEntry]:
        out = []
        for e in self.entries():
            a = self.anchor_key(e)
            if a and a.vlm_mapping and not e.anchor_approved:
                out.append(e)
        return out

    def colorize(self) -> None:
        m = self.manifest
        missing = [e.layer_name for e in self.entries() if not (e.ref_path and Path(e.ref_path).exists())]
        if missing:
            raise ValueError("Character reference missing for: " + ", ".join(missing))
        todo = self.pending()
        if not todo:
            waiting = [e.layer_name for e in self.awaiting_approval()]
            self.log(("Waiting for anchor approval: " + ", ".join(waiting)) if waiting else "Nothing to colour.")
            self.propagate_duplicates()
            return
        vlm = V.VLMColorizer(get_api_key(), self.cfg.vlm_model, self.cancel)
        anchors = [(e, k) for e, k in todo if k.idx == e.anchor_idx]
        follows = [(e, k) for e, k in todo if k.idx != e.anchor_idx]
        self.log(f"VLM {self.cfg.vlm_model}: {len(anchors)} anchor(s), {len(follows)} frame(s)")

        def run(e: M.LayerEntry, k: M.KeyFrame):
            pal = reference_palette(Path(e.ref_path))
            regs = self._regions(e, k)
            rgba = self._key_rgba(e, k)
            if k.idx == e.anchor_idx:
                mapping, cost, _ = vlm.color_anchor(rgba, regs, Path(e.ref_path), pal)
            else:
                a = e.key(e.anchor_idx)
                a_regs = self._regions(e, a)
                a_fill = V.fill(a_regs, e.anchor_approved_mapping, pal)
                mapping, cost, _ = vlm.color_follow(rgba, regs, self._key_rgba(e, a), a_regs,
                                                    e.anchor_approved_mapping, a_fill, Path(e.ref_path), pal)
                k.vlm_anchor_ver = e.anchor_version
            k.vlm_mapping = {str(r): mapping.get(str(r), {"part": "?", "color": V.BG}) for r in range(1, regs.n + 1)}
            k.cost = self.cost.record(project=str(self.project.path), clip=e.clip_name, layer=e.layer_name,
                                      frame=k.start, model=self.cfg.vlm_model, usd=cost)
            self._write_outputs(e, k)
            m.save()

        jobs = anchors + follows
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, int(self.cfg.concurrency))) as pool:
            futs = {pool.submit(run, e, k): (e, k) for e, k in jobs}
            for f in as_completed(futs):
                e, k = futs[f]
                done += 1
                try:
                    f.result()
                    tag = "  ← ANCHOR: review and approve" if k.idx == e.anchor_idx else ""
                    self.log(f"  ✓ {e.layer_name} key {k.idx} (frame {k.start}) ${k.cost:.3f}{tag}")
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
                if not (master.color_file and m.abs(e, master.color_file).exists()):
                    continue
                base = Path(k.key_file).stem
                for kind, src_rel, attr, suffix in (("regions", master.regions_file, "regions_file", "_regions.png"),
                                                    ("color", master.color_file, "color_file", "_color.png")):
                    if src_rel and m.abs(e, src_rel).exists():
                        shutil.copyfile(m.abs(e, src_rel), m.sub(e, kind) / f"{base}{suffix}")
                        setattr(k, attr, f"{kind}/{base}{suffix}")
                k.vlm_mapping, k.status, k.cost = dict(master.vlm_mapping), M.COLORED, 0.0
        m.save()

    # ----------------------------------------------------------------- inject
    def inject(self) -> Path:
        m = self.manifest
        ready = [e for e in self.entries()
                 if e.keys and all(k.color_file and m.abs(e, k.color_file).exists() for k in e.keys)]
        skipped = [e.layer_name for e in self.entries() if e not in ready]
        if skipped:
            self.log("  skipped (not all keys coloured): " + ", ".join(skipped))
        if not ready:
            raise ValueError("No layer is fully coloured yet.")
        total = sum(len(e.keys) for e in ready)
        n = 0
        for e in ready:
            e.color_layer_id = bridge.ensure_color_layer(e.clip_id, e.layer_id, e.color_layer_id, self.log)
            m.save()
            for k in e.keys:
                if self.cancel.is_set():
                    raise Cancelled()
                n += 1
                self.progress(n, total, f"Insert {e.layer_name} frame {k.start}")
                bridge.load_frame(e.clip_id, e.color_layer_id, k.start, k.length, m.abs(e, k.color_file))
                k.status = M.INJECTED
            m.save()
        target = self._next_version_path() if self.cfg.save_as_new_version else None
        self.log(f"Saving project → {target or self.project.path} (this can take a while)…")
        saved = bridge.save_project(target)
        self.log(f"Saved: {saved}")
        return saved

    def _next_version_path(self) -> Path:
        p = self.project.path
        stem = p.stem.split("_tintr_v")[0]
        i = 1
        while (p.parent / f"{stem}_tintr_v{i:02d}{p.suffix}").exists():
            i += 1
        return p.parent / f"{stem}_tintr_v{i:02d}{p.suffix}"
