"""Parametric build123d source templates for the no-LLM shape generator.

Each *_source() function takes already-validated numeric/bool parameters
(never raw request text) and returns a complete, self-contained build123d
Python module defining gen_step() -- the CAD skill's validator requires a
literal `def gen_step():`, not an assignment (see project memory). Because
the values are validated against SCHEMA before formatting, there is no
code-injection surface here: a request can only ever select a shape name
and supply numbers/bools that get embedded as Python literals.
"""

from __future__ import annotations

# One entry per shape. Each param: (label, kind, default, min, max, step).
# kind is "float" or "bool". This is the single source of truth for both
# server-side validation (main.py) and the UI form (auto-generated from it).
SCHEMA: dict[str, dict] = {
    "bar": {
        "label": "Bar (constant width, rounded ends)",
        "params": {
            "length": ("Length (mm)", "float", 30.0, 2.0, 300.0, 0.5),
            "width": ("Width (mm)", "float", 6.0, 1.0, 60.0, 0.5),
            "thickness": ("Thickness (mm)", "float", 3.0, 0.5, 30.0, 0.5),
            "hole_dia": ("Pin hole diameter (mm, 0 = none)", "float", 2.5, 0.0, 20.0, 0.25),
            "hole_start": ("Hole at start", "bool", True, None, None, None),
            "hole_end": ("Hole at end", "bool", True, None, None, None),
        },
    },
    "link": {
        "label": "Tapered link (dog-bone, bossed pivots)",
        "params": {
            "length": ("Length (mm)", "float", 30.0, 4.0, 300.0, 0.5),
            "end_width": ("End width (mm)", "float", 5.0, 1.5, 60.0, 0.5),
            "waist_width": ("Waist width (mm)", "float", 3.0, 0.8, 40.0, 0.5),
            "thickness": ("Thickness (mm)", "float", 2.0, 0.5, 20.0, 0.25),
            "boss_dia": ("Boss diameter (mm, 0 = none)", "float", 3.5, 0.0, 30.0, 0.25),
            "boss_height": ("Boss height (mm)", "float", 0.9, 0.0, 10.0, 0.1),
            "hole_dia": ("Pin hole diameter (mm, 0 = none)", "float", 2.2, 0.0, 20.0, 0.25),
            "hole_start": ("Hole at start", "bool", True, None, None, None),
            "hole_end": ("Hole at end", "bool", True, None, None, None),
        },
    },
    "rod": {
        "label": "Rod (plain cylinder)",
        "params": {
            "length": ("Length (mm)", "float", 25.0, 1.0, 300.0, 0.5),
            "radius": ("Radius (mm)", "float", 1.5, 0.25, 50.0, 0.25),
        },
    },
    "coil": {
        "label": "Coil spring",
        "params": {
            "length": ("Free length (mm)", "float", 20.0, 3.0, 200.0, 0.5),
            "coil_radius": ("Coil radius (mm)", "float", 2.5, 0.5, 40.0, 0.25),
            "wire_radius": ("Wire radius (mm)", "float", 0.4, 0.1, 5.0, 0.05),
            "turns": ("Turns", "float", 6.0, 1.0, 40.0, 1.0),
        },
    },
    "box": {
        "label": "Box",
        "params": {
            "length": ("Length (mm)", "float", 20.0, 1.0, 300.0, 0.5),
            "width": ("Width (mm)", "float", 15.0, 1.0, 300.0, 0.5),
            "height": ("Height (mm)", "float", 8.0, 0.5, 300.0, 0.5),
        },
    },
    "plate": {
        "label": "Mounting plate (rounded rect, corner holes)",
        "params": {
            "length": ("Length (mm)", "float", 40.0, 4.0, 400.0, 0.5),
            "width": ("Width (mm)", "float", 25.0, 4.0, 400.0, 0.5),
            "thickness": ("Thickness (mm)", "float", 3.0, 0.5, 30.0, 0.5),
            "corner_radius": ("Corner radius (mm)", "float", 3.0, 0.0, 40.0, 0.25),
            "hole_dia": ("Corner hole diameter (mm, 0 = none)", "float", 3.5, 0.0, 20.0, 0.25),
            "hole_inset": ("Corner hole inset (mm)", "float", 6.0, 1.0, 60.0, 0.5),
            "holes_at_corners": ("Holes at 4 corners", "bool", True, None, None, None),
        },
    },
    "polygon": {
        "label": "Free-form polygon extrusion",
        "params": {
            "points": (
                "Outline points, one \"x,y\" per line (mm, at least 3)",
                "points",
                "0,0\n30,0\n30,15\n15,25\n0,15",
                None,
                None,
                None,
            ),
            "thickness": ("Extrude thickness (mm)", "float", 4.0, 0.25, 40.0, 0.25),
            "round_edges": ("Round the top/bottom perimeter edge", "bool", True, None, None, None),
            "smooth": (
                "Smooth free-form curve through the points instead of straight edges",
                "bool", False, None, None, None,
            ),
        },
    },
}


