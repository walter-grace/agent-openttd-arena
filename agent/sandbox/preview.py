"""Colored preview render of a generated heightmap + its town list.

The heightmap PNG that OpenTTD eats is a flat grayscale image — readable by
the game, useless to a human deciding whether a scenario is worth playing.
This renders the same data as a shaded map with the imported towns marked,
so you can eyeball two things before spending minutes on world generation:

    1. Did the terrain come out right (coastline, mountains, enough relief)?
    2. Did the towns land on land? `pct_in_water` is the QA number —
       anything above a few percent usually means a bad bbox or a
       swapped X/Y.

Convention: height 0 is water (see heightmap._normalize_to_png), and town
`x`/`y` are normalized 0..1 image coords in the same orientation as the
heightmap. If the scenario was built with `--swap-xy`, the markers here are
transposed relative to the terrain — the preview mirrors what the JSON says,
not what OpenTTD will do with it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "preview.py requires numpy + Pillow. "
        "Install: pip3 install --user numpy pillow"
    ) from exc


# (elevation 0..1, RGB) stops, interpolated linearly. Low land is green,
# mid is tan, high is rock, peaks are snow.
_LAND_RAMP = (
    (0.00, (60, 110, 55)),
    (0.25, (110, 145, 65)),
    (0.50, (170, 150, 95)),
    (0.75, (150, 120, 95)),
    (1.00, (245, 245, 250)),
)
_WATER = (32, 68, 110)
_CITY = (255, 92, 92)
_TOWN = (255, 205, 90)
_MAX_LABELS = 25


def _ramp(t: float) -> Tuple[int, int, int]:
    for (t0, c0), (t1, c1) in zip(_LAND_RAMP, _LAND_RAMP[1:]):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(int(a + (b - a) * f) for a, b in zip(c0, c1))
    return _LAND_RAMP[-1][1]


def _colorize(gray: "np.ndarray") -> "np.ndarray":
    """Grayscale heights -> RGB, with a cheap hillshade so relief reads."""
    lut = np.array([_ramp(i / 255.0) for i in range(256)], dtype=np.float32)
    rgb = lut[gray]

    # Hillshade from a north-west light: brighten slopes facing the light,
    # darken those facing away. Keeps flat terrain untouched.
    h = gray.astype(np.float32)
    dy, dx = np.gradient(h)
    shade = np.clip(1.0 + (dx + dy) * 0.06, 0.65, 1.35)[..., None]
    rgb = np.clip(rgb * shade, 0, 255)

    rgb[gray == 0] = _WATER
    return rgb.astype(np.uint8)


def render_preview(heightmap_path: Path | str,
                   towns_json_path: Path | str,
                   out_path: Path | str,
                   *, max_labels: int = _MAX_LABELS) -> dict:
    """Render `heightmap_path` in color with towns from `towns_json_path`
    marked, write it to `out_path`, and report how many towns landed in
    water."""
    heightmap_path = Path(heightmap_path)
    towns_json_path = Path(towns_json_path)
    out_path = Path(out_path)

    gray = np.array(Image.open(heightmap_path).convert("L"))
    height, width = gray.shape
    img = Image.fromarray(_colorize(gray), mode="RGB")
    draw = ImageDraw.Draw(img)

    towns = json.loads(towns_json_path.read_text())
    in_water = 0
    for i, t in enumerate(towns):
        px = int(round(float(t["x"]) * (width - 1)))
        py = int(round(float(t["y"]) * (height - 1)))
        px = max(0, min(width - 1, px))
        py = max(0, min(height - 1, py))
        if gray[py, px] == 0:
            in_water += 1

        is_city = bool(t.get("city"))
        r = 4 if is_city else 2
        fill = _CITY if is_city else _TOWN
        draw.ellipse((px - r, py - r, px + r, py + r),
                     fill=fill, outline=(20, 20, 20))
        if i < max_labels:
            draw.text((px + r + 2, py - 5), str(t.get("name", "")),
                      fill=(255, 255, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)

    n = len(towns)
    return {
        "path": str(out_path),
        "width": width,
        "height": height,
        "town_count": n,
        "towns_in_water": in_water,
        "pct_in_water": round(100.0 * in_water / n, 1) if n else 0.0,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("heightmap", help="path to <name>_heightmap.png")
    ap.add_argument("towns", help="path to <name>_towns.json")
    ap.add_argument("out", help="path to write <name>_preview.png")
    args = ap.parse_args()
    print(json.dumps(render_preview(args.heightmap, args.towns, args.out),
                     indent=2))
