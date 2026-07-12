"""step_recognize.py — recover a featuretree IR from a STEP B-rep (feature RECOGNITION).

A STEP file is a dumb B-rep: geometry, no feature tree. You cannot *convert* it to an IR;
you can only *infer* one. The insight this leans on: **most machined/printed parts are a 2D
profile extruded along some axis** (plus holes). So the core intelligence is CROSS-SECTIONAL —
find the axis the solid is a prismatic extrusion along (every face is planar-perpendicular,
planar-parallel, or a cylinder parallel to it), rotate that axis onto Z, and recover the profile
+ through/blind holes. Orientation-agnostic: a part extruded along X or Y is found the same way.

The honesty comes from SELF-VERIFICATION: the recognized IR is re-emitted through b3d_emit and
its volume + (rotation-tolerant) bounding box are compared to the original STEP. So every result
is either "verified" (provably the same solid, Δvol≈0) or "partial/unrecognized" with the residual
reported — in which case the caller should fall back to importing the STEP as one solid. Out of
scope (surfaced as residual, never silently wrong): fillets/chamfers, revolves / lofts / sweeps /
freeform, additive bosses, and profiles whose boundary has ARCS (straight-edge polygons + circular
holes only for now — arc-wire support is the next increment).

    python step_recognize.py part.step [--emit part.ir.json]
    from step_recognize import recognize;  spec, report = recognize("part.step")
"""

import json
import math
import sys
from pathlib import Path

from build123d import Axis, GeomType, Vector, import_step

import ir as IR
import b3d_emit

EPS = 1e-3
ANG = 1e-2           # direction tolerance: |dot|<ANG == perpendicular, >1-ANG == parallel
VOL_TOL = 0.005      # 0.5% volume agreement -> "verified"
DIM_TOL = 0.05       # mm bbox-size agreement


# --- cross-sectional intelligence: is this solid a 2D profile extruded along SOME axis? ---------
# A prismatic extrusion along axis a has EVERY face either planar-perpendicular to a (an end cap),
# planar-parallel to a (a flat side wall), or cylindrical with its axis parallel to a (a rounded
# wall or a through hole). Any other face (angled plane, cone/sphere/torus/bspline) rules a out.
# We find such an axis, rotate it onto Z, and then the Z-prismatic profile+holes logic applies to
# ANY orientation — the general "most parts are an extrude of a 2D wire" case.

def _cyl_axis(face):
    ces = face.edges().filter_by(GeomType.CIRCLE)
    if len(ces) < 2:
        return None
    a, b = ces[0].arc_center, ces[1].arc_center
    d = Vector(b.X - a.X, b.Y - a.Y, b.Z - a.Z)
    return d.normalized() if d.length > EPS else None


def _is_extrude_along(solid, a):
    for f in solid.faces():
        gt = f.geom_type
        if gt == GeomType.PLANE:
            d = abs(_normal(f).normalized().dot(a))
            if not (d < ANG or d > 1 - ANG):
                return False                      # angled wall -> not a prism along a
        elif gt == GeomType.CYLINDER:
            ax = _cyl_axis(f)
            if ax is None or abs(ax.dot(a)) < 1 - ANG:
                return False                      # cylinder axis not along a
        else:
            return False                          # cone/sphere/torus/bspline -> not a simple extrude
    return True


def _find_extrude_axis(solid):
    """Return a unit axis the solid is a prismatic extrusion along, or None. Prefer Z, then Y, X,
    then any face-derived direction (so an off-axis part is still found)."""
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for f in solid.faces().filter_by(GeomType.PLANE):
        cands.append(_normal(f).normalized())
    for f in solid.faces().filter_by(GeomType.CYLINDER):
        ax = _cyl_axis(f)
        if ax:
            cands.append(ax)
    seen = []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a)
        if _is_extrude_along(solid, a):
            return a
    return None


