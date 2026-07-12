"""Tests for b3d_emit: featuretree IR -> a build123d solid. Each primitive should produce a single
valid solid of the expected volume (arcs, revolve, polar_pocket, and the XY/XZ planes especially)."""
import math

import pytest

import b3d_emit
import ir as IR


def emit(spec):
    part, res = b3d_emit.emit(spec)
    return part, res["volume"]


def test_pad_rect():
    _, v = emit(IR.part("p", IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("b", "o", 10)))
    assert v == pytest.approx(12000)


def test_pad_circle():
    _, v = emit(IR.part("p", IR.sketch("o", "XY", circles=[(0, 0, 5)]), IR.pad("b", "o", 10)))
    assert v == pytest.approx(math.pi * 25 * 10, rel=1e-3)


def test_pocket_through():
    _, v = emit(IR.part("p", IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("b", "o", 10),
                        IR.sketch("h", "XY", circles=[(0, 0, 4)]), IR.pocket("d", "h", through=True)))
    assert v == pytest.approx(12000 - math.pi * 16 * 10, rel=1e-3)


def test_pocket_blind_depth():
    """A blind pocket removes area*length, not the full thickness."""
    _, v = emit(IR.part("p", IR.sketch("o", "XY", rects=[(40, 30, 0, 0)]), IR.pad("b", "o", 10),
                        IR.sketch("h", circles=[(0, 0, 4)], on={"face_of": "b", "side": "top"}),
                        IR.pocket("d", "h", through=False, length=3)))
    assert v == pytest.approx(12000 - math.pi * 16 * 3, rel=1e-3)


def test_straight_poly_backward_compatible():
    """A poly of plain (x, y) 2-tuples (no bulge) still works."""
    _, v = emit(IR.part("p", IR.sketch("o", "XY", polys=[[(0, 0), (40, 0), (40, 30), (0, 30)]]),
                        IR.pad("b", "o", 5)))
    assert v == pytest.approx(6000)


def test_polygon_with_hole():
    """polys[0] outer, polys[1:] holes -> extrudes outer-minus-holes in one pad."""
    outer = [(0, 0), (40, 0), (40, 30), (0, 30)]
    hole = [(15, 10), (25, 10), (25, 20), (15, 20)]
    _, v = emit(IR.part("p", IR.sketch("o", "XY", polys=[outer, hole]), IR.pad("b", "o", 5)))
    assert v == pytest.approx((40 * 30 - 10 * 10) * 5)


def test_arc_poly_is_single_valid_solid():
    part, v = emit(IR.part("d", IR.sketch("o", "XY",
                                          polys=[[(-10, -10, 0.0), (10, -10, 0.0), (10, 10, 0.0), (-10, 10, 0.5)]]),
                           IR.pad("b", "o", 5)))
    assert len(part.solids()) == 1 and v > 0


def test_revolve_tube_volume():
    """Revolve of an annular profile (r 1..10, height 10) -> a tube."""
    _, v = emit(IR.part("t", IR.sketch("s", "XZ", polys=[[(1, -5), (10, -5), (10, 5), (1, 5)]]),
                        IR.revolve("b", "s", 360.0)))
    assert v == pytest.approx(math.pi * (100 - 1) * 10, rel=1e-3)


def test_revolve_partial_angle_is_half():
    full = IR.part("f", IR.sketch("s", "XZ", polys=[[(1, -5), (10, -5), (10, 5), (1, 5)]]),
                   IR.revolve("b", "s", 360.0))
    half = IR.part("h", IR.sketch("s", "XZ", polys=[[(1, -5), (10, -5), (10, 5), (1, 5)]]),
                   IR.revolve("b", "s", 180.0))
    _, vf = emit(full)
    _, vh = emit(half)
    assert vh == pytest.approx(vf / 2, rel=1e-3)


def test_polar_pocket_cuts_a_ring():
    """A ring of tangent pockets removes material and stays a single solid."""
    base = IR.part("b0", IR.sketch("o", "XY", circles=[(0, 0, 30)]), IR.pad("b", "o", 20))
    _, v0 = emit(base)
    ringed = IR.part("b1", IR.sketch("o", "XY", circles=[(0, 0, 30)]), IR.pad("b", "o", 20),
                     IR.polar_pocket("r", radius=5, length=16, mount_r=25, z=0, count=6, phase=0))
    part, v1 = emit(ringed)
    assert v1 < v0 and len(part.solids()) == 1


def test_fillet_by_query_reduces_volume():
    sharp = IR.part("s", IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("b", "o", 6))
    round_ = IR.part("r", IR.sketch("o", "XY", circles=[(0, 0, 20)]), IR.pad("b", "o", 6),
                     IR.fillet("f", radius=1.0, select={"circles": "top_outer"}))
    _, vs = emit(sharp)
    _, vr = emit(round_)
    assert vr < vs
