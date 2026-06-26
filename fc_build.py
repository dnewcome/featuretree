"""fc_build.py — emit a FreeCAD .FCStd feature tree from the IR.

RUNS UNDER freecadcmd (FreeCAD's Python 3.11), not the project interpreter.
Inputs via env: FC_IR (ir json path), FC_OUT (.FCStd path). (Paths can't be argv —
freecadcmd opens path args as documents.) Prints a line "RESULT:" + json (tree,
final volume, as-built params).

Each feature becomes a native PartDesign object with Label = the IR name, so a human
edit in FreeCAD can be matched back by name (see fc_read.py).
"""

import json
import os
import sys

import FreeCAD as App
import Part

sys.path.insert(0, os.environ.get("FC_LIBDIR", os.path.dirname(os.path.abspath(__file__))))
import fc_common  # noqa: E402


def _add_rect(sk, w, h, cx, cy):
    hw, hh = w / 2.0, h / 2.0
    pts = [App.Vector(cx - hw, cy - hh, 0), App.Vector(cx + hw, cy - hh, 0),
           App.Vector(cx + hw, cy + hh, 0), App.Vector(cx - hw, cy + hh, 0)]
    for i in range(4):
        sk.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]), False)


def _add_poly(sk, pts):
    """Closed polyline from [(x, y), ...] — drops a duplicate closing vertex if present."""
    vs = [App.Vector(x, y, 0) for (x, y) in pts]
    if len(vs) > 1 and (vs[0] - vs[-1]).Length < 1e-7:
        vs = vs[:-1]
    n = len(vs)
    for i in range(n):
        sk.addGeometry(Part.LineSegment(vs[i], vs[(i + 1) % n]), False)


def build(spec, out_path):
    doc = App.newDocument(spec["name"])
    body = doc.addObject("PartDesign::Body", "Body")
    sketches = {}
    tip = None      # last solid-producing feature (fillet bases reference it)

    for f in spec["features"]:
        kind = f["kind"]
        if kind == "sketch":
            sk = body.newObject("Sketcher::SketchObject", f["name"])
            sk.Label = f["name"]
            on = f.get("on")
            if on:                                  # attach to a face chosen by query
                if f.get("rects") or f.get("polys"):
                    raise ValueError("face-attached sketches support circles only (v0)")
                face = fc_common.resolve_face(tip.Shape, on.get("side", "top"))
                sk.AttachmentSupport = [(tip, [face])]
                sk.MapMode = "FlatFace"
                doc.recompute()                     # so sk.Placement is resolved
                zref = max(v.Z for v in tip.Shape.Vertexes) if on.get("side", "top") == "top" \
                    else min(v.Z for v in tip.Shape.Vertexes)
                inv = sk.Placement.inverse()
                for (cx, cy, r) in f["circles"]:    # global -> sketch-local coords
                    lp = inv.multVec(App.Vector(cx, cy, zref))
                    sk.addGeometry(Part.Circle(App.Vector(lp.x, lp.y, 0), App.Vector(0, 0, 1), r), False)
            else:
                if f["plane"] != "XY":
                    raise ValueError("unattached sketches must be on XY (v0)")
                for (cx, cy, r) in f["circles"]:
                    sk.addGeometry(Part.Circle(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), r), False)
                for (w, h, cx, cy) in f["rects"]:
                    _add_rect(sk, w, h, cx, cy)
                for poly in f.get("polys", []):
                    _add_poly(sk, poly)
            sketches[f["name"]] = sk
        elif kind == "pad":
            p = body.newObject("PartDesign::Pad", f["name"])
            p.Label = f["name"]
            p.Profile = sketches[f["sketch"]]
            p.Length = f["length"]
            p.Midplane = bool(f["symmetric"])
            tip = p
        elif kind == "pocket":
            p = body.newObject("PartDesign::Pocket", f["name"])
            p.Label = f["name"]
            p.Profile = sketches[f["sketch"]]
            if f["through"]:
                p.Type = "ThroughAll"
                p.Midplane = True            # cut both ways -> robust without a face attach
            else:
                p.Length = f["length"]
            tip = p
        elif kind == "fillet":
            fl = body.newObject("PartDesign::Fillet", f["name"])
            fl.Label = f["name"]
            edges = fc_common.resolve_edges(tip.Shape, f["select"])  # QUERY -> live EdgeN
            if not edges:
                raise ValueError(f"fillet '{f['name']}' selected no edges")
            fl.Base = (tip, edges)
            fl.Radius = f["radius"]
            tip = fl
        else:
            raise ValueError(f"unknown feature kind: {kind}")
        doc.recompute()

    doc.recompute()
    # A headless (freecadcmd) doc has no GUI visibility state, so it opens ALL-HIDDEN in the
    # FreeCAD GUI. Set App-level Visibility (persists to the file, the GUI honours it): show the
    # Body + final tip solid, hide sketches/intermediate features.
    for o in doc.Objects:
        if hasattr(o, "Visibility"):
            o.Visibility = (o is body or o is tip)
    doc.saveAs(out_path)
    stl = out_path[:-6] + ".stl" if out_path.endswith(".FCStd") else out_path + ".stl"
    body.Shape.exportStl(stl)
    return fc_common.result(doc)


# freecadcmd execs this file but not as __main__, so run at top level.
_spec = json.load(open(os.environ["FC_IR"]))
_result = build(_spec, os.environ["FC_OUT"])
print("RESULT:" + json.dumps(_result))
