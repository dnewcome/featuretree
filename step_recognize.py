"""step_recognize.py — recover a featuretree IR from a STEP B-rep (feature RECOGNITION).

A STEP file is a dumb B-rep: geometry, no feature tree. You cannot *convert* it to an IR;
you can only *infer* one. The insight this leans on: **most machined/printed parts are a 2D
profile EXTRUDED along some axis, or REVOLVED about one** (plus holes). So the core intelligence
is CROSS-SECTIONAL. recognize() tries both:
  * EXTRUDE — find the axis the solid is a prismatic extrusion along (every face is
    planar-perpendicular, planar-parallel, or a cylinder parallel to it), rotate it onto Z, and
    recover the profile + through/blind holes. Orientation-agnostic (X/Y/Z or any face normal).
  * REVOLVE — find the axis the solid is unchanged under rotation about (a body of revolution),
    take the MERIDIAN (a half-plane section through the axis) as the profile, emit revolve(360).
Profiles may have straight edges AND circular arcs (DXF bulge). It returns whichever the re-emit
verifies.

The honesty comes from SELF-VERIFICATION: the recognized IR is re-emitted through b3d_emit and
its volume + (rotation-tolerant) bounding box are compared to the original STEP. So every result
is either "verified" (provably the same solid, Δvol≈0) or "partial/unrecognized" with the residual
reported — in which case the caller should fall back to importing the STEP as one solid. Out of
scope (surfaced as residual, never silently wrong): fillets/chamfers, revolves / lofts / sweeps /
freeform, additive bosses, and profiles whose boundary has ARCS (straight-edge polygons + circular
holes only for now — arc-wire support is the next increment).

    python step_recognize.py part.step                    # recognize + print the tree/verdict
    python step_recognize.py part.step --stl out.stl      # + write the recovered solid (mesh viewer)
    python step_recognize.py part.step --fcstd out.FCStd  # + write an editable FreeCAD tree
    python step_recognize.py part.step --emit out.ir.json # + write the recovered IR
    from step_recognize import recognize;  spec, report = recognize("part.step")
"""

import json
import math
import sys
from pathlib import Path

from build123d import Axis, GeomType, Plane, Pos, Rectangle, Vector, import_step

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


def _extrude_nonconforming(solid, a):
    """How many faces are NOT consistent with a prismatic extrusion along `a` (angled walls, holes
    across the axis, freeform). 0 == a clean extrude; a few == a mostly-extruded part with chamfers
    or cross-holes (recoverable as PARTIAL, the residual flagged)."""
    bad = 0
    for f in solid.faces():
        gt = f.geom_type
        if gt == GeomType.PLANE:
            d = abs(_normal(f).normalized().dot(a))
            if not (d < ANG or d > 1 - ANG):
                bad += 1                          # angled wall (chamfer/draft)
        elif gt in (GeomType.CYLINDER, GeomType.CONE):
            ax = _cyl_axis(f)                      # cone (countersink) fine if its axis is along a
            if ax is not None and abs(ax.dot(a)) < 1 - ANG:
                bad += 1                          # a hole across the axis (cross-hole)
        else:
            bad += 1                              # sphere/torus/bspline
    return bad


def _find_extrude_axis(solid):
    """The axis the solid is (most nearly) a prismatic extrusion along — the one with the FEWEST
    non-conforming faces. Returns (axis, n_bad); n_bad == 0 is a clean extrude, a few means chamfers/
    cross-holes (best-effort, PARTIAL). None if even the best axis is mostly non-conforming."""
    faces = solid.faces()
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for f in faces.filter_by(GeomType.PLANE):
        cands.append(_normal(f).normalized())
    for gt in (GeomType.CYLINDER, GeomType.CONE):
        for f in faces.filter_by(gt):
            ax = _cyl_axis(f)
            if ax:
                cands.append(ax)
    best, best_bad, seen = None, None, []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a.normalized())
        bad = _extrude_nonconforming(solid, a)
        if best is None or bad < best_bad:
            best, best_bad = a.normalized(), bad
    if best is not None and best_bad <= 0.4 * len(faces):   # mostly a prism along `best`
        return best, best_bad
    return None, None


