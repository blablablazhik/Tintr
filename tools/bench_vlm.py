"""Compare VLMs on a real anchor: speed, tokens, cost and agreement with the colours you approved.

Uses the project's manifest.json (needs an approved anchor) — no TVPaint needed. Each model gets the
exact prompt and images Tintr sends; reasoning off first (falls back to on when the model requires it).

    .venv\\Scripts\\python.exe tools\\bench_vlm.py test_materials\\BL_88_2.tvpp ^
        --models google/gemini-3.8-flash openai/gpt-6-luna --runs 1 [--follow 3] [--reasoning both]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tintr.config import app_dir, get_api_key  # noqa: E402
from tintr.core import manifest as M  # noqa: E402
from tintr.core import vlm as V  # noqa: E402
from tintr.core.palette import reference_palette  # noqa: E402


def agreement(mapping: dict, truth: dict, regs: V.Regions) -> tuple[float, float, list[str]]:
    """(share of regions, share of pixels) whose colour equals the approved one; plus the mismatches."""
    areas = np.bincount(regs.reg.ravel(), minlength=regs.n + 1)
    ok_n = ok_px = tot_px = 0
    bad = []
    for r in range(1, regs.n + 1):
        want = str(truth.get(str(r), {}).get("color", "")).upper()
        got = str((mapping.get(str(r)) or {}).get("color", "")).upper()
        tot_px += areas[r]
        if want == got:
            ok_n += 1
            ok_px += areas[r]
        else:
            bad.append(f"{r}:{truth.get(str(r), {}).get('part', '?')} {want}→{got or '—'}")
    return ok_n / max(1, regs.n), ok_px / max(1, tot_px), bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", help="path to the .tvpp (its _tintr folder holds manifest.json)")
    ap.add_argument("--layer", help="layer uid (clip:layer); default: first layer with an approved anchor")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--follow", type=int, help="also time a follow-the-anchor call for this key idx")
    ap.add_argument("--reasoning", choices=["off", "on", "both"], default="off")
    ap.add_argument("--out", help="write results JSON here")
    a = ap.parse_args()

    m = M.Manifest.load(Path(a.project))
    layers = [e for e in m.layers.values() if e.anchor_approved and (not a.layer or e.uid == a.layer)]
    if not layers:
        print("No layer with an approved anchor in", m.file)
        return 1
    e = layers[0]
    anchor = e.key(e.anchor_idx)
    truth = e.anchor_approved_mapping
    ref = Path(e.ref_path)
    pal = reference_palette(ref)

    def load(k):
        from PIL import Image
        rgba = np.array(Image.open(m.abs(e, k.key_file)).convert("RGBA"))
        regs = V.load_regions(m.sub(e, "regions") / f"{Path(k.key_file).stem}.npz", k.hash) or V.find_regions(rgba)
        return rgba, regs

    a_rgba, a_regs = load(anchor)
    a_fill = V.fill(a_regs, truth, pal)
    fk = e.key(a.follow) if a.follow else None
    if fk:
        f_rgba, f_regs = load(fk)
        f_truth = fk.vlm_mapping  # colours currently used on that key (anchor-followed / edited by you)
    print(f"Layer {e.layer_name}: anchor #{anchor.idx} with {a_regs.n} regions"
          + (f", follow key #{fk.idx} with {f_regs.n} regions" if fk else "") + "\n")

    modes = {"off": [False], "on": [True], "both": [False, True]}[a.reasoning]
    rows = []
    for model in a.models:
        for reasoning in modes:
            for run in range(1, a.runs + 1):
                v = V.VLMColorizer(get_api_key(), model, reasoning=reasoning, url_cache=app_dir() / "upload_cache.json")
                row = {"model": model, "asked_reasoning": reasoning, "run": run}
                try:
                    t = time.time()
                    mp, cost, _ = v.color_anchor(a_rgba, a_regs, ref, pal, "bench")
                    row.update(anchor_s=round(time.time() - t, 1), anchor_vlm_s=round(v.last["seconds"], 1),
                               tokens=v.last["tokens"], reasoning=v.last["reasoning"], cost=round(cost, 4))
                    row["acc_regions"], row["acc_pixels"], row["wrong"] = agreement(mp, truth, a_regs)
                    if fk:
                        t = time.time()
                        mp2, cost2, _ = v.color_follow(f_rgba, f_regs, a_rgba, a_regs, truth, a_fill, ref, pal, "bench")
                        row.update(follow_s=round(time.time() - t, 1), follow_tokens=v.last["tokens"],
                                   follow_cost=round(cost2, 4))
                        row["follow_acc_pixels"] = agreement(mp2, f_truth, f_regs)[1]
                except Exception as ex:  # noqa: BLE001
                    row["error"] = str(ex)[:200]
                rows.append(row)
                if "error" in row:
                    print(f"{model:32s} r={reasoning!s:5}  ERROR {row['error']}")
                else:
                    print(f"{model:32s} r={row['reasoning']!s:5} anchor {row['anchor_vlm_s']:6.1f}s "
                          f"{row['tokens'] or 0:6} tok ${row['cost']:.4f}  ok {row['acc_regions']:.0%} "
                          f"regions / {row['acc_pixels']:.0%} px"
                          + (f" | follow {row['follow_s']:5.1f}s {row['follow_acc_pixels']:.0%} px" if fk else "")
                          + (f"  wrong: {', '.join(row['wrong'])}" if row["wrong"] else ""))
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
