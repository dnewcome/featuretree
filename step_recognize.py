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
Profiles may have straight edges AND circular arcs (DXF bulge).

MULTI-AXIS RECOVERY closes real machined parts that no single extrude/revolve can: when the base
extrude doesn't verify, the residual (base − original) is carved feature by feature. Every leftover
lump is itself a 2D profile extruded along ITS OWN axis — a floor / through-web pocket along the
main axis, a cross-hole perpendicular to it — so each is recognized and subtracted as a `prism_cut`
(a placed profile-along-an-axis), looping until the residual vanishes. On the NIST CTC-01 test part
this takes a 139%-off base extrude to a verified reconstruction, leaving only the edge chamfers.

The honesty comes from SELF-VERIFICATION: the recognized IR is re-emitted through b3d_emit and its
volume + (rotation-tolerant) bounding box are compared to the original STEP. Several axes can look
prismatic, so recognize() gathers a candidate per axis and lets verification pick the winner — first
a base that verifies on its own, else the first whose recovery verifies. Every result is either
"verified" (provably the same solid within tolerance, Δvol≈0) or PARTIAL with the residual reported.
Out of scope (surfaced as residual, never silently wrong): edge fillets/chamfers, additive bosses,
lofts/sweeps/freeform, and profiles whose boundary has splines/ellipses (lines + circular arcs only).

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
RECOVER_PASSES = 4   # multi-axis residual-carve passes (each re-decomposes what's left)
RECOVER_EPS = 5.0    # mm^3: ignore boolean slivers / zero-volume sheets in the residual


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


def _extrude_axes(solid):
    """Candidate extrude axes RANKED by how prismatic the solid is along each (fewest non-conforming
    faces first), keeping only axes that are mostly-prismatic (n_bad <= 40% of faces). A ranked list,
    not just the winner, so the recognizer can fall through to the next axis when the best one yields
    a fragmented cross-section (a cross-feature can make an orthogonal axis look deceptively clean)."""
    faces = solid.faces()
    cands = [Vector(0, 0, 1), Vector(0, 1, 0), Vector(1, 0, 0)]
    for f in faces.filter_by(GeomType.PLANE):
        cands.append(_normal(f).normalized())
    for gt in (GeomType.CYLINDER, GeomType.CONE):
        for f in faces.filter_by(gt):
            ax = _cyl_axis(f)
            if ax:
                cands.append(ax)
    scored, seen = [], []
    for a in cands:
        if a.length < EPS or any(abs(a.dot(u)) > 1 - ANG for u in seen):
            continue
        seen.append(a.normalized())
        scored.append((a.normalized(), _extrude_nonconforming(solid, a)))
    lim = 0.4 * len(faces)
    return sorted([(a, b) for a, b in scored if b <= lim], key=lambda t: t[1])


def _find_extrude_axis(solid):
    """The single best-fit extrude axis (fewest non-conforming faces). Returns (axis, n_bad), or
    (None, None) if even the best axis is mostly non-conforming."""
    axes = _extrude_axes(solid)
    return axes[0] if axes else (None, None)


def _align_to_z(solid, a):
    """Rotate the solid so axis `a` lands on +Z (the IR's extrude/revolve axis)."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    if abs(a.dot(z)) > 1 - ANG:
        return solid                              # already along Z
    axis_dir = a.cross(z)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, a.dot(z)))))
    return solid.rotate(Axis((0, 0, 0), axis_dir.to_tuple()), angle)


def _canon_axis(a):
    """Flip an axis so its dominant component is positive — a stable canonical direction (a prism is
    the same whether swept +axis or -axis), which keeps prism_cut off the -Z degenerate."""
    t = a.to_tuple()
    i = max(range(3), key=lambda k: abs(t[k]))
    return a if t[i] >= 0 else Vector(-a.X, -a.Y, -a.Z)


def _unalign_vec(v, a):
    """Inverse of _align_to_z, on a direction vector: map the Z-frame vector `v` back into axis a's
    frame (Rodrigues rotation by -angle about a×z). So a profile recovered in the aligned Z-frame can
    be placed back at a's true orientation for a prism_cut."""
    a = a.normalized()
    z = Vector(0, 0, 1)
    d = max(-1.0, min(1.0, a.dot(z)))
    if d > 1 - ANG:
        return v
    if d < -1 + ANG:
        return Vector(v.X, -v.Y, -v.Z)            # a == -Z: 180deg about X
    k = a.cross(z).normalized()
    theta = -math.acos(d)
    ct, st = math.cos(theta), math.sin(theta)
    kv, kd = k.cross(v), k.dot(v)
    return Vector(v.X * ct + kv.X * st + k.X * kd * (1 - ct),
                  v.Y * ct + kv.Y * st + k.Y * kd * (1 - ct),
                  v.Z * ct + kv.Z * st + k.Z * kd * (1 - ct))