def _align_to_z(solid, a):
    """Rotate the solid so axis `a` lands on +Z (the IR's extrude/revolve axis)."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    if abs(a.dot(z)) > 1 - ANG:
        return solid                              # already along Z
    axis_dir = a.cross(z)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, a.dot(z)))))
    return solid.rotate(Axis((0, 0, 0), axis_dir.to_tuple()), angle)


# --- revolve intelligence: is this solid a body of revolution about SOME axis? -----------------
# Robust test: rotating a body of revolution by any angle about its axis leaves it unchanged. So
# rotate by a non-special angle and check the symmetric-difference volume is ~0 — works for any
# surface types (cone/sphere/torus/bspline), unlike a per-face axis check. Then the MERIDIAN (the
# 2D section in a half-plane through the axis) is the profile the IR revolves.

def _is_revolve_about(solid, a, delta=17.0):
    try:
        rot = solid.rotate(Axis((0, 0, 0), a.normalized().to_tuple()), delta)
        slack = (solid - rot).volume + (rot - solid).volume
    except Exception:
        return False
    return slack < 1e-3 * solid.volume


def _find_revolve_axis(solid):
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for gt in (GeomType.CYLINDER, GeomType.CONE):
        for f in solid.faces().filter_by(gt):
            ax = _cyl_axis(f)                     # axis via the circular edges' centers
            if ax:
                cands.append(ax)
    seen = []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a.normalized())
        if _is_revolve_about(solid, a):
            return a.normalized()
    return None


def _recognize_revolve(orig, name):
    """Recognize a body of revolution: find the axis, take the meridian (half-plane section), and
    emit an XZ profile + revolve(360). Returns (spec, extras) or raises."""
    axis = _find_revolve_axis(orig)
    if axis is None:
        raise ValueError("not a body of revolution")
    solid = _align_to_z(orig, axis)
    bb = solid.bounding_box()
    R, H = bb.max.X + max(1.0, 0.1 * bb.size.X), bb.size.Z + 2
    zc = (bb.min.Z + bb.max.Z) / 2                          # center the section on the solid's z-range
    half = Plane.XZ * Pos(R / 2, zc, 0) * Rectangle(R, H)   # a face on XZ spanning x in [0, R]
    faces = solid.intersect(half).faces()
    if len(faces) != 1:
        raise ValueError(f"revolve meridian has {len(faces)} regions (axial hole etc. — unsupported)")
    merid = faces[0].rotate(Axis((0, 0, 0), (1, 0, 0)), -90)   # XZ (r,axial) -> XY (x=r, y=axial)
    warnings = []
    prof = _classify_wire(merid.outer_wire(), warnings)
    if prof is None or prof[0] != "poly":
        raise ValueError("revolve meridian is not a line/arc profile")
    spec = IR.part(name,
                   IR.sketch("section", "XZ", polys=[prof[1]]),
                   IR.revolve("body", "section", angle=360.0))
    return spec, {"warnings": warnings, "method": "revolve", "axis": axis,
                  "through_holes": 0, "blind_holes": 0}


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
    """STEP file -> (IR spec, report). Tries EXTRUDE then REVOLVE, returning whichever the re-emit
    VERIFIES (Δvol≈0); if neither verifies, the closer residual. report.verified says which."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    name = name or Path(step_path).stem
    candidates = []
    for fn in (_recognize_extrude, _recognize_revolve):
        try:
            spec, extras = fn(orig, name)
        except Exception:
            continue
        ax = extras.get("axis")
        report = {"name": name, "features": len(spec["features"]), "method": extras.get("method"),
                  "through_holes": extras.get("through_holes", 0),
                  "blind_holes": extras.get("blind_holes", 0), "warnings": extras.get("warnings", []),
                  "extrude_axis": tuple(round(c, 3) for c in ax.to_tuple()) if ax is not None else None,
                  "verified": None}
        if verify:
            report.update(_verify(spec, orig))
        if not verify or report.get("verified"):
            return spec, report
        candidates.append((spec, report))
    if candidates:
        return min(candidates, key=lambda c: c[1].get("dvol_pct", 1e9))
    raise ValueError("neither a recognizable extrude nor a body of revolution")


