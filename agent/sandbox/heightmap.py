"""Generate an OpenTTD heightmap PNG from a lat/lon bbox.

Pulls elevation from AWS Mapzen Terrain Tiles ("terrarium" PNGs):
free, no API key, 30m SRTM globally between roughly 60S and 60N.

Encoding: each tile is a PNG where R/G/B channels encode meters.
    elevation_m = (R * 256 + G + B / 256) - 32768

Pipeline:
  bbox + zoom  ->  list of tile coords
  fetch tiles in parallel
  stitch into one big array
  crop to bbox
  decode terrarium -> elevation in meters (int16)
  resize to OpenTTD-legal output dimensions
  normalize: anything <= sea_level_m becomes 0; rest scales 1..255
  save grayscale PNG

OpenTTD heightmap rules:
  - dimensions in {64, 128, 256, 512, 1024, 2048, 4096}
  - aspect ratios in {1:1, 1:2, 1:4, 1:8} (or inverse)
  - grayscale; 0 = sea, 255 = peak

Usage:

    from sandbox.heightmap import generate_heightmap
    path = generate_heightmap(
        bbox=(35.30, 36.07, 36.17, 37.21),
        out_path=Path("/tmp/idlib_heightmap.png"),
        size=1024,
        sea_level_m=0,
    )
"""
from __future__ import annotations

import io
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Tuple

try:
    import httpx
    import numpy as np
    from PIL import Image
except ImportError as e:
    raise ImportError(
        "heightmap.py requires httpx + numpy + Pillow. "
        "Install: pip3 install --user httpx numpy pillow"
    ) from e


TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_PX = 256
LEGAL_DIMS = (64, 128, 256, 512, 1024, 2048, 4096)


# ---------------------------------------------------------------------------
# Mercator tile math
# ---------------------------------------------------------------------------

