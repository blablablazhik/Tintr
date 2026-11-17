"""Draw the Tintr app icon: an ink-outlined paint drop half filled with the brand yellow.

    .venv\\Scripts\\python.exe tools\\make_icon.py      → tintr/web/static/tintr.ico (+ tintr.png)

Brand palette (app.css): ground #021019, ivory #FFFAF0, accent yellow #EFFB30, periwinkle #A3B1E6.
Drawn at 1024 px and downsampled, so it stays crisp from 256 px down to 16 px.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

S = 1024
OUT = Path(__file__).resolve().parents[1] / "tintr" / "web" / "static"

GROUND_TOP, GROUND_BOTTOM = (14, 42, 60), (2, 16, 25)
IVORY = (255, 250, 240)
YELLOW = (239, 251, 48)
YELLOW_DEEP = (205, 220, 20)
PERI = (163, 177, 230)
INSIDE = (9, 30, 44)


def drop_polygon(cx: float, cy: float, r: float, apex_y: float, n: int = 240) -> list[tuple[float, float]]:
    """A paint drop: circle of radius r at (cx, cy) with tangent lines meeting at the apex."""
    d = cy - apex_y
    a = math.asin(r / d)  # angle between the axis and a tangent line
    t = a  # where the tangents touch the circle: a above the horizontal (radius ⟂ tangent line)
    pts = [(cx, apex_y)]
    for k in range(n + 1):  # right tangent point → around the bottom → left tangent point
        ang = -t + (math.pi + 2 * t) * k / n
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def main() -> None:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # rounded-square ground with a soft vertical gradient
    g = np.linspace(0, 1, S)[:, None]
    grad = (np.array(GROUND_TOP) * (1 - g)[..., None] + np.array(GROUND_BOTTOM) * g[..., None]).astype(np.uint8)
    grad = np.repeat(grad, S, axis=1)
    ground = Image.fromarray(grad, "RGB")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, S - 24, S - 24), radius=230, fill=255)
    im.paste(ground, (0, 0), mask)

    cx, cy, r, apex = S / 2, 610, 250, 150
    poly = drop_polygon(cx, cy, r, apex)

    # inside of the drop (dark) and the paint filling its lower part with a wave on top
    drop = Image.new("L", (S, S), 0)
    ImageDraw.Draw(drop).polygon(poly, fill=255)
    inside = Image.new("RGBA", (S, S), INSIDE + (255,))
    im.paste(inside, (0, 0), drop)

    xs = np.arange(S)
    wave = 560 + 26 * np.sin((xs - cx) / 62.0)
    liquid = (np.arange(S)[:, None] >= wave[None, :]).astype(np.uint8) * 255
    liquid = Image.fromarray(liquid, "L")
    fill = Image.new("L", (S, S), 0)
    fill.paste(drop, (0, 0), liquid)
    paint = Image.new("RGBA", (S, S), YELLOW + (255,))
    # slightly deeper yellow towards the bottom for volume
    shade = np.clip((np.arange(S) - 600) / 300.0, 0, 1)[:, None, None]
    pa = np.array(paint).astype(float)
    pa[..., :3] = pa[..., :3] * (1 - shade) + np.array(YELLOW_DEEP) * shade
    paint = Image.fromarray(pa.astype(np.uint8), "RGBA")
    im.paste(paint, (0, 0), fill)

    # small periwinkle drop falling into it
    small = Image.new("L", (S, S), 0)
    ImageDraw.Draw(small).polygon(drop_polygon(cx + 230, 300, 44, 196, 120), fill=255)
    im.paste(Image.new("RGBA", (S, S), PERI + (255,)), (0, 0), small)

    # ink outline: a smooth band around the drop edge (from a distance field, anti-aliased)
    m = np.array(drop) > 127
    dist = np.where(m, ndi.distance_transform_edt(m), -ndi.distance_transform_edt(~m))
    w = 30.0  # half the line width
    cov = np.clip(w - np.abs(dist) + 0.5, 0, 1)
    line = Image.fromarray((cov * 255).astype(np.uint8), "L")
    im.paste(Image.new("RGBA", (S, S), IVORY + (255,)), (0, 0), line)
    OUT.mkdir(parents=True, exist_ok=True)
    im.resize((512, 512), Image.LANCZOS).save(OUT / "tintr.png")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    im.save(OUT / "tintr.ico", sizes=sizes)
    print("wrote", OUT / "tintr.ico", OUT / "tintr.png")


if __name__ == "__main__":
    main()