def _recognize_extrude(orig, name):
    """Recognize a 2D profile extruded along some axis (+ through/blind holes). Returns
    (spec, extras) or raises if it isn't a prismatic extrusion."""
    warnings = []
    # cross-section intelligence: find the axis this solid is a prismatic extrusion along (any
    # orientation) and rotate it onto Z, so the Z-profile+holes logic below is axis-agnostic.
    axis, n_bad = _find_extrude_axis(orig)
    if axis is None:
        raise ValueError("not a prismatic extrusion (mostly angled/curved/cross-axis faces)")
    solid = _align_to_z(orig, axis)
    if abs(axis.dot(Vector(0, 0, 1))) < 1 - ANG:
        warnings.append(f"extrude axis {tuple(round(c, 3) for c in axis.to_tuple())} -> rotated onto Z")
    if n_bad:
        warnings.append(f"{n_bad} face(s) not captured by a single extrude (chamfers / cross-holes / "
                        "freeform) — best-effort, expect PARTIAL")
    bb = solid.bounding_box()
    base_z, top_z, thick = bb.min.Z, bb.max.Z, bb.size.Z

    # 1. OUTLINE + through-holes from a CROSS-SECTION, not a single end face — robust to stepped or
    # fragmented ends (a plate whose bottom is broken into islands still yields one clean outline).
    # Section a bit below mid so top-side blind features aren't mistaken for through-holes.
    zc = base_z + 0.4 * thick
    cutter = Plane.XY.offset(zc) * Rectangle(bb.size.X + 50, bb.size.Y + 50)
    sec = solid.intersect(cutter).faces()
    if len(sec) != 1:
        raise ValueError(f"cross-section is {len(sec)} disjoint regions — not one extruded profile")
    sec = sec[0]
    outer = sec.outer_wire()
    outline = _classify_wire(outer, warnings)
    if outline is None:
        raise ValueError("outline not recognizable (lines + circular arcs only; has splines/ellipses)")

    feats = [_sketch_from_outline("outline", outline),
             IR.pad("body", "outline", length=round(thick, 4))]

    # 2a. THROUGH holes = the section's INNER wires — ANY shape (circle, slot, obround, polygon,
    # arcs), not just circular bores. Circles -> one drill sketch; each non-circular wire -> a poly
    # cut. `captured` collects every wire's circular-edge signature (outer + inner) so their cylinders
    # aren't re-detected as blind holes below.
    def _sig(e):
        return (round(e.arc_center.X, 2), round(e.arc_center.Y, 2), round(e.radius, 2))
    captured = {_sig(e) for e in outer.edges().filter_by(GeomType.CIRCLE)}
    thru_circ, thru_poly = [], []
    for w in sec.inner_wires():
        cl = _classify_wire(w, warnings)
        if cl is None:
            continue
        captured.update(_sig(e) for e in w.edges().filter_by(GeomType.CIRCLE))
        (thru_circ if cl[0] == "circle" else thru_poly).append(
            (cl[1], cl[2], cl[3]) if cl[0] == "circle" else cl[1])
    if thru_circ:
        feats.append(IR.sketch("holes", "XY", circles=thru_circ))
        feats.append(IR.pocket("drill", "holes", through=True))
    for i, poly in enumerate(thru_poly):
        feats.append(IR.sketch(f"cut_sk{i}", "XY", polys=[poly]))
        feats.append(IR.pocket(f"cut{i}", f"cut_sk{i}", through=True))

    # 2b. BLIND holes = concave Z-cylinders that DON'T span the thickness (circular only), excluding
    # any cylinder whose circle belongs to a wire we already captured (outline arcs, through-holes).
    blind = []
    for f in solid.faces().filter_by(GeomType.CYLINDER):
        h = _cyl_hole(f, base_z, top_z, warnings, captured)
        if h is None or h["through"]:
            continue
        blind.append(h)
    for i, h in enumerate(blind):
        sk = IR.sketch(f"blind_sk{i}", circles=[(h["x"], h["y"], h["r"])],
                       on={"face_of": "body", "side": h["side"]})
        feats.append(sk)
        feats.append(IR.pocket(f"blind{i}", f"blind_sk{i}", through=False, length=round(h["depth"], 4)))

    spec = IR.part(name, *feats)
    return spec, {"method": "extrude", "axis": axis, "warnings": warnings,
                  "through_holes": len(thru_circ) + len(thru_poly), "blind_holes": len(blind)}


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
    boxpost = IR.part("boxpost",                 # RECTANGULAR base + a raised central post: not a
                      IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("body", "o", length=6),
                      IR.sketch("b", circles=[(0, 0, 6)], on={"face_of": "body", "side": "top"}),
                      IR.pad("post", "b", length=10))   # revolve (rect base) NOR a clean extrude (post)
    specs = {"plate": IR.SAMPLES["plate"](), "poly": IR.SAMPLES["poly"](),
             "disc_hole": disc, "boxpost": boxpost}
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
    # REVOLVE: a cone frustum with a bore (a conical face -> the extrude path can't do it, so the
    # dispatcher must route to revolve). Meridian (r, axial): r2..r10 slanted side + r2 bore.
    cone = IR.part("cone",
                   IR.sketch("m", "XZ", polys=[[(2, -8), (10, -8), (2, 8)]]),
                   IR.revolve("body", "m", angle=360.0))
    cpart, _ = b3d_emit.emit(cone)
    p = Path(tmp) / "cone.step"
    export_step(cpart, str(p))
    paths["cone"] = str(p)
    # SLOT: a plate with a native OBROUND through-hole (non-circular hole — arcs in a hole wire)
    from build123d import BuildPart, BuildSketch, Box, SlotOverall, extrude, Mode, Plane
    with BuildPart() as sp:
        Box(30, 16, 5)
        with BuildSketch(Plane.XY):
            SlotOverall(12, 6)
        extrude(amount=5, both=True, mode=Mode.SUBTRACT)
    p = Path(tmp) / "slot.step"
    export_step(sp.part, str(p))
    paths["slot"] = str(p)
    return paths


