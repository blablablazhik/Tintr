"""Python side of the web UI: the object exposed to JS as `window.pywebview.api`.

Long operations (TVPaint, VLM) run on ONE worker thread (TVPaint RPC is not thread-safe) and report
back through events pushed to JS (`tintr.on({...})`). Quick edits run directly and return the new state.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from tintr import APP_NAME, __version__
from tintr.config import Config, app_dir, get_api_key, set_api_key
from tintr.core import manifest as M
from tintr.core.palette import reference_palette
from tintr.core.vlm import Cancelled
from tintr.naming import TAG_RE
from tintr.pipeline import Pipeline

LOG_KEEP = 800


def _hex(c) -> str:
    return "#%02X%02X%02X" % (int(c[0]), int(c[1]), int(c[2]))


def _short(name: str) -> str:
    """Layer name without the _cc- tag: 'Anim_1_cc-' → 'Anim_1'."""
    return TAG_RE.split(name)[0].rstrip("_ -") or name


class Api:
    def __init__(self):
        self._cfg = Config.load()
        self._window = None  # set by app.main after the window exists
        self._port = 0  # image server port
        self._log: list[str] = []
        self._busy = ""
        self._progress = {"n": 0, "total": 0, "msg": ""}
        self._live: dict[str, dict] = {}
        self._worker = ThreadPoolExecutor(max_workers=1)
        self._last_push = 0.0
        self._push_timer: threading.Timer | None = None
        self._pl = Pipeline(self._cfg, self._on_log, self._on_progress)
        self._pl.key_status = self._on_key

    # ------------------------------------------------------------------ events → JS
    def _emit(self, kind: str, data) -> None:
        if not self._window:
            return
        try:
            self._window.evaluate_js(f"window.tintr && tintr.on({json.dumps({'type': kind, 'data': data}, ensure_ascii=False)})")
        except Exception:  # noqa: BLE001 — window closing
            pass

    def _on_log(self, s: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {s}"
        self._log.append(line)
        del self._log[:-LOG_KEEP]
        self._emit("log", line)

    def _on_progress(self, n: int, total: int, msg: str) -> None:
        self._progress = {"n": n, "total": total, "msg": msg}
        self._emit("progress", self._progress)
        self._push_state_soon()

    def _on_key(self, uid: str, idx: int, text: str) -> None:
        k = f"{uid}:{idx}"
        if text:
            prev = self._live.get(k)
            self._live[k] = {"text": text, "t0": prev["t0"] if prev else time.time()}
        else:
            self._live.pop(k, None)
        self._emit("key", {"id": k, "live": self._live.get(k)})
        if not text:
            self._push_state_soon()

    def _push_state_soon(self, delay: float = 0.4) -> None:
        """Throttled full-state push (a coloured key changes thumbnails, counts, costs)."""
        if self._push_timer and self._push_timer.is_alive():
            return
        self._push_timer = threading.Timer(delay, lambda: self._emit("state", self._state()))
        self._push_timer.daemon = True
        self._push_timer.start()

    # ------------------------------------------------------------------ tasks
    def _task(self, name: str, fn) -> dict:
        if self._busy:
            return {"ok": False, "error": f"Зачекайте: виконується «{self._busy}»."}
        self._busy = name
        self._progress = {"n": 0, "total": 0, "msg": name}
        self._pl.cancel.clear()
        self._on_log(f"— {name}")
        self._emit("state", self._state())

        def run():
            err = ""
            try:
                fn()
            except Cancelled:
                err = "Зупинено."
                self._on_log("Зупинено користувачем.")
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                err = f"{e}"
                self._on_log(f"✗ {type(e).__name__}: {e}")
            finally:
                self._busy = ""
                self._live.clear()
                self._progress = {"n": 0, "total": 0, "msg": ""}
            self._emit("state", self._state())
            self._emit("done", {"task": name, "ok": not err, "error": err})

        self._worker.submit(run)
        return {"ok": True}

    # ------------------------------------------------------------------ state
    def _url(self, p: Path | str | None, w: int = 0) -> str:
        if not p:
            return ""
        p = Path(p)
        try:
            v = p.stat().st_mtime_ns
        except OSError:
            return ""
        return f"http://127.0.0.1:{self._port}/f?p={quote(str(p))}&v={v}" + (f"&w={w}" if w else "")

    def _allowed_paths(self) -> list[Path]:
        out: list[Path] = []
        m = self._pl.manifest
        if m:
            out.append(m.root)
            out += [Path(e.ref_path) for e in m.layers.values() if e.ref_path]
        return out

    def _stage(self, e: M.LayerEntry) -> str:
        m = self._pl.manifest
        if e.missing:
            return "missing"
        if not e.keys or not all(k.key_file and m.abs(e, k.key_file).exists() for k in e.keys):
            return "export"
        if not (e.ref_path and Path(e.ref_path).exists()):
            return "ref"
        a = self._pl.anchor_key(e)
        if a is None:
            return "export"
        if not a.vlm_mapping:
            return "anchor"
        if not e.anchor_approved:
            return "approve"
        if any(k.idx != a.idx and (not k.vlm_mapping or k.vlm_anchor_ver != e.anchor_version)
               for k in e.masters() if k.key_file):
            return "frames"
        if all(k.status == M.INJECTED for k in e.keys):
            return "done"
        return "ready"

    _STAGE_TEXT = {
        "missing": ("не знайдено в TVPaint", "warn"), "export": ("ключі не виведено", "muted"),
        "ref": ("оберіть референс", "warn"), "anchor": ("якір не розфарбовано", "muted"),
        "approve": ("якір чекає затвердження", "accent"), "frames": ("кадри за якорем", "muted"),
        "ready": ("готово до вставки", "ok"), "done": ("вставлено", "ok"),
    }

    def _layer(self, e: M.LayerEntry) -> dict:
        m = self._pl.manifest
        stage = self._stage(e)
        a = self._pl.anchor_key(e) if stage not in ("missing", "export") else None
        pal = []
        if e.ref_path and Path(e.ref_path).exists():
            try:
                pal = [{"name": f"C{i}", "hex": _hex(c)} for i, c in enumerate(reference_palette(Path(e.ref_path)), 1)]
            except Exception:  # noqa: BLE001
                pal = []
        keys = []
        for k in e.keys:
            def f(rel, w=0, _k=k):
                return self._url(m.abs(e, rel), w) if rel else ""
            colored = bool(k.color_file and m.abs(e, k.color_file).exists())
            keys.append({
                "idx": k.idx, "start": k.start, "length": k.length, "dup": k.dup_of,
                "anchor": bool(a and k.idx == a.idx), "status": k.status, "error": k.error, "cost": k.cost,
                "colored": colored, "followed": bool(k.vlm_mapping) and (k.dup_of is not None or k.idx == (a.idx if a else None)
                                                                     or k.vlm_anchor_ver == e.anchor_version),
                "key": f(k.key_file, 1600), "keyThumb": f(k.key_file, 280),
                "regions": f(k.regions_file, 1600),
                "color": f(k.color_file, 1600) if colored else "", "colorThumb": f(k.color_file, 280) if colored else "",
                "live": self._live.get(f"{e.uid}:{k.idx}"),
            })
        n_follow = sum(1 for le, _ in self._pl.pending_follows() if le.uid == e.uid) if stage == "frames" else 0
        stext, tone = self._STAGE_TEXT[stage]
        return {
            "uid": e.uid, "name": _short(e.layer_name), "fullName": e.layer_name, "enabled": e.enabled,
            "clipId": e.clip_id, "stage": stage, "stateText": stext, "tone": tone,
            "done": sum(1 for k in keys if k["colored"]), "total": len(keys),
            "unique": len(e.masters()), "anchorIdx": a.idx if a else None, "anchorApproved": e.anchor_approved,
            "ref": Path(e.ref_path).name if e.ref_path else "", "refUrl": self._url(e.ref_path, 900),
            "palette": pal, "swatch": self._swatch(pal),
            "followsN": n_follow, "keys": keys,
        }

    @staticmethod
    def _swatch(pal: list[dict]) -> str:
        """Layer marker colour: the most common clearly coloured palette entry (skips paper white / ink)."""
        for p in pal:
            r, g, b = (int(p["hex"][i:i + 2], 16) for i in (1, 3, 5))
            if max(r, g, b) - min(r, g, b) > 40 and 40 < (r + g + b) / 3 < 235:
                return p["hex"]
        return pal[0]["hex"] if pal else ""

    def _state(self) -> dict:
        m, p = self._pl.manifest, self._pl.project
        st: dict = {
            "app": f"{APP_NAME} {__version__}", "connected": bool(m and p), "busy": self._busy,
            "progress": self._progress, "est": self._cfg.vlm_est_cost, "estFollow": self._cfg.vlm_est_cost_follow,
            "port": self._cfg.rpc_port,
            "apiKey": bool(get_api_key()), "recent": self._recent(), "saveNew": self._cfg.save_as_new_version,
        }
        total, p_usd, p_n = self._pl.cost.totals(str(p.path) if p else None)
        st["cost"] = {"session": self._pl.cost.session, "project": p_usd, "projectN": p_n, "total": total}
        if not st["connected"]:
            return st
        st["project"] = {"name": p.path.name, "path": str(p.path), "w": p.width, "h": p.height,
                         "scannedAt": m.scanned_at, "scanned": bool(m.scanned_at or m.layers),
                         "nextVersion": self._pl._next_version_path().name}
        clips: dict[int, dict] = {}
        for e in m.layers.values():
            c = clips.setdefault(e.clip_id, {"id": e.clip_id, "pos": e.clip_pos + 1, "name": e.clip_name, "layers": []})
            c["layers"].append(self._layer(e))
        st["clips"] = sorted(clips.values(), key=lambda c: c["pos"])
        return st

    # ------------------------------------------------------------------ recent projects
    def _recent_file(self) -> Path:
        return app_dir() / "recent.json"

    def _recent(self) -> list[dict]:
        try:
            return json.loads(self._recent_file().read_text(encoding="utf-8"))[:6]
        except Exception:  # noqa: BLE001
            return []

    def _remember_project(self) -> None:
        m, p = self._pl.manifest, self._pl.project
        _, usd, _ = self._pl.cost.totals(str(p.path))
        item = {"name": p.path.name, "path": str(p.path), "layers": len(m.layers),
                "clips": len({e.clip_id for e in m.layers.values()}), "usd": round(usd, 2),
                "when": datetime.now().strftime("%d.%m")}
        rec = [r for r in self._recent() if r.get("path") != item["path"]]
        try:
            self._recent_file().write_text(json.dumps([item] + rec[:9], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ================================================================== JS API
    def get_state(self) -> dict:
        st = self._state()
        st["log"] = self._log[-300:]
        return st

    def connect(self, port: int | None = None, rescan: bool = False) -> dict:
        if port:
            self._cfg.rpc_port = int(port)
            self._cfg.save()

        def run():
            self._pl.connect(rescan=rescan)
            self._remember_project()
        return self._task("Сканування проекту" if rescan else "Підключення до TVPaint", run)

    def export(self) -> dict:
        return self._task("Вивід ключових кадрів", self._pl.export)

    def colorize_anchor(self, uid: str, force: bool = False) -> dict:
        if not get_api_key():
            return {"ok": False, "error": "Задайте fal.ai API key у налаштуваннях."}
        return self._task("Розфарбовування якоря", lambda: self._pl.colorize_anchors(uid, force))

    def colorize_follows(self) -> dict:
        if not get_api_key():
            return {"ok": False, "error": "Задайте fal.ai API key у налаштуваннях."}
        return self._task("Розфарбовування кадрів за якорем", self._pl.colorize_follows)

    def inject(self, uids: list[str], save: str) -> dict:
        if save in ("new", "overwrite"):
            self._cfg.save_as_new_version = save == "new"
            self._cfg.save()
        return self._task("Вставка в TVPaint", lambda: self._pl.inject(uids, save))

    def refill(self) -> dict:
        return self._task("Перезаливка зі збережених відповідей", self._pl.refill_all)

    def stop(self) -> dict:
        self._pl.cancel.set()
        self._on_log("Зупинка… (поточні запити буде завершено)")
        return {"ok": True}

    # ---- quick edits (no TVPaint)
    def _entry(self, uid: str) -> M.LayerEntry:
        return self._pl.manifest.layers[uid]

    def _guard(self) -> dict | None:
        if self._busy:
            return {"ok": False, "error": f"Зачекайте: виконується «{self._busy}»."}
        return None

    def approve_anchor(self, uid: str) -> dict:
        if g := self._guard():
            return g
        e = self._entry(uid)
        self._pl.approve_anchor(e)
        self._on_log(f"✓ Якір затверджено: {_short(e.layer_name)} ключ #{e.anchor_idx}")
        return {"ok": True, "state": self._state()}

    def set_anchor(self, uid: str, idx: int) -> dict:
        if g := self._guard():
            return g
        e = self._entry(uid)
        self._pl.set_anchor(e, e.key(int(idx)))
        return {"ok": True, "state": self._state()}

    def region_at(self, uid: str, idx: int, x: float, y: float) -> dict:
        e = self._entry(uid)
        k = e.key(int(idx))
        master = e.key(k.dup_of) if k.dup_of is not None else k
        if not master.key_file:
            return {"ok": False}
        r = self._pl.region_at(e, master, int(x), int(y))
        if not r:
            return {"ok": False}
        info = master.vlm_mapping.get(str(r), {})
        return {"ok": True, "region": r, "part": info.get("part", ""), "color": info.get("color", ""),
                "editable": bool(master.vlm_mapping)}

    def set_region_color(self, uid: str, idx: int, region: int, color: str) -> dict:
        if g := self._guard():
            return g
        e = self._entry(uid)
        self._pl.set_region_color(e, e.key(int(idx)), int(region), str(color))
        self._on_log(f"  {_short(e.layer_name)} #{idx}: область {region} → {color}")
        return {"ok": True, "state": self._state()}

    def pick_ref(self, uid: str) -> dict:
        if g := self._guard():
            return g
        import webview

        e = self._entry(uid)
        start = str(Path(e.ref_path).parent) if e.ref_path else (self._cfg.last_refs_dir or str(self._pl.manifest.project_path.parent))
        res = self._window.create_file_dialog(webview.FileDialog.OPEN, directory=start,
                                             file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",))
        if not res:
            return {"ok": False}
        path = res[0] if isinstance(res, (list, tuple)) else res
        e.ref_path = str(path)
        self._cfg.last_refs_dir = str(Path(path).parent)
        self._cfg.save()
        self._pl.manifest.save()
        if any(k.vlm_mapping for k in e.keys):
            self._on_log("  референс змінено — заливки перемальовано зі збережених відповідей")
            self._pl.refill_all()
        return {"ok": True, "state": self._state()}

    def open_folder(self) -> dict:
        m = self._pl.manifest
        if m and m.root.exists():
            os.startfile(m.root)  # noqa: S606
        return {"ok": True}

    # ---- settings
    def get_settings(self) -> dict:
        c = self._cfg
        return {"apiKey": get_api_key(), "model": c.vlm_model, "reasoning": c.vlm_reasoning,
                "modelFollow": c.vlm_model_follow, "concurrency": c.concurrency, "port": c.rpc_port,
                "est": c.vlm_est_cost, "estFollow": c.vlm_est_cost_follow}

    def save_settings(self, s: dict) -> dict:
        c = self._cfg
        set_api_key(s.get("apiKey", ""))
        c.vlm_model = (s.get("model") or c.vlm_model).strip()
        c.vlm_reasoning = bool(s.get("reasoning"))
        c.concurrency = max(1, min(16, int(s.get("concurrency") or c.concurrency)))
        c.rpc_port = int(s.get("port") or c.rpc_port)
        c.vlm_est_cost = float(s.get("est") or c.vlm_est_cost)
        c.vlm_model_follow = (s.get("modelFollow") or c.vlm_model_follow).strip()
        c.vlm_est_cost_follow = float(s.get("estFollow") or c.vlm_est_cost_follow)
        c.save()
        return {"ok": True, "state": self._state()}

    def check_key(self, key: str, model: str) -> dict:
        from tintr.core.vlm import VLMColorizer

        try:
            VLMColorizer(key.strip(), model.strip()).check_key()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
