"""Tests for step_recognize: STEP B-rep -> featuretree IR feature recognition.

Every part is built to a known STEP, recognized, and the result checked against what we know is
true. The recognizer self-verifies by re-emit, so `verified` is the headline assertion; we also
pin the method (extrude vs revolve), hole counts, and axis where they matter.
"""
import math

import pytest
from build123d import (Axis, Box, BuildPart, BuildSketch, Circle, Ellipse, Location, Locations,
                       Mode, Plane, SlotOverall, extrude, fillet)

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