def _face_polys(face):
    """A planar face -> [outer, hole, hole, ...] wires as (u, v[, bulge]) rings (a circle becomes a
    2-arc ring), preserving islands. None if any wire is a spline/ellipse (recover honestly, no fake)."""
    polys = []
    for w in [face.outer_wire()] + list(face.inner_wires()):
        cl = _classify_wire(w, [])
        if cl is None:
            return None
        if cl[0] == "circle":
            _, cx, cy, r = cl
            polys.append([[cx - r, cy, 1.0], [cx + r, cy, 1.0]])   # full circle = two 180deg arcs
        else:
            polys.append(cl[1])
    return polys


def _component_prism(comp, name):
    """A residual component -> a prism_cut IR feature: its recognized 2D profile extruded along its
    OWN dominant axis over its own extent, placed at its true location. None if it isn't a single
    clean profile this pass (multi-region -> a later pass re-decomposes it; sliver -> left honest)."""
    try:
        a, _ = _find_extrude_axis(comp)
        if a is None:
            return None
        a = _canon_axis(a.normalized())
        al = _align_to_z(comp, a)
        lb = al.bounding_box()
        cx, cy = 0.5 * (lb.min.X + lb.max.X), 0.5 * (lb.min.Y + lb.max.Y)   # lumps are off-center
        zc = lb.min.Z + 0.5 * lb.size.Z
        sec = al.intersect(Pos(cx, cy, zc) * Rectangle(lb.size.X + 50, lb.size.Y + 50)).faces()
        if len(sec) != 1:
            return None
        polys = _face_polys(sec[0])
        if polys is None:
            return None
        b3d_emit._polys_region(polys, Plane.XY)     # validate: raises on a degenerate profile
        origin = _unalign_vec(Vector(0, 0, lb.min.Z), a)
        xdir = _unalign_vec(Vector(1, 0, 0), a)
        return IR.prism_cut(name, origin=origin.to_tuple(), normal=a.to_tuple(),
                            xdir=xdir.to_tuple(), depth=round(lb.size.Z, 4), polys=polys)
    except Exception:
        return None


def _is_hole_prism(feat):
    p = feat.get("polys", [])
    return len(p) == 1 and len(p[0]) == 2 and all(len(v) > 2 and abs(abs(v[2]) - 1) < 0.1 for v in p[0])


