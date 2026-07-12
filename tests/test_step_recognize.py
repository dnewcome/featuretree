"""Tests for step_recognize: STEP B-rep -> featuretree IR feature recognition.

Every part is built to a known STEP, recognized, and the result checked against what we know is
true. The recognizer self-verifies by re-emit, so `verified` is the headline assertion; we also
pin the method (extrude vs revolve), hole counts, and axis where they matter.
"""
import math
from pathlib import Path

import pytest
from build123d import (Axis, Box, BuildPart, BuildSketch, Circle, Ellipse, Location, Locations,
                       Mode, Plane, Rectangle, SlotOverall, extrude, fillet)

import ir as IR
import step_recognize as sr
from _helpers import emit_step, part_step


# --------------------------------------------------------------------------- EXTRUDE

def test_rect_plate_with_hole(tmp_path):
    _, r = sr.recognize(emit_step(IR.SAMPLES["plate"](), tmp_path))
    assert r["verified"] and r["method"] == "extrude" and r["through_holes"] == 1


def test_l_bracket_polygon(tmp_path):
    _, r = sr.recognize(emit_step(IR.SAMPLES["poly"](), tmp_path))
    assert r["verified"] and r["method"] == "extrude"


def test_circular_base_with_bore(tmp_path):
    spec = IR.part("disc", IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("b", "o", 8),
                   IR.sketch("h", "XY", circles=[(0, 0, 5)]), IR.pocket("d", "h", through=True))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["through_holes"] == 1


def test_extrude_axis_agnostic(tmp_path):
    """An L-bracket extruded along Z, rotated so it's extruded along Y, is still found."""
    part, _ = __import__("b3d_emit").emit(IR.SAMPLES["poly"]())
    rotated = part.rotate(Axis((0, 0, 0), (1, 0, 0)), 90)
    _, r = sr.recognize(part_step(rotated, tmp_path))
    assert r["verified"] and r["method"] == "extrude"
    assert max(abs(c) for c in r["extrude_axis"]) == pytest.approx(1.0)   # a unit axis
    assert abs(r["extrude_axis"][1]) == pytest.approx(1.0)                # along Y


def test_arc_boundary_dshape(tmp_path):
    """A profile with one rounded (arc) side round-trips; the arc is NOT mistaken for a hole."""
    spec = IR.part("d", IR.sketch("o", "XY",
                                  polys=[[(-10, -10, 0.0), (10, -10, 0.0), (10, 10, 0.0), (-10, 10, 0.5)]]),
                   IR.pad("b", "o", 5))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["through_holes"] == 0 and r["blind_holes"] == 0


def test_obround_slot_through_hole(tmp_path):
    """A non-circular (obround) through-hole is recovered as a poly cut, no spurious blind holes."""
    with BuildPart() as p:
        Box(30, 16, 5)
        with BuildSketch(Plane.XY):
            SlotOverall(12, 6)
        extrude(amount=5, both=True, mode=Mode.SUBTRACT)
    _, r = sr.recognize(part_step(p.part, tmp_path))
    assert r["verified"] and r["through_holes"] == 1 and r["blind_holes"] == 0


def test_blind_hole(tmp_path):
    spec = IR.part("bl", IR.sketch("o", "XY", rects=[(30, 20, 0, 0)]), IR.pad("b", "o", 8),
                   IR.sketch("h", circles=[(0, 0, 4)], on={"face_of": "body", "side": "top"}),
                   IR.pocket("p", "h", through=False, length=4))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["blind_holes"] == 1 and r["through_holes"] == 0


def test_multiple_through_holes(tmp_path):
    spec = IR.part("mh", IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("b", "o", 6),
                   IR.sketch("h", "XY", circles=[(-12, 0, 2), (0, 0, 2), (12, 0, 2)]),
                   IR.pocket("d", "h", through=True))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["through_holes"] == 3


# --------------------------------------------------------------------------- REVOLVE