def _soften_helper_source() -> str:
    # Inlined verbatim into every generated module that needs it -- each
    # generate_step call is a standalone, self-contained script with no
    # shared imports across calls.
    return '''
def _soften(part, radii=(1.2, 0.9, 0.6, 0.4, 0.25), min_hole_radius=1.5):
    from build123d import Axis, fillet
    try:
        edges = part.edges()
        vertical = edges.filter_by(Axis.Z)
        candidates = edges - vertical
        candidates = [
            e for e in candidates
            if not (e.geom_type == e.geom_type.CIRCLE and e.radius < min_hole_radius)
            and e.geom_type != e.geom_type.ELLIPSE
        ]
        if not candidates:
            return part
    except Exception:
        return part
    for r in radii:
        try:
            return fillet(candidates, radius=r)
        except Exception:
            continue
    return part
'''


def bar_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, BuildSketch, Rectangle, Circle, Locations, extrude, Cylinder, Mode

def gen_step():
    length = {p["length"]}
    width = {p["width"]}
    thickness = {p["thickness"]}
    hole_dia = {p["hole_dia"]}
    with BuildPart() as part:
        with BuildSketch() as sk:
            with Locations((0, 0), (length, 0)):
                Circle(width / 2)
            with Locations((length / 2, 0)):
                Rectangle(length, width)
        extrude(sk.sketch, amount=thickness)
        hole_pts = []
        if {p["hole_start"]}:
            hole_pts.append((0, 0, 0))
        if {p["hole_end"]}:
            hole_pts.append((length, 0, 0))
        if hole_pts and hole_dia > 0:
            with Locations(*hole_pts):
                Cylinder(hole_dia / 2, thickness * 3, mode=Mode.SUBTRACT)
    return part.part
'''


def link_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, BuildSketch, Polygon, Circle, Locations, extrude, Cylinder, Mode
{_soften_helper_source()}

def gen_step():
    length = {p["length"]}
    end_width = {p["end_width"]}
    waist_width = {p["waist_width"]}
    thickness = {p["thickness"]}
    boss_dia = {p["boss_dia"]}
    boss_height = {p["boss_height"]}
    hole_dia = {p["hole_dia"]}
    half_end = end_width / 2
    half_waist = waist_width / 2
    outline = [
        (0, half_end), (length / 2, half_waist), (length, half_end),
        (length, -half_end), (length / 2, -half_waist), (0, -half_end),
    ]
    pivots = []
    if {p["hole_start"]}:
        pivots.append((0, 0))
    if {p["hole_end"]}:
        pivots.append((length, 0))

    with BuildPart() as part:
        with BuildSketch() as sk:
            Polygon(*outline)
            if pivots:
                with Locations(*pivots):
                    Circle(half_end)
        extrude(sk.sketch, amount=thickness)
        if pivots and hole_dia > 0:
            with Locations(*[(px, py, 0) for px, py in pivots]):
                Cylinder(hole_dia / 2, thickness * 3, mode=Mode.SUBTRACT)
    blade = _soften(part.part)

    if pivots and boss_dia > 0 and boss_height > 0:
        with BuildPart() as boss:
            with Locations(*[(px, py, thickness) for px, py in pivots]):
                Cylinder(boss_dia / 2, boss_height)
            if hole_dia > 0:
                with Locations(*[(px, py, thickness) for px, py in pivots]):
                    Cylinder(hole_dia / 2, boss_height * 3, mode=Mode.SUBTRACT)
        blade = blade + boss.part
    return blade
'''


def rod_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, Cylinder

def gen_step():
    with BuildPart() as part:
        Cylinder({p["radius"]}, {p["length"]})
    return part.part
'''


def coil_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, BuildSketch, Circle, sweep, Plane, Helix

def gen_step():
    length = {p["length"]}
    turns = {p["turns"]}
    pitch = length / turns
    with BuildPart() as part:
        helix = Helix(pitch=pitch, height=length, radius={p["coil_radius"]})
        e = helix.edges()[0]
        with BuildSketch(Plane(origin=e.position_at(0), z_dir=e.tangent_at(0))) as sk:
            Circle({p["wire_radius"]})
        sweep(sk.sketch, path=helix)
    return part.part
'''