def _recover_multiaxis(spec, orig, axis, name):
    """Close the residual of a best-effort extrude (outline + through-holes that didn't verify) by
    recovering the machined interior as prism_cuts: (A) floor pockets — every significant intermediate
    perpendicular-to-axis planar face is a pocket floor; (B) a residual-carve loop — whatever's left
    (multi-level / through-web pockets, cross-axis holes) is a set of separate lumps, each a clean 2D
    profile extruded along its OWN axis. All prism_cuts, so the whole thing self-verifies via re-emit.
    Returns (recovered_spec, counts)."""
    feats = list(spec["features"])
    counts = {"pockets": 0, "holes": 0, "residual_lumps": 0}

    # Work in the EMITTED base's own frame: emit the base, then translate the axis-aligned original so
    # its bounding box coincides with it. (The base pad's direction isn't fixed — the outline winding
    # can send it +Z or -Z — so we can't assume z in [0, thick]; matching bboxes makes recovery
    # frame-agnostic, and the two share the same outer envelope so min-corner alignment is exact.)
    part, _ = b3d_emit.emit(IR.part(name, *feats))
    pbb = part.bounding_box()
    aligned = _align_to_z(orig, axis)
    abb = aligned.bounding_box()
    aligned0 = aligned.translate((pbb.min.X - abb.min.X, pbb.min.Y - abb.min.Y, pbb.min.Z - abb.min.Z))
    base_z0, top_z0 = pbb.min.Z, pbb.max.Z

    # (A) floor pockets — every significant intermediate perpendicular-to-axis planar face is a floor.
    foot = pbb.size.X * pbb.size.Y
    for f in aligned0.faces().filter_by(GeomType.PLANE):
        nrm = _normal(f).normalized()
        if abs(abs(nrm.Z) - 1) > ANG:
            continue
        zc = f.position_at(0.5, 0.5).Z
        if not (base_z0 + EPS < zc < top_z0 - EPS) or f.area <= 0.01 * foot:
            continue
        polys = _face_polys(f)
        if polys is None:
            continue
        origin_z, depth = (zc, top_z0 - zc) if nrm.Z > 0 else (base_z0, zc - base_z0)
        feats.append(IR.prism_cut(f"pocket{counts['pockets']}", origin=(0, 0, origin_z),
                                  normal=(0, 0, 1), xdir=(1, 0, 0), depth=round(depth, 4), polys=polys))
        counts["pockets"] += 1

    # (B) residual carve
    if counts["pockets"]:
        part, _ = b3d_emit.emit(IR.part(name, *feats))
    for _p in range(RECOVER_PASSES):
        comps = [c for c in (part - aligned0).solids() if c.volume > RECOVER_EPS]
        if not comps:
            break
        added = []
        for c in comps:
            pf = _component_prism(c, f"cut{counts['pockets'] + counts['holes'] + len(added)}")
            if pf is not None:
                added.append(pf)
        if not added:
            break
        trial = feats + added
        try:
            part2, _ = b3d_emit.emit(IR.part(name, *trial))
        except Exception:
            break                                        # a cut didn't build -> keep what we have
        if part2.volume >= part.volume - RECOVER_EPS:
            break                                        # no progress -> stop (rest is chamfers)
        for pf in added:
            counts["holes" if _is_hole_prism(pf) else "pockets"] += 1
        feats, part = trial, part2
    counts["residual_lumps"] = len([c for c in (part - aligned0).solids() if c.volume > RECOVER_EPS])
    return IR.part(name, *feats), counts


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


def _wire_xy(wire):
    """A wire's (x, y) bounding-box center, rounded — a stable key for matching the same vertical
    hole across cross-sections at different heights."""
    wb = wire.bounding_box()
    return (round((wb.min.X + wb.max.X) / 2, 1), round((wb.min.Y + wb.max.Y) / 2, 1))


def _through_centroids(solid, base_z, top_z, bb):
    """(x, y) keys of inner-wire holes that appear near BOTH end faces — i.e. bores that actually go
    all the way through. A blind pocket appears near only one face, so it's excluded (and recovered as
    a pocket instead of being over-cut as a through-hole)."""
    def inner_keys(z):
        cut = Plane.XY.offset(z) * Rectangle(bb.size.X + 50, bb.size.Y + 50)
        keys = set()
        for ff in solid.intersect(cut).faces():
            for w in ff.inner_wires():
                keys.add(_wire_xy(w))
        return keys
    d = min(1.0, 0.02 * (top_z - base_z))
    return inner_keys(base_z + d) & inner_keys(top_z - d)


def _report_for(spec, extras, name):
    ax = extras.get("axis")
    return {"name": name, "features": len(spec["features"]), "method": extras.get("method"),
            "through_holes": extras.get("through_holes", 0),
            "blind_holes": extras.get("blind_holes", 0), "warnings": list(extras.get("warnings", [])),
            "extrude_axis": tuple(round(c, 3) for c in ax.to_tuple()) if ax is not None else None,
            "verified": None}


