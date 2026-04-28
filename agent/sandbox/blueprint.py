"""Blueprint = deterministic build plan handed from Planner -> Executor.

Wire format is a set of OpenTTD signs. Each sign carries one fact at one
tile; the executor reads them and builds in order. Sign text is kept under
32 chars so legacy OpenTTD versions don't truncate.

Sign grammar (all start with DLF:bp:<job>:):
  DLF:bp:<job>:S:fr=<tile>:eg=<id>   placed at station-A tile
  DLF:bp:<job>:E:fr=<tile>           placed at station-B tile
  DLF:bp:<job>:W:<n>                 placed at every road-path tile in order
  DLF:bp:<job>:D:fr=<tile>           placed at depot tile

The path waypoints implicitly include the road tiles immediately adjacent
to each station (W:0 borders S, W:N borders E). The executor builds road
between consecutive waypoint tiles, no A* required at runtime.

Admin-cmd JSON (sent over the admin GameScript channel) is the same
information serialized once for the bridge GS to expand into signs:

    {"cmd":"blueprint","job":1,
     "stations":[[a, fa],[b, fb]],
     "path":[t0, t1, ..., tN],
     "depot":[d, fd],
     "engine":-1}

`engine`=-1 means "executor picks best buildable passenger road engine."
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class Blueprint:
    job_id: int
    station_a: Tuple[int, int]    # (tile, road_front_tile)
    station_b: Tuple[int, int]
    path: List[int]               # ordered road tile sequence between fronts
    depot: Tuple[int, int]        # (tile, front)
    engine: int = -1              # -1 = executor chooses

    def validate(self) -> None:
        if self.job_id < 0 or self.job_id > 999:
            raise ValueError(f"job_id {self.job_id} out of range (0..999)")
        if not self.path:
            raise ValueError("path must contain at least one waypoint tile")
        if len(self.path) > 200:
            raise ValueError(f"path too long ({len(self.path)} > 200)")
        for label, (tile, front) in [
            ("station_a", self.station_a),
            ("station_b", self.station_b),
            ("depot", self.depot),
        ]:
            if tile < 0 or front < 0:
                raise ValueError(f"{label} has negative tile/front")

    def to_admin_cmd(self) -> str:
        self.validate()
        return json.dumps({
            "cmd": "blueprint",
            "job": self.job_id,
            "stations": [list(self.station_a), list(self.station_b)],
            "path": list(self.path),
            "depot": list(self.depot),
            "engine": self.engine,
        }, separators=(",", ":"))

    def to_signs(self) -> List[Tuple[int, str]]:
        """Return (tile, text) pairs the bridge GS would place. Useful for
        sandbox bootstrap where we skip the admin channel and place signs
        directly via a one-shot GS."""
        self.validate()
        j = self.job_id
        out: List[Tuple[int, str]] = [
            (self.station_a[0], f"DLF:bp:{j}:S:fr={self.station_a[1]}:eg={self.engine}"),
            (self.station_b[0], f"DLF:bp:{j}:E:fr={self.station_b[1]}"),
            (self.depot[0],     f"DLF:bp:{j}:D:fr={self.depot[1]}"),
        ]
        for n, t in enumerate(self.path):
            out.append((t, f"DLF:bp:{j}:W:{n}"))
        for tile, text in out:
            if len(text) > 31:
                raise ValueError(f"sign text >31 chars: {text!r}")
        return out


def encode_admin_cmd(bp: Blueprint) -> str:
    return bp.to_admin_cmd()


_TAG_S, _TAG_E, _TAG_W, _TAG_D = "S", "E", "W", "D"


def decode_signs(signs: List[Tuple[int, str]]) -> List[Blueprint]:
    """Parse a list of (tile, sign_text) pairs back into Blueprint(s).
    Signs not matching DLF:bp:* are ignored. Useful in tests + executor sim."""
    by_job: dict[int, dict] = {}
    for tile, text in signs:
        if not text.startswith("DLF:bp:"):
            continue
        rest = text[len("DLF:bp:"):]
        try:
            job_str, tag, *kvs = rest.split(":")
            job = int(job_str)
        except ValueError:
            continue
        slot = by_job.setdefault(job, {"path": {}, "engine": -1})
        if tag == _TAG_S and kvs:
            kv = dict(p.split("=") for p in kvs if "=" in p)
            slot["station_a"] = (tile, int(kv["fr"]))
            slot["engine"] = int(kv.get("eg", -1))
        elif tag == _TAG_E and kvs:
            kv = dict(p.split("=") for p in kvs if "=" in p)
            slot["station_b"] = (tile, int(kv["fr"]))
        elif tag == _TAG_D and kvs:
            kv = dict(p.split("=") for p in kvs if "=" in p)
            slot["depot"] = (tile, int(kv["fr"]))
        elif tag == _TAG_W and kvs:
            try:
                idx = int(kvs[0])
                slot["path"][idx] = tile
            except ValueError:
                continue

    out: List[Blueprint] = []
    for job_id, slot in by_job.items():
        if not all(k in slot for k in ("station_a", "station_b", "depot")):
            continue
        if not slot["path"]:
            continue
        path_sorted = [slot["path"][i] for i in sorted(slot["path"])]
        out.append(Blueprint(
            job_id=job_id,
            station_a=slot["station_a"],
            station_b=slot["station_b"],
            path=path_sorted,
            depot=slot["depot"],
            engine=slot["engine"],
        ))
    return out


if __name__ == "__main__":
    bp = Blueprint(
        job_id=1,
        station_a=(12345, 12346),
        station_b=(99999, 99998),
        path=[12346, 12347, 12348, 99998],
        depot=(12350, 12351),
        engine=-1,
    )
    print("admin cmd:", bp.to_admin_cmd())
    print("signs:")
    for t, s in bp.to_signs():
        print(f"  tile={t:>6}  text={s!r}  len={len(s)}")
    rt = decode_signs(bp.to_signs())
    assert len(rt) == 1 and rt[0] == bp, "round-trip failed"
    print("round-trip ok")