def box_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, Box

def gen_step():
    with BuildPart() as part:
        Box({p["length"]}, {p["width"]}, {p["height"]})
    return part.part
'''


def plate_source(p: dict) -> str:
    return f'''
from build123d import BuildPart, BuildSketch, RectangleRounded, Circle, Locations, extrude, Cylinder, Mode

def gen_step():
    length = {p["length"]}
    width = {p["width"]}
    thickness = {p["thickness"]}
    hole_dia = {p["hole_dia"]}
    hole_inset = {p["hole_inset"]}
    with BuildPart() as part:
        with BuildSketch() as sk:
            RectangleRounded(length, width, radius={p["corner_radius"]})
        extrude(sk.sketch, amount=thickness)
        if {p["holes_at_corners"]} and hole_dia > 0:
            hx, hy = length / 2 - hole_inset, width / 2 - hole_inset
            with Locations((hx, hy, 0), (-hx, hy, 0), (hx, -hy, 0), (-hx, -hy, 0)):
                Cylinder(hole_dia / 2, thickness * 3, mode=Mode.SUBTRACT)
    return part.part
'''


def polygon_source(p: dict) -> str:
    points_literal = ", ".join(f"({x}, {y})" for x, y in p["points"])
    # Smooth mode swaps the straight-edge Polygon for a Spline run through the
    # same points, closed back to the start with one straight Line -- same
    # closure convention Polygon uses (implicit last->first edge), so switching
    # `smooth` on/off doesn't change how the outline is meant to be closed,
    # only whether the edges between points are curved or straight. A fully
    # periodic (self-closing) spline was considered instead, but a spline can
    # overshoot its control points between segments, and letting that overshoot
    # happen right at the closing edge too (rather than confining it to one
    # known straight segment) makes self-intersecting outlines more likely.
    if p["smooth"]:
        profile = f'''            with BuildLine() as ln:
                Spline(*outline)
                Line(outline[-1], outline[0])
            make_face()'''
    else:
        profile = "            Polygon(*outline)"
    return f'''
from build123d import BuildPart, BuildSketch, BuildLine, Polygon, Spline, Line, make_face, extrude
{_soften_helper_source()}

def gen_step():
    thickness = {p["thickness"]}
    outline = [{points_literal}]
    with BuildPart() as part:
        with BuildSketch() as sk:
{profile}
        extrude(sk.sketch, amount=thickness)
    body = part.part
    if {p["round_edges"]}:
        body = _soften(body)
    return body
'''


BUILDERS = {
    "bar": bar_source,
    "link": link_source,
    "rod": rod_source,
    "coil": coil_source,
    "box": box_source,
    "plate": plate_source,
    "polygon": polygon_source,
}

MAX_POLYGON_POINTS = 60


def _parse_points(raw: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for line_no, line in enumerate(str(raw or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 2:
            raise ValueError(f"Line {line_no}: expected \"x,y\", got {line!r}")
        try:
            x, y = float(parts[0].strip()), float(parts[1].strip())
        except ValueError as exc:
            raise ValueError(f"Line {line_no}: not numeric ({line!r})") from exc
        points.append((x, y))
    if len(points) < 3:
        raise ValueError(f"Need at least 3 points, got {len(points)}")
    if len(points) > MAX_POLYGON_POINTS:
        raise ValueError(f"Too many points ({len(points)}), max {MAX_POLYGON_POINTS}")
    return points


def validate_params(shape: str, raw_params: dict) -> dict:
    """Coerce+clamp raw request params against SCHEMA. Raises ValueError on
    an unknown shape or a param that isn't a real number/bool/point-list --
    the only validation that matters here, since every value ends up as a
    Python literal in generated source, never as interpolated arbitrary
    text."""
    if shape not in SCHEMA:
        raise ValueError(f"Unknown shape '{shape}'. Valid: {', '.join(SCHEMA)}")
    out = {}
    for name, (_, kind, default, lo, hi, _step) in SCHEMA[shape]["params"].items():
        raw = raw_params.get(name, default)
        if kind == "bool":
            out[name] = bool(raw)
        elif kind == "points":
            out[name] = _parse_points(raw)
        else:
            value = float(raw)
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
            out[name] = value
    return out


def build_source(shape: str, raw_params: dict) -> str:
    params = validate_params(shape, raw_params)
    return BUILDERS[shape](params)