def test_revolve_cone_frustum(tmp_path):
    spec = IR.part("cone", IR.sketch("m", "XZ", polys=[[(2, -8), (10, -8), (2, 8)]]),
                   IR.revolve("b", "m", 360.0))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["method"] == "revolve"


def test_revolve_stepped_boss(tmp_path):
    """A disc with a coaxial post is a body of revolution (extrude can't do the step + post)."""
    spec = IR.part("boss", IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("b", "o", 6),
                   IR.sketch("t", circles=[(0, 0, 6)], on={"face_of": "b", "side": "top"}),
                   IR.pad("post", "t", 10))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["method"] == "revolve"


def test_revolve_arc_meridian_barrel(tmp_path):
    spec = IR.part("barrel",
                   IR.sketch("m", "XZ", polys=[[(2, -10, 0.0), (8, -10, 0.35), (8, 10, 0.0), (2, 10, 0.0)]]),
                   IR.revolve("b", "m", 360.0))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] and r["method"] == "revolve"


# --------------------------------------------------------------------------- OUT OF SCOPE (honest)

def test_rect_base_plus_post_is_partial(tmp_path):
    """A rectangular base + a raised round post is neither a clean extrude nor a revolve."""
    spec = IR.part("bp", IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("b", "o", 6),
                   IR.sketch("t", circles=[(0, 0, 6)], on={"face_of": "b", "side": "top"}),
                   IR.pad("post", "t", 10))
    _, r = sr.recognize(emit_step(spec, tmp_path))
    assert r["verified"] is False


def test_top_fillet_is_partial(tmp_path):
    """A part with a rounded TOP edge (fillet) isn't a plain extrude — flagged, not faked."""
    with BuildPart() as p:
        Box(30, 20, 8)
        fillet(p.edges().group_by(Axis.Z)[-1], radius=2)   # round the top edges
    _, r = sr.recognize(part_step(p.part, tmp_path))
    assert r["verified"] is False


def test_elliptical_boundary_unsupported(tmp_path):
    """An elliptical extrusion has a non-arc boundary — neither extrude nor revolve; must not fake."""
    with BuildPart() as p:
        with BuildSketch(Plane.XY):
            Ellipse(12, 6)
        extrude(amount=5)
    with pytest.raises(ValueError):
        sr.recognize(part_step(p.part, tmp_path))


# --------------------------------------------------------------------------- meta properties

def test_deterministic(tmp_path):
    path = emit_step(IR.SAMPLES["plate"](), tmp_path)
    a, _ = sr.recognize(path)
    b, _ = sr.recognize(path)
    assert a == b


def test_verify_false_when_disabled_reports_none(tmp_path):
    _, r = sr.recognize(emit_step(IR.SAMPLES["plate"](), tmp_path), verify=False)
    assert r["verified"] is None and r["method"] == "extrude"


def test_nist_ctc01_multiaxis_recovery():
    """A real NIST test part (multi-level pockets + through-web windows + cross-axis holes + chamfers)
    is far beyond a single extrude/revolve. Multi-axis recovery closes it: the base extrude leaves the
    machined interior in the residual, then floor/through pockets and cross-holes are recovered as
    prism_cuts until it re-emit-verifies to within tolerance. The chamfers (sub-tolerance) are left
    honestly flagged as residual, not faked."""
    path = Path(__file__).parent / "step" / "nist_ctc_01_asme1_rd.stp"
    if not path.exists():
        pytest.skip("NIST fixture not present")
    spec, rep = sr.recognize(str(path))
    assert rep["method"] == "extrude" and rep["extrude_axis"] is not None
    assert rep["verified"] is True                        # recovery closes it to within tolerance
    assert rep["dvol_pct"] < 0.5                          # only the chamfers remain (~0.1%)
    rec = rep["recovered"]
    assert rec["pockets"] >= 6 and rec["holes"] >= 6      # multi-level pockets + cross/vertical holes
    assert any("recovery" in w for w in rep["warnings"])  # discloses what was recovered / left