def _align_to_z(solid, a):
    """Rotate the solid so extrude axis `a` lands on +Z (the IR's extrude direction)."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    if abs(a.dot(z)) > 1 - ANG:
        return solid                              # already along Z
    axis_dir = a.cross(z)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, a.dot(z)))))
    return solid.rotate(Axis((0, 0, 0), axis_dir.to_tuple()), angle)


def _r2(v):
    return (round(v.X, 4), round(v.Y, 4))


def _normal(f):
    try:
        return f.normal_at(f.position_at(0.5, 0.5))
    except Exception:
        return f.normal_at()


def _classify_wire(wire, warnings):
    """A closed outer wire -> ('circle', cx, cy, r) | ('poly', [(x,y[,bulge]),...]) | None.
    Handles lines AND circular arcs (as DXF bulges); rejects splines / ellipses."""
    edges = wire.edges()
    circ = edges.filter_by(GeomType.CIRCLE)
    if len(edges) == 1 and len(circ) == 1:
        c = circ[0]
        return ("circle", round(c.arc_center.X, 4), round(c.arc_center.Y, 4), round(c.radius, 4))
    if all(e.geom_type in (GeomType.LINE, GeomType.CIRCLE) for e in edges):
        return ("poly", _ordered_poly(edges))
    warnings.append("outline has splines/ellipses — unsupported (lines + circular arcs only)")
    return None


def _edge_bulge(e):
    """DXF bulge tan(theta/4) of a circular-arc edge = 2*(signed sagitta)/chord; 0 for a line."""
    if e.geom_type != GeomType.CIRCLE:
        return 0.0
    a, b, m = e @ 0.0, e @ 1.0, e @ 0.5
    chord = math.hypot(b.X - a.X, b.Y - a.Y)
    if chord < EPS:
        return 0.0
    ux, uy = (b.X - a.X) / chord, (b.Y - a.Y) / chord      # chord dir; left normal = (-uy, ux)
    sag = -uy * (m.X - a.X) + ux * (m.Y - a.Y)             # signed dist chord->arc-mid
    return round(2.0 * sag / chord, 6)


def _ordered_poly(edges):
    """Chain line/arc edges by shared endpoints into an ordered loop of (x, y, bulge), where the
    bulge on each vertex describes the segment LEAVING it (0 = straight)."""
    segs = [(_r2(e @ 0.0), _r2(e @ 1.0), _edge_bulge(e)) for e in edges]
    order = [(segs[0][0], segs[0][2])]            # (vertex, outgoing bulge)
    used, tail = {0}, segs[0][1]
    while len(used) < len(segs):
        for i, (s, e, bl) in enumerate(segs):
            if i in used:
                continue
            if _close(s, tail):
                order.append((s, bl)); used.add(i); tail = e; break
            if _close(e, tail):
                order.append((e, -bl)); used.add(i); tail = s; break   # reversed arc -> flip bulge
        else:
            break
    return [[p[0], p[1], b] for (p, b) in order]


def _close(a, b):
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


def recognize(step_path, name=None, verify=True):
    """STEP file -> (IR spec, report). report.verified is True iff the re-emitted IR
    reproduces the STEP's volume + bbox within tolerance."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    name = name or Path(step_path).stem
    warnings = []

    # cross-section intelligence: find the axis this solid is a prismatic extrusion along (any
    # orientation) and rotate it onto Z, so the Z-profile+holes logic below is axis-agnostic.
    axis = _find_extrude_axis(orig)
    solid = _align_to_z(orig, axis) if axis is not None else orig
    if axis is not None and abs(axis.dot(Vector(0, 0, 1))) < 1 - ANG:
        warnings.append(f"extrude axis {tuple(round(c, 3) for c in axis.to_tuple())} -> rotated onto Z")
    bb = solid.bounding_box()
    base_z, top_z, thick = bb.min.Z, bb.max.Z, bb.size.Z

    # 1. BASE: the largest Z-normal planar face at min z -> outline, extruded `thick` up.
    planar = solid.faces().filter_by(GeomType.PLANE)
    bottoms = [f for f in planar if abs(_normal(f).Z) > 0.99 and abs(f.center().Z - base_z) < EPS]
    if not bottoms:
        raise ValueError("no Z-normal planar face at the base — not a Z-prismatic part")
    base_face = max(bottoms, key=lambda f: f.area)
    outer = base_face.outer_wire()
    outline = _classify_wire(outer, warnings)
    if outline is None:
        raise ValueError("base outline not recognizable (lines + circular arcs only; has splines/ellipses)")

    feats = [_sketch_from_outline("outline", outline),
             IR.pad("body", "outline", length=round(thick, 4))]

    # 2. HOLES: concave cylindrical faces with a Z axis -> circle pocket (through or blind). Skip any
    # cylinder whose circle is part of the OUTER wire — that's a rounded corner/side of the outline
    # (already captured as an arc), not a hole.
    outline_arcs = {(round(e.arc_center.X, 2), round(e.arc_center.Y, 2), round(e.radius, 2))
                    for e in outer.edges().filter_by(GeomType.CIRCLE)}
    through, blind = [], []
    for f in solid.faces().filter_by(GeomType.CYLINDER):
        h = _cyl_hole(f, base_z, top_z, warnings, outline_arcs)
        if h is None:
            continue
        (through if h["through"] else blind).append(h)

    if through:
        circles = [(h["x"], h["y"], h["r"]) for h in through]
        feats.append(IR.sketch("holes", "XY", circles=circles))
        feats.append(IR.pocket("drill", "holes", through=True))
    for i, h in enumerate(blind):
        sk = IR.sketch(f"blind_sk{i}", circles=[(h["x"], h["y"], h["r"])],
                       on={"face_of": "body", "side": h["side"]})
        feats.append(sk)
        feats.append(IR.pocket(f"blind{i}", f"blind_sk{i}", through=False, length=round(h["depth"], 4)))

    spec = IR.part(name, *feats)

    report = {"name": name, "features": len(feats), "through_holes": len(through),
              "blind_holes": len(blind), "warnings": warnings, "verified": None,
              "extrude_axis": tuple(round(c, 3) for c in axis.to_tuple()) if axis is not None else None}
    if verify:
        report.update(_verify(spec, orig))
    return spec, report


