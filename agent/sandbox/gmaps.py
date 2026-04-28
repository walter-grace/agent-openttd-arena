"""Parse Google Maps URLs to a (lat, lon, zoom) center, then a bbox.

Google Maps URLs don't expose a bounding box directly - only a center
point and a viewport zoom. We extract those and approximate the bbox
using standard mercator pixel math, sized to a typical desktop viewport.

Supported URL shapes:
  https://www.google.com/maps/@51.5074,-0.1278,12z
  https://www.google.com/maps/place/London/@51.5073,-0.1276,11z/data=!3m1!4b1!4m6!3m5!1s0x...
  https://www.google.com/maps/search/cities/@51.5,-0.1,10z
  https://maps.app.goo.gl/abc123          (short link, HTTP-resolved)
  https://goo.gl/maps/xyz                 (legacy short link)

If a URL has both `@lat,lon,zoom` and `!3d<lat>!4d<lon>`, prefer the @
form (that's what was framed in the user's viewport). The !3d!4d form
is the destination/place - falls back to it with a default zoom of 12.

Note: Google Maps URLs do NOT contain Google's tile data. We only read
the public link path. Town data still comes from OSM via geo.py.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+),(\d+(?:\.\d+)?)z")
_DEST_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_SHORT_HOSTS = ("maps.app.goo.gl", "goo.gl/maps")


@dataclass
class Center:
    lat: float
    lon: float
    zoom: float    # may be fractional in some URLs


def resolve_short(url: str, timeout_s: float = 10.0) -> str:
    """If the URL is a short link, follow redirects to the canonical form.
    Otherwise return as-is."""
    if not any(h in url for h in _SHORT_HOSTS):
        return url
    if httpx is None:
        raise RuntimeError("httpx required to resolve short links")
    with httpx.Client(follow_redirects=True, timeout=timeout_s) as client:
        r = client.get(url)
        # str(r.url) is the final URL after all redirects.
        return str(r.url)


def parse_center(url: str) -> Optional[Center]:
    """Extract (lat, lon, zoom) from a Google Maps URL. Returns None if
    we can't find anything usable. Caller decides whether to call
    `resolve_short` first."""
    m = _AT_RE.search(url)
    if m:
        return Center(lat=float(m.group(1)),
                      lon=float(m.group(2)),
                      zoom=float(m.group(3)))
    m = _DEST_RE.search(url)
    if m:
        return Center(lat=float(m.group(1)),
                      lon=float(m.group(2)),
                      zoom=12.0)
    return None


def bbox_from_center(c: Center,
                     viewport_w_px: int = 1024,
                     viewport_h_px: int = 768
                     ) -> Tuple[float, float, float, float]:
    """Approximate the bbox a viewport at this zoom would frame.

    Mercator math:
      meters_per_pixel = 156543.03392 * cos(lat) / 2^zoom
      lon_span_deg     = w_px * meters_per_pixel / 111_320
      lat_span_deg     = h_px * meters_per_pixel / 110_540

    Returns (lat_south, lon_west, lat_north, lon_east) - the same shape
    that geo.fetch_osm_towns() expects.
    """
    if c.zoom < 1 or c.zoom > 22:
        raise ValueError(f"zoom {c.zoom} out of range")
    mpp = 156543.03392 * math.cos(math.radians(c.lat)) / (2 ** c.zoom)
    lon_span = viewport_w_px * mpp / 111320.0
    lat_span = viewport_h_px * mpp / 110540.0
    return (
        c.lat - lat_span / 2,
        c.lon - lon_span / 2,
        c.lat + lat_span / 2,
        c.lon + lon_span / 2,
    )


def url_to_bbox(url: str,
                viewport_w_px: int = 1024,
                viewport_h_px: int = 768
                ) -> Tuple[Center, Tuple[float, float, float, float]]:
    """One-shot: resolve short link if needed, parse, compute bbox.
    Raises ValueError if no center could be extracted."""
    expanded = resolve_short(url) if any(h in url for h in _SHORT_HOSTS) else url
    c = parse_center(expanded)
    if c is None:
        raise ValueError(f"no center in URL: {expanded[:200]}")
    return c, bbox_from_center(c, viewport_w_px, viewport_h_px)


def climate_for_lat(lat: float) -> str:
    """Pick OpenTTD climate from latitude. Rough but sensible:
      |lat| > 50  -> sub-arctic   (Scandinavia, Russia, Canada, Patagonia)
      |lat| < 24  -> sub-tropical (tropics, deserts)
      otherwise   -> temperate    (mid-latitudes)
    """
    abs_lat = abs(lat)
    if abs_lat > 50:
        return "sub-arctic"
    if abs_lat < 24:
        return "sub-tropical"
    return "temperate"


if __name__ == "__main__":
    samples = [
        "https://www.google.com/maps/@51.5074,-0.1278,12z",
        "https://www.google.com/maps/place/London/@51.5073219,-0.1276474,11z/data=!3m1!4b1!4m6!3m5",
        "https://www.google.com/maps/search/cities/@40.7128,-74.0060,10.5z",
        "https://www.google.com/maps/place/Paris/!3d48.8566!4d2.3522",
    ]
    for u in samples:
        try:
            c, bbox = url_to_bbox(u)
            print(f"{u}")
            print(f"  center=({c.lat}, {c.lon}) z={c.zoom}")
            print(f"  bbox  ={bbox}")
        except Exception as e:
            print(f"{u}\n  ERR: {e}")
