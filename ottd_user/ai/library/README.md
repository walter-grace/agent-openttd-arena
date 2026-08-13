# Bundled AI libraries

The Nutz Executor AI depends on these standard OpenTTD community script
libraries (normally fetched from OpenTTD's in-game content service / BaNaNaS).
A headless dedicated server can't download them on demand, so they're vendored
here and copied into place by `agent/setup_arena.sh`:

| File | Library | Why |
|---|---|---|
| `5046524f-Pathfinder.Road-3.tar` | Pathfinder.Road v3 | road routing (`import("pathfinder.road", "Road", 3)`) |
| `4752412a-Graph.AyStar-4.tar` | Graph.AyStar v4 | A* graph search (Pathfinder.Road dependency) |
| `51554248-Queue.BinaryHeap-1.tar` | Queue.BinaryHeap v1 | priority queue (AyStar dependency) |

These are unmodified releases from https://bananas.openttd.org authored by the
OpenTTD community, redistributed here as build dependencies. To refresh them,
run OpenTTD and `content download` the Road Pathfinder AI library, then copy the
tars from `<personal-dir>/content_download/ai/library/`.