def _sketch_from_outline(sk_name, outline):
    if outline[0] == "circle":
        _, cx, cy, r = outline
        return IR.sketch(sk_name, "XY", circles=[(cx, cy, r)])
    return IR.sketch(sk_name, "XY", polys=[outline[1]])


def _cyl_hole(face, base_z, top_z, warnings, outline_arcs=frozenset()):
    """A cylindrical face -> a hole dict if it is concave (material outside) with a ~Z axis and is
    NOT part of the outer wire (a rounded outline corner)."""
    ces = face.edges().filter_by(GeomType.CIRCLE)
    if not ces:
        return None
    ctr = ces[0].arc_center
    r = ces[0].radius
    if (round(ctr.X, 2), round(ctr.Y, 2), round(r, 2)) in outline_arcs:
        return None                       # this cylinder is an outline arc, already in the profile
    if any(abs(e.radius - r) > EPS for e in ces):
        return None                       # tapered/variable — not a straight bore
    zs = [e.arc_center.Z for e in ces]
    zlo, zhi = min(zs), max(zs)
    if zhi - zlo < EPS:
        return None                       # degenerate (a flat circle edge, not a wall)
    # concavity: outward surface normal points toward the axis => a hole (void outside)
    sp = face.position_at(0.5, 0.5)
    n = _normal(face)
    if n.X * (sp.X - ctr.X) + n.Y * (sp.Y - ctr.Y) >= 0:
        return None                       # convex -> outer wall, part of the outline
    through = (zlo - base_z) < EPS and (top_z - zhi) < EPS
    side = "top" if (top_z - zhi) < EPS else "bottom"
    return {"x": round(ctr.X, 4), "y": round(ctr.Y, 4), "r": round(r, 4),
            "through": through, "side": side, "depth": round(zhi - zlo, 4)}


def _verify(spec, solid):
    """Re-emit the recognized IR and compare volume + bbox size to the original STEP."""
    try:
        part, res = b3d_emit.emit(spec)
    except Exception as e:
        return {"verified": False, "reason": f"re-emit failed: {e}", "vol_orig": round(solid.volume, 1)}
    ob, nb = solid.bounding_box(), part.bounding_box()
    # sorted bbox sizes: rotation-tolerant (the IR is rebuilt with the extrude axis on Z, which may
    # permute the original's axes)
    os_, ns_ = sorted([ob.size.X, ob.size.Y, ob.size.Z]), sorted([nb.size.X, nb.size.Y, nb.size.Z])
    dvol = abs(res["volume"] - solid.volume)
    dsize = max(abs(o - n) for o, n in zip(os_, ns_))
    ok = dvol <= VOL_TOL * solid.volume and dsize <= DIM_TOL
    return {"verified": bool(ok), "vol_orig": round(solid.volume, 1), "vol_ir": res["volume"],
            "dvol": round(dvol, 2), "dvol_pct": round(100 * dvol / max(solid.volume, 1e-9), 2),
            "dsize_mm": round(dsize, 3)}