def lat_lon_to_tile_xy(lat: float, lon: float, zoom: int) -> Tuple[float, float]:
    """Slippy-tile mercator projection. Returns FRACTIONAL tile coords so
    we can compute exact pixel offsets for a bbox crop."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n
    return x, y


def choose_zoom(bbox: Tuple[float, float, float, float], output_px: int) -> int:
    """Pick a zoom that yields ~2x the output resolution before resize.
    Wider bboxes need lower zoom; small city bboxes need higher."""
    s, w, n, e = bbox
    # Use the wider dimension; slightly favors longitude since high-latitude
    # pixels have less ground per pixel due to mercator stretch.
    span_deg = max(e - w, n - s)
    if span_deg <= 0:
        return 12
    target_tiles = (output_px * 2) / TILE_PX
    z_float = math.log2(target_tiles * 360.0 / span_deg)
    return max(2, min(13, int(round(z_float))))


# ---------------------------------------------------------------------------
# Fetch + stitch
# ---------------------------------------------------------------------------

def _fetch_tile(client: "httpx.Client", z: int, x: int, y: int) -> "np.ndarray":
    url = TILE_URL.format(z=z, x=x, y=y)
    r = client.get(url, timeout=15.0)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    return np.array(img, dtype=np.uint8)  # shape (256, 256, 3)


def _fetch_grid(bbox: Tuple[float, float, float, float], zoom: int,
                 max_workers: int = 8) -> Tuple["np.ndarray", Tuple[int, int, int, int]]:
    """Fetch all tiles covering bbox at zoom; return stitched RGB array
    plus the (x_min, y_min, x_max, y_max) tile-coord bounds (inclusive)."""
    s, w, n, e = bbox
    # Tile bounds (note y is flipped: north has lower y in tile space)
    x_a, y_a = lat_lon_to_tile_xy(n, w, zoom)   # NW
    x_b, y_b = lat_lon_to_tile_xy(s, e, zoom)   # SE
    x_min = int(math.floor(x_a))
    y_min = int(math.floor(y_a))
    x_max = int(math.floor(x_b))
    y_max = int(math.floor(y_b))
    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    if cols * rows > 256:
        raise RuntimeError(
            f"too many tiles ({cols}x{rows}={cols*rows}) for zoom {zoom}; "
            "bbox too wide for that detail level - lower zoom or smaller bbox"
        )
    out = np.zeros((rows * TILE_PX, cols * TILE_PX, 3), dtype=np.uint8)
    headers = {"User-Agent": "agent-openttd-arena/0.1"}
    with httpx.Client(headers=headers, http2=False) as client:
        def task(coord):
            r, c = coord
            tile = _fetch_tile(client, zoom, x_min + c, y_min + r)
            return r, c, tile
        coords = [(r, c) for r in range(rows) for c in range(cols)]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for r, c, tile in pool.map(task, coords):
                out[r*TILE_PX:(r+1)*TILE_PX, c*TILE_PX:(c+1)*TILE_PX] = tile
    return out, (x_min, y_min, x_max, y_max)


def _crop_to_bbox(stitched: "np.ndarray",
                   bbox: Tuple[float, float, float, float],
                   zoom: int,
                   tile_bounds: Tuple[int, int, int, int]) -> "np.ndarray":
    s, w, n, e = bbox
    x_min, y_min, _, _ = tile_bounds
    x_a, y_a = lat_lon_to_tile_xy(n, w, zoom)
    x_b, y_b = lat_lon_to_tile_xy(s, e, zoom)
    px_left   = int(round((x_a - x_min) * TILE_PX))
    px_top    = int(round((y_a - y_min) * TILE_PX))
    px_right  = int(round((x_b - x_min) * TILE_PX))
    px_bottom = int(round((y_b - y_min) * TILE_PX))
    return stitched[px_top:px_bottom, px_left:px_right]


def _decode_elevation(rgb: "np.ndarray") -> "np.ndarray":
    """Terrarium decoding: meters (signed). Returns float32 array."""
    r = rgb[:, :, 0].astype(np.int32)
    g = rgb[:, :, 1].astype(np.int32)
    b = rgb[:, :, 2].astype(np.int32)
    return (r * 256 + g + b / 256.0).astype(np.float32) - 32768.0


# ---------------------------------------------------------------------------
# OpenTTD output sizing + normalization
# ---------------------------------------------------------------------------

def _legal_dims_for_bbox(bbox: Tuple[float, float, float, float],
                          target: int) -> Tuple[int, int]:
    """Pick (width, height) from {64..4096} with aspect 1:1, 1:2, 1:4, 1:8
    (or inverse) closest to the bbox aspect."""
    s, w, n, e = bbox
    bbox_aspect = (e - w) / max(n - s, 1e-9)  # width / height in degrees
    # Mercator stretches lat at high latitudes; correct using middle latitude.
    mid_lat = math.radians((n + s) / 2)
    bbox_aspect = bbox_aspect / max(math.cos(mid_lat), 0.01)
    legal_aspects = [1/8, 1/4, 1/2, 1, 2, 4, 8]
    chosen = min(legal_aspects, key=lambda a: abs(math.log(a) - math.log(bbox_aspect)))
    if chosen >= 1:
        # Wider than tall.
        w_dim = target
        h_dim = max(64, int(target / chosen))
    else:
        h_dim = target
        w_dim = max(64, int(target * chosen))
    # Snap each to nearest legal power-of-2 dimension.
    def _snap(d):
        return min(LEGAL_DIMS, key=lambda x: abs(x - d))
    return _snap(w_dim), _snap(h_dim)


def _normalize_to_png(elev: "np.ndarray", sea_level_m: float) -> "np.ndarray":
    """Map elevation -> 0..255 grayscale. Anything at or below sea_level_m
    becomes 0 (water in OpenTTD). Land scales linearly 1..255."""
    land_mask = elev > sea_level_m
    if not land_mask.any():
        return np.zeros(elev.shape, dtype=np.uint8)
    peak = float(elev[land_mask].max())
    if peak <= sea_level_m:
        return np.zeros(elev.shape, dtype=np.uint8)
    out = np.zeros(elev.shape, dtype=np.uint8)
    span = peak - sea_level_m
    scaled = (elev - sea_level_m) / span * 254.0 + 1.0  # 1..255 for land
    out[land_mask] = np.clip(scaled[land_mask], 1, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_heightmap(bbox: Tuple[float, float, float, float],
                        out_path: Path,
                        size: int = 1024,
                        sea_level_m: float = 0.0,
                        zoom: Optional[int] = None,
                        smooth_sigma_px: Optional[float] = None,
                        max_workers: int = 8) -> dict:
    """Fetch, stitch, decode, smooth, resize, save. Returns metadata.

    `smooth_sigma_px` applies a Gaussian blur (in input-pixel units) to
    the elevation float array before resize.
      - None (default): auto = size / 256  (1024 -> 4, 2048 -> 8, 4096 -> 16)
      - 0: disable smoothing
      - explicit float: override

    Auto-scaling matters: a sigma tuned for 1024-wide maps is too soft
    on a 2048 map and not soft enough on 4096. The ratio keeps the
    relative smoothing constant.
    """
    if zoom is None:
        zoom = choose_zoom(bbox, size)
    if smooth_sigma_px is None:
        smooth_sigma_px = max(1.0, size / 256.0)
    stitched, tile_bounds = _fetch_grid(bbox, zoom, max_workers=max_workers)
    cropped = _crop_to_bbox(stitched, bbox, zoom, tile_bounds)
    elev = _decode_elevation(cropped)
    if smooth_sigma_px and smooth_sigma_px > 0:
        try:
            from scipy.ndimage import gaussian_filter
            # Clamp deep ocean to sea level BEFORE smoothing. Otherwise
            # averaging a -5000m trench with a +20m coastal plain pulls
            # the plain underwater. We want the coast to stay land.
            elev_for_smooth = np.maximum(elev, sea_level_m - 1)
            elev_smooth = gaussian_filter(elev_for_smooth, sigma=smooth_sigma_px)
        except ImportError:
            # Fallback: PIL Gaussian on a temp uint8 (lossy precision).
            tmp = _normalize_to_png(elev, sea_level_m)
            from PIL import ImageFilter
            tmp_img = Image.fromarray(tmp).filter(
                ImageFilter.GaussianBlur(radius=smooth_sigma_px))
            elev_smooth = np.array(tmp_img, dtype=np.float32)
            # Re-derive a sea_level so normalize_to_png handles it consistently.
            sea_level_m = -1
    else:
        elev_smooth = elev
    out_w, out_h = _legal_dims_for_bbox(bbox, size)
    img = Image.fromarray(_normalize_to_png(elev_smooth, sea_level_m))
    img = img.resize((out_w, out_h), Image.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return {
        "path": str(out_path),
        "zoom": zoom,
        "tile_count": (tile_bounds[2] - tile_bounds[0] + 1)
                       * (tile_bounds[3] - tile_bounds[1] + 1),
        "width": out_w,
        "height": out_h,
        "elev_min_m": float(elev.min()),
        "elev_max_m": float(elev.max()),
        "sea_level_m": sea_level_m,
        "smooth_sigma_px": smooth_sigma_px,
    }


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True,
                    help="lat_s,lon_w,lat_n,lon_e")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--sea-level", type=float, default=0.0)
    ap.add_argument("--zoom", type=int, default=None)
    ap.add_argument("--smooth", type=float, default=2.0,
                    help="Gaussian sigma in input pixels; 0 disables")
    args = ap.parse_args()
    bbox = tuple(float(p) for p in args.bbox.split(","))
    meta = generate_heightmap(bbox, args.out, size=args.size,
                                sea_level_m=args.sea_level, zoom=args.zoom,
                                smooth_sigma_px=args.smooth)
    print(json.dumps(meta, indent=2))