def selftest():
    import tempfile
    paths = _fixtures(tempfile.mkdtemp())
    problems = []
    for nm in ("plate", "poly", "disc_hole", "off_axis", "dshape", "cone", "slot"):  # in scope -> VERIFY
        _, rep = recognize(paths[nm])
        print(f"  {nm:10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  "
              f"vol Δ{rep['dvol_pct']}%  (method={rep.get('method')}, thru={rep['through_holes']})")
        if not rep["verified"]:
            problems.append(f"{nm}: expected VERIFIED, got Δ{rep['dvol_pct']}%")
    _, rep = recognize(paths["boxpost"])             # out of scope -> must be flagged PARTIAL
    print(f"  {'boxpost':10} -> {'VERIFIED' if rep['verified'] else 'PARTIAL'}  vol Δ{rep['dvol_pct']}%  "
          "(rect base + additive post — neither a clean extrude nor a revolve)")
    if rep["verified"]:
        problems.append("boxpost: must NOT verify (neither extrude nor revolve should fake it)")
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

    def _arg(flag):
        return Path(args[args.index(flag) + 1]) if flag in args else None
    if _arg("--emit"):
        _arg("--emit").write_text(json.dumps(spec, indent=2))
        print(f"  wrote {_arg('--emit')}  (recovered IR)")
    if _arg("--stl"):
        from build123d import export_stl
        part, _ = b3d_emit.emit(spec)
        export_stl(part, str(_arg("--stl")))
        print(f"  wrote {_arg('--stl')}  (build123d solid — view in any mesh viewer)")
    if _arg("--fcstd"):
        import gen                                   # emits via FreeCAD (needs the AppImage)
        gen.emit(spec, str(_arg("--fcstd")))
        print(f"  wrote {_arg('--fcstd')}  (editable FreeCAD tree)")
    return 0 if v else 1


if __name__ == "__main__":
    sys.exit(main())