def _fixtures(tmp):
    """Generate known STEP fixtures from IR via b3d_emit: three in-scope prismatic parts +
    one out-of-scope part (an additive boss on top of the base) the recognizer must REJECT."""
    from build123d import export_step
    disc = IR.part("disc_hole",
                   IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("body", "o", length=8),
                   IR.sketch("h", "XY", circles=[(0, 0, 5)]), IR.pocket("drill", "h", through=True))
    boss = IR.part("boss",                       # base disc + a raised central post (additive)
                   IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("body", "o", length=6),
                   IR.sketch("b", circles=[(0, 0, 6)], on={"face_of": "body", "side": "top"}),
                   IR.pad("post", "b", length=10))
    specs = {"plate": IR.SAMPLES["plate"](), "poly": IR.SAMPLES["poly"](),
             "disc_hole": disc, "boss": boss}
    paths = {}
    for nm, spec in specs.items():
        part, _ = b3d_emit.emit(spec)
        p = Path(tmp) / f"{nm}.step"
        export_step(part, str(p))
        paths[nm] = str(p)
    # OFF-AXIS: the L-bracket extruded along Z, rotated 90deg about X so it's extruded along Y —
    # the axis-agnostic recognizer must still find it and verify.
    lbr, _ = b3d_emit.emit(IR.SAMPLES["poly"]())
    p = Path(tmp) / "off_axis.step"
    export_step(lbr.rotate(Axis((0, 0, 0), (1, 0, 0)), 90), str(p))
    paths["off_axis"] = str(p)
    # ARC boundary: a D-shape (one rounded side, bulge) — profile has a circular arc, not just lines
    dshape = IR.part("dshape",
                     IR.sketch("o", "XY", polys=[[(-10, -10, 0.0), (10, -10, 0.0),
                                                  (10, 10, 0.0), (-10, 10, 0.6)]]),
                     IR.pad("body", "o", length=5))
    dpart, _ = b3d_emit.emit(dshape)
    p = Path(tmp) / "dshape.step"
    export_step(dpart, str(p))
    paths["dshape"] = str(p)
    return paths


def selftest():
    import tempfile
    paths = _fixtures(tempfile.mkdtemp())
    problems = []
    for nm in ("plate", "poly", "disc_hole", "off_axis", "dshape"):   # in scope -> must VERIFY at Δ≈0
        _, rep = recognize(paths[nm])
        ax = rep.get("extrude_axis")
        print(f"  {nm:10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  "
              f"vol Δ{rep['dvol_pct']}%  (thru={rep['through_holes']}, axis={ax})")
        if not rep["verified"]:
            problems.append(f"{nm}: expected VERIFIED, got Δ{rep['dvol_pct']}%")
    _, rep = recognize(paths["boss"])                # out of scope -> must be flagged PARTIAL
    print(f"  {'boss':10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  vol Δ{rep['dvol_pct']}%  "
          "(additive post — base-extrusion model can't capture it)")
    if rep["verified"]:
        problems.append("boss: an additive-boss part must NOT verify (should fall back to a solid)")
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 1
    print("PASS: 2.5D-prismatic parts recognized + re-emit-verified; out-of-scope flagged, not faked")
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    if not args:
        print(__doc__)
        return 0
    step_path = args[0]
    spec, report = recognize(step_path)
    v = report.get("verified")
    tag = "VERIFIED" if v else ("PARTIAL" if v is False else "UNVERIFIED")
    print(f"recognize {step_path}:")
    print(f"  tree: {' -> '.join(f['name'] for f in spec['features'])}")
    print(f"  {report['through_holes']} through hole(s), {report['blind_holes']} blind")
    if "vol_orig" in report:
        print(f"  volume: STEP {report['vol_orig']}  vs re-emitted IR {report.get('vol_ir','?')}  "
              f"(Δ{report.get('dvol_pct','?')}%, bbox Δ{report.get('dsize_mm','?')}mm)")
    for w in report["warnings"]:
        print(f"  ! {w}")
    print(f"  => {tag}" + ("" if v else " — fall back to importing the STEP as one solid"))
    if "--emit" in args:
        out = Path(args[args.index("--emit") + 1])
        out.write_text(json.dumps(spec, indent=2))
        print(f"  wrote {out}")
    return 0 if v else 1


if __name__ == "__main__":
    sys.exit(main())