def test_nist_ctc01_without_recovery_is_partial():
    """With recovery disabled, the SAME part is honestly PARTIAL — a best-effort extrude along the
    dominant axis that flags the faces a single extrude can't capture, never faking them verified."""
    path = Path(__file__).parent / "step" / "nist_ctc_01_asme1_rd.stp"
    if not path.exists():
        pytest.skip("NIST fixture not present")
    spec, rep = sr.recognize(str(path), recover=False)
    assert rep["verified"] is False
    assert rep["method"] == "extrude" and rep["extrude_axis"] is not None
    assert any("not captured" in w for w in rep["warnings"])


def test_multilevel_pocket_and_cross_hole(tmp_path):
    """A block with a two-level (stepped) pocket AND a cross-axis hole — neither a single extrude nor
    a revolve — is fully recovered by the multi-axis pass and re-emit-verifies to Δ~0."""
    with BuildPart() as p:
        Box(60, 40, 30)                                   # z in [-15, 15]
        with BuildSketch(Plane.XY):
            with Locations((-24, 0), (24, 0)):
                Circle(3)
        extrude(amount=30, both=True, mode=Mode.SUBTRACT)  # two vertical through-holes
        with BuildSketch(Plane.XY.offset(15)):
            Rectangle(30, 24)
        extrude(amount=-8, mode=Mode.SUBTRACT)             # shallow ledge, floor z=7
        with BuildSketch(Plane.XY.offset(15)):
            Rectangle(18, 12)
        extrude(amount=-20, mode=Mode.SUBTRACT)            # deeper floor z=-5
        with BuildSketch(Plane.XZ):
            with Locations((0, -10)):
                Circle(3)
        extrude(amount=40, both=True, mode=Mode.SUBTRACT)  # cross-hole along Y, below the pocket
    _, r = sr.recognize(part_step(p.part, tmp_path))
    assert r["verified"] is True and r["method"] == "extrude"
    assert r["recovered"]["pockets"] >= 2                  # the two pocket levels
    assert r["recovered"]["holes"] >= 1                    # the cross-axis hole


def test_blind_pocket_not_over_drilled(tmp_path):
    """A blind pocket whose floor sits below the mid cross-section must NOT be mistaken for a through
    hole (which would over-cut, unrecoverably). The two-section test defers it to pocket recovery, and
    the part still verifies with a solid floor under the pocket."""
    with BuildPart() as p:
        Box(40, 40, 20)
        with BuildSketch(Plane.XY.offset(10)):
            Rectangle(20, 20)
        extrude(amount=-12, mode=Mode.SUBTRACT)            # blind pocket, floor at z=-2 (not through)
    _, r = sr.recognize(part_step(p.part, tmp_path))
    assert r["verified"] is True
    assert r["through_holes"] == 0                         # never drilled through
    assert r["recovered"]["pockets"] >= 1


def test_cli_stl_output(tmp_path):
    """`step_recognize.py part.step --stl out.stl` recovers AND writes a viewable solid in one shot."""
    import subprocess
    import sys as _sys
    import trimesh
    step = emit_step(IR.SAMPLES["plate"](), tmp_path)
    out = tmp_path / "recovered.stl"
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run([_sys.executable, str(repo / "step_recognize.py"), step, "--stl", str(out)],
                       capture_output=True, text=True, cwd=str(repo))
    assert out.exists(), r.stdout + r.stderr
    m = trimesh.load(str(out))
    assert m.is_watertight and m.volume == pytest.approx(11497.3, rel=1e-3)


def test_emit_roundtrip_reproduces_volume(tmp_path):
    """recognize -> the recovered IR re-emits to the same volume as the STEP (that IS 'verified')."""
    import b3d_emit
    from build123d import import_step
    path = emit_step(IR.SAMPLES["plate"](), tmp_path)
    orig = import_step(path)
    orig = orig.solid() if hasattr(orig, "solid") else orig
    spec, _ = sr.recognize(path)
    reemit, res = b3d_emit.emit(spec)
    assert res["volume"] == pytest.approx(orig.volume, rel=1e-3)
