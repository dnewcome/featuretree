"""Test helpers: turn an IR spec or a build123d part into a STEP file for the recognizer to chew on."""
import b3d_emit
from build123d import export_step


def emit_step(spec, tmp_path, name="p"):
    """IR spec -> build123d solid -> STEP file; returns the path."""
    part, _ = b3d_emit.emit(spec)
    return part_step(part, tmp_path, name)


def part_step(part, tmp_path, name="p"):
    """A build123d part -> STEP file; returns the path."""
    p = tmp_path / f"{name}.step"
    export_step(part, str(p))
    return str(p)