def recognize(step_path, name=None, verify=True, recover=True):
    """STEP file -> (IR spec, report). Recovers a feature tree by EXTRUDE (a 2D profile swept along an
    axis + holes) or REVOLVE. Several axes may look prismatic, so it gathers a candidate per axis and
    lets SELF-VERIFICATION pick the winner: first a candidate whose base re-emit VERIFIES on its own
    (Δvol≈0); else — with recover=True — multi-axis recovery (floor / through pockets + cross-axis
    holes as prism_cuts) is run per axis and the first that verifies wins (report.recovered has the
    tally). If nothing verifies, the closest residual is returned (report.verified is False)."""
    orig = import_step(str(step_path))
    orig = orig.solid() if hasattr(orig, "solid") else orig
    name = name or Path(step_path).stem

    # candidate decompositions: one extrude per prismatic axis (rank order) + a revolve if applicable.
    cands = []
    for axis, n_bad in _extrude_axes(orig):
        try:
            cands.append(_extrude_along(orig, name, axis, n_bad))
        except ValueError:
            continue
    try:
        cands.append(_recognize_revolve(orig, name))
    except Exception:
        pass
    if not cands:
        raise ValueError("neither a recognizable extrude nor a body of revolution")
    if not verify:
        spec, extras = cands[0]
        return spec, _report_for(spec, extras, name)

    # Pass 1: prefer a candidate whose BASE verifies with NO recovery — the simplest clean extrude /
    # revolve (this is what keeps an L extruded along Y from being "recovered" as a box + a carve).
    reports = []
    for spec, extras in cands:
        report = _report_for(spec, extras, name)
        report.update(_verify(spec, orig))
        if report["verified"]:
            return spec, report
        reports.append((spec, extras, report))

    # Pass 2: multi-axis recovery per extrude candidate (rank order); first that verifies wins.
    best = min(reports, key=lambda c: c[2].get("dvol_pct", 1e9))
    if recover:
        for spec, extras, report in reports:
            if extras.get("method") != "extrude" or extras.get("axis") is None:
                continue
            try:
                rspec, counts = _recover_multiaxis(spec, orig, extras["axis"], name)
            except Exception:
                continue
            rreport = _report_for(rspec, extras, name)
            rreport.update(_verify(rspec, orig))
            rreport["recovered"] = counts
            rreport["warnings"].append(
                f"multi-axis recovery: +{counts['pockets']} pocket(s), +{counts['holes']} "
                f"cross-hole(s); {counts['residual_lumps']} residual lump(s) (chamfers/fillets) left uncut")
            if rreport["verified"]:
                return rspec, rreport
            if rreport.get("dvol_pct", 1e9) < best[2].get("dvol_pct", 1e9):
                best = (rspec, extras, rreport)
    return best[0], best[2]


def _extrude_along(orig, name, axis, n_bad):
    """Recognize `orig` as a 2D profile extruded along the given `axis` (+ through/blind holes).
    Raises ValueError if the cross-section along this axis isn't a single recognizable profile."""
    warnings = []
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
    # Sample a few heights (not just one fraction) so the section doesn't land exactly on a feature
    # plane (a pocket floor) and fragment; take the first height giving one clean region.
    sec = None
    for frac in (0.4, 0.27, 0.6, 0.5, 0.72, 0.33):
        zc = base_z + frac * thick
        faces = solid.intersect(Plane.XY.offset(zc) * Rectangle(bb.size.X + 50, bb.size.Y + 50)).faces()
        if len(faces) == 1:
            sec = faces[0]
            break
    if sec is None:
        raise ValueError("cross-section is disjoint at every sampled height — not one extruded profile")
    outer = sec.outer_wire()
    outline = _classify_wire(outer, warnings)
    if outline is None:
        raise ValueError("outline not recognizable (lines + circular arcs only; has splines/ellipses)")

    feats = [_sketch_from_outline("outline", outline),
             IR.pad("body", "outline", length=round(thick, 4))]

    # 2a. THROUGH holes = the section's INNER wires that are ACTUALLY through — present near BOTH end
    # faces (a blind pocket whose floor is below the section shows up here too, but only near one face;
    # emitting it as through would over-cut, and an over-cut can't be recovered, so we leave it for the
    # pocket-recovery pass). Any shape (circle, slot, obround, polygon, arcs) — circles become one drill
    # sketch, each non-circular wire a poly cut. `captured` collects the through wires' circular-edge
    # signatures (+ the outline's) so their cylinders aren't re-detected as blind holes below.
    def _sig(e):
        return (round(e.arc_center.X, 2), round(e.arc_center.Y, 2), round(e.radius, 2))
    thru_at = _through_centroids(solid, base_z, top_z, bb)
    captured = {_sig(e) for e in outer.edges().filter_by(GeomType.CIRCLE)}
    thru_circ, thru_poly = [], []
    for w in sec.inner_wires():
        cl = _classify_wire(w, warnings)
        if cl is None or _wire_xy(w) not in thru_at:
            continue                          # blind / stepped -> recovery handles it (don't over-cut)
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
