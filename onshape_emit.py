"""onshape_emit.py — emit a featuretree IR into an Onshape Part Studio via onpy.

The featuretree Onshape backend. One IR drives FreeCAD (fc_build) and Onshape (here). Onshape
geometry is created through onpy (its BTM is known-correct). onpy's METRIC unit system is METERS,
and the IR is in mm, so dimensions are scaled by 0.001.

    python onshape_emit.py <ir.json> <document_id>     # emit into an existing (public) doc
    from onshape_emit import emit; emit(spec, doc_id)

v0: sketches (polys / circles / rects) on the Top plane, pad, pocket (through/blind). Fillets and
face-attached sketches are not emitted yet (build123d/FreeCAD still cover those).
"""
import json
import sys

import onpy

SCALE = 0.001  # mm -> m (onpy METRIC == meters)


def _add_entities(sk, f):
    for poly in f.get("polys", []):
        pts = [(x * SCALE, y * SCALE) for (x, y) in poly]
        n = len(pts)
        if n > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]; n -= 1
        for i in range(n):
            sk.add_line(pts[i], pts[(i + 1) % n])
    for (cx, cy, r) in f.get("circles", []):
        sk.add_circle(center=(cx * SCALE, cy * SCALE), radius=r * SCALE)
    for (w, h, cx, cy) in f.get("rects", []):
        sk.add_corner_rectangle((cx * SCALE - w * SCALE / 2, cy * SCALE - h * SCALE / 2),
                                (cx * SCALE + w * SCALE / 2, cy * SCALE + h * SCALE / 2))


def emit(spec, doc_id):
    client = onpy.Client(units="metric")
    doc = client.get_document(doc_id)
    ps = doc.get_partstudio()
    sketches = {}
    for f in spec["features"]:
        kind = f["kind"]
        if kind == "sketch":
            sk = ps.add_sketch(plane=ps.features.top_plane, name=f["name"])
            _add_entities(sk, f)
            sketches[f["name"]] = sk
        elif kind == "pad":
            sk = sketches[f["sketch"]]
            ps.add_extrude(faces=sk.faces, distance=f["length"] * SCALE, name=f["name"])
        elif kind == "pocket":
            sk = sketches[f["sketch"]]
            parts = ps.list_parts()
            depth = (f.get("length") or 1000.0) * SCALE   # through -> overshoot, clipped by the part
            ps.add_extrude(faces=sk.faces, distance=depth, name=f["name"],
                           subtract_from=parts[0] if parts else None)
        elif kind == "fillet":
            print(f"  (skipping fillet '{f['name']}' — not supported by the onpy backend yet)")
        else:
            raise ValueError(f"unknown feature kind: {kind}")
    return ps, doc


if __name__ == "__main__":
    spec = json.load(open(sys.argv[1]))
    doc_id = sys.argv[2]
    ps, doc = emit(spec, doc_id)
    print("emitted to Onshape:", [p.name for p in ps.list_parts()])
    print("open: https://cad.onshape.com/documents/" + doc_id)
