"""Generate the ShelfMind worlds: python3 tools/make_worlds.py

worlds/stockroom.wbt          main demo: one gantry, 6 boxes
worlds/stockroom_duo.wbt      two gantries (A and B) on the same rails
worlds/stockroom_plus.wbt     big box, fragile glass box, parts bin with a lid
worlds/stockroom_physics.wbt  dynamic gantry with a real VacuumGripper (suction physics)
worlds/stockroom_humanoid.wbt the main demo plus a humanoid robot that inspects the shelves

Only base Webots nodes are used (no EXTERNPROTO), so the worlds work offline.
"""

import math
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lookat(pos, target):
    """Axis-angle rotation that points a Webots camera/viewpoint (x forward, z up) at target."""
    dx, dy, dz = (t - p for t, p in zip(target, pos))
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(-dz, math.hypot(dx, dy))
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    r = [[cy * cp, -sy, cy * sp], [sy * cp, cy, sy * sp], [-sp, 0, cp]]
    ang = math.acos(max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1) / 2)))
    if ang < 1e-9:
        return "0 0 1 0"
    ax = [r[2][1] - r[1][2], r[0][2] - r[2][0], r[1][0] - r[0][1]]
    n = math.sqrt(sum(a * a for a in ax))
    return " ".join("%.4f" % v for v in ([a / n for a in ax] + [ang]))


def indent(text):
    out, depth = [], 0
    for line in text.splitlines():
        t = line.strip()
        if not t:
            continue
        code = "".join(t.split('"')[0::2])
        if t.startswith("#"):
            out.append("  " * depth + t if not t.startswith("#VRML") else t)
            continue
        if code.startswith(("}", "]")):
            depth -= 1
        out.append("  " * depth + t)
        depth += code.count("{") + code.count("[") - code.count("}") - code.count("]")
        if code.startswith(("}", "]")):
            depth += 1
    assert depth == 0, depth
    return "\n".join(out) + "\n"


def pbr(color, rough=0.5, metal=0.0, extra=""):
    return "PBRAppearance { baseColor %s roughness %s metalness %s %s }" % (color, rough, metal, extra)


def shape(app, geom, shadows=True):
    return "Shape { appearance %s geometry %s %s }" % (
        app, geom, "" if shadows else "castShadows FALSE")


def pose(t, *children, rot=None):
    r = "rotation %s" % rot if rot else ""
    return "Pose { translation %s %s children [ %s ] }" % (t, r, " ".join(children))


# ---------------------------------------------------------------------------
# Scene pieces
# ---------------------------------------------------------------------------
def header(title, viewpoint):
    pos, target, fov = viewpoint
    return """#VRML_SIM R2025a utf8

WorldInfo {
  info [
    "ShelfMind: a stockroom gantry robot whose brain is NVIDIA Nemotron (via Nebius Token Factory)."
    "Click the 3D view and press T to run task.txt. Double-click the robot to open the control panel."
  ]
  title "%s"
  basicTimeStep 16
  contactProperties [
    ContactProperties {
      coulombFriction [
        0.9
      ]
      bounce 0.1
    }
  ]
}
Viewpoint {
  fieldOfView %s
  orientation %s
  position %s %s %s
}
Background {
  skyColor [
    0.05 0.06 0.08
  ]
  luminosity 0.8
}
DirectionalLight {
  ambientIntensity 0.6
  direction -0.4 0.3 -1
  intensity 2.5
  castShadows TRUE
}
DirectionalLight {
  ambientIntensity 0.3
  direction 0.5 -0.6 -0.7
  intensity 1
}
""" % (title, fov, lookat(pos, target), *pos)


def floor():
    yellow = pbr("1 0.8 0", 0.6)
    return """# Floor with two yellow safety lines
Solid {
  translation 0 0 -0.01
  children [
    Shape { appearance %s geometry DEF FLOOR_BOX Box { size 4 4 0.02 } }
    %s
    %s
  ]
  name "floor"
  boundingObject USE FLOOR_BOX
}
""" % (pbr("0.28 0.29 0.31", 0.9),
       pose("0 0.75 0.011", shape("DEF SAFETY_YELLOW " + yellow, "Box { size 3.6 0.05 0.002 }")),
       pose("0 -0.75 0.011", shape("USE SAFETY_YELLOW", "Box { size 3.6 0.05 0.002 }")))


def table():
    return """# Pickup table (top at z = 0.40)
Solid {
  translation -0.5 0 0.2
  children [
    Shape { appearance %s geometry DEF TABLE_BOX Box { size 0.6 0.8 0.4 } }
  ]
  name "pickup_table"
  boundingObject USE TABLE_BOX
}
""" % pbr("0.5 0.36 0.22", 0.7)


def shelf(name, x, top, colour, board_def, first):
    bz = top - 0.01
    legs = []
    for lx in (-0.13, 0.13):
        for ly in (-0.38, 0.38):
            if first and not legs:
                app = "DEF SHELF_METAL " + pbr("0.7 0.72 0.75", 0.3, 0.3)
            else:
                app = "USE SHELF_METAL"
            legs.append(pose("%s %s %.3f" % (lx, ly, (top - 0.02) / 2),
                             shape(app, "Cylinder { radius 0.015 height %.3f }" % (top - 0.02))))
    return """Solid {
  translation %s 0 0
  children [
    %s
    %s
  ]
  name "%s"
  boundingObject Pose { translation 0 0 %.2f children [ USE %s ] }
}
""" % (x, pose("0 0 %.2f" % bz, shape(pbr(colour, 0.5, 0.2), "DEF %s Box { size 0.3 0.8 0.02 }" % board_def)),
       "\n    ".join(legs), name, bz, board_def)


def frame(post_x):
    parts = []
    first = True
    for px in (-post_x, post_x):
        for py in (-0.65, 0.65):
            app = ("DEF FRAME_METAL " + pbr("0.35 0.37 0.4", 0.4, 0.3)) if first else "USE FRAME_METAL"
            first = False
            parts.append(pose("%s %s 0.65" % (px, py), shape(app, "Box { size 0.06 0.06 1.3 }")))
    for ry in (-0.65, 0.65):
        parts.append(pose("0 %s 1.31" % ry,
                          shape("USE FRAME_METAL", "Box { size %.2f 0.06 0.04 }" % (2 * post_x + 0.06))))
    return "# Gantry frame (visual only)\nSolid {\n  children [\n%s\n  ]\n  name \"gantry_frame\"\n}\n" % (
        "\n".join(parts))


def markers(prefix, colour):
    app = pbr(colour, 1, 0, "emissiveColor %s transparency 0.55" % colour)
    app2 = pbr(colour, 1, 0, "emissiveColor %s transparency 0.35" % colour)
    return """# Markers for robot %s: the box it is handling and the slot it is heading for
DEF %s_BOX_MARKER Pose {
  translation 0 0 -2
  children [ %s ]
}
DEF %s_SLOT_MARKER Pose {
  translation 0 0 -2
  children [ %s ]
}
""" % (prefix, prefix, shape(app, "Box { size 0.13 0.13 0.13 }", False),
       prefix, shape(app2, "Box { size 0.16 0.16 0.006 }", False))


def dock():
    return """# Inbound dock at the east end (robot B's side), top at z = 0.40
DEF INBOUND_DOCK Solid {
  translation 1.1 0 0.2
  children [
    Shape { appearance %s geometry DEF DOCK_BOX Box { size 0.24 0.8 0.4 } }
  ]
  name "inbound_dock"
  boundingObject USE DOCK_BOX
}
""" % pbr("0.35 0.42 0.5", 0.7)


def bin_with_lid():
    wall = pbr("0.25 0.27 0.3", 0.6)
    walls = [("0.075 0 0.17", "0.01 0.16 0.12"), ("-0.075 0 0.17", "0.01 0.16 0.12"),
             ("0 0.075 0.17", "0.16 0.01 0.12"), ("0 -0.075 0.17", "0.16 0.01 0.12"),
             ("0 0 0.055", "0.16 0.16 0.11")]
    vis = [pose(t, shape("USE BIN_GREY" if i else "DEF BIN_GREY " + wall, "Box { size %s }" % s))
           for i, (t, s) in enumerate(walls)]
    bnd = [pose(t, "Box { size %s }" % s) for t, s in walls]
    return """# Parts bin on a stand (inside floor at z = 0.11) with a hinged lid
DEF PARTS_BIN Solid {
  translation 0.75 -0.47 0
  children [
    %s
  ]
  name "parts_bin"
  boundingObject Group { children [ %s ] }
}
DEF BIN_LID Solid {
  translation 0.75 -0.55 0.235
  rotation 1 0 0 0
  children [
    %s
  ]
  name "bin_lid"
}
""" % ("\n    ".join(vis), " ".join(bnd),
       pose("0 0.08 0.005", shape(pbr("0.85 0.4 0.05", 0.5), "Box { size 0.17 0.16 0.01 }")))


def box(name, x, y, z, mass, size, colour, rough, metal, extra=""):
    return """# %s: %s kg
Solid {
  translation %s %s %.3f
  children [
    Shape { appearance %s geometry Box { size %s %s %s } }
  ]
  name "%s"
  boundingObject Box { size %s %s %s }
  physics Physics { density -1 mass %s }
}
""" % (name, mass, x, y, z, pbr(colour, rough, metal, extra), size, size, size, name, size, size,
       size, mass)


STANDARD_BOXES = [
    ("red_box_1", -0.65, 0.25, 2.0, "0.55 0.04 0.04", 0.2, 0.35),
    ("blue_box_1", -0.65, 0.0, 0.3, "0.62 0.76 1", 1.0, 0.0),
    ("green_box_1", -0.65, -0.25, 2.0, "0.03 0.35 0.08", 0.2, 0.35),
    ("red_box_2", -0.40, 0.25, 0.3, "1 0.6 0.6", 1.0, 0.0),
    ("blue_box_2", -0.40, 0.0, 2.0, "0.04 0.1 0.5", 0.2, 0.35),
    ("green_box_2", -0.40, -0.25, 0.3, "0.62 0.95 0.62", 1.0, 0.0),
]


def standard_boxes():
    return "".join(box(n, x, y, 0.451, m, 0.1, c, r, me) for n, x, y, m, c, r, me in STANDARD_BOXES)


def duo_boxes():
    """Heavy boxes on the west table (robot A), light boxes arriving on the east dock (robot B)."""
    return "".join([
        box("red_box_1", -0.65, 0.25, 0.451, 2.0, 0.1, "0.55 0.04 0.04", 0.2, 0.35),
        box("green_box_1", -0.65, -0.25, 0.451, 2.0, 0.1, "0.03 0.35 0.08", 0.2, 0.35),
        box("blue_box_2", -0.40, 0.0, 0.451, 2.0, 0.1, "0.04 0.1 0.5", 0.2, 0.35),
        box("green_box_2", 1.1, 0.25, 0.451, 0.3, 0.1, "0.62 0.95 0.62", 1.0, 0.0),
        box("blue_box_1", 1.1, 0.0, 0.451, 0.3, 0.1, "0.62 0.76 1", 1.0, 0.0),
        box("red_box_2", 1.1, -0.25, 0.451, 0.3, 0.1, "1 0.6 0.6", 1.0, 0.0),
    ])


def plus_boxes():
    return "".join([
        box("red_box_1", -0.65, 0.25, 0.451, 2.0, 0.1, "0.55 0.04 0.04", 0.2, 0.35),
        box("yellow_box_big", -0.65, 0.0, 0.471, 3.0, 0.14, "0.75 0.6 0.02", 0.25, 0.35),
        box("green_box_1", -0.65, -0.25, 0.451, 2.0, 0.1, "0.03 0.35 0.08", 0.2, 0.35),
        box("red_box_2", -0.40, 0.25, 0.451, 0.3, 0.1, "1 0.6 0.6", 1.0, 0.0),
        box("green_box_2", -0.40, 0.0, 0.451, 0.3, 0.1, "0.62 0.95 0.62", 1.0, 0.0),
        box("glass_box_1", -0.40, -0.25, 0.451, 0.2, 0.1, "0.85 0.95 1", 0.05, 0.0, "transparency 0.45"),
        box("parts_box_1", 0.35, 0.0, 0.331, 0.1, 0.06, "1 0.45 0.05", 0.6, 0.0),
    ])


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------
def pad_children(pad_colour):
    return """Shape {
  appearance PBRAppearance { baseColor %s roughness 0.4 metalness 0 emissiveColor 0.1 0.2 0 }
  geometry Cylinder { radius 0.045 height 0.03 }
}
Pose {
  translation 0 0 0.515
  children [
    Shape {
      appearance PBRAppearance { baseColor 0.6 0.62 0.65 roughness 0.3 metalness 0.3 }
      geometry Cylinder { radius 0.012 height 1 }
    }
  ]
}""" % pad_colour


def physics(mass, dynamic):
    if not dynamic:
        return ""
    return "physics Physics { density -1 mass %s centerOfMass [ 0 0 0 ] inertiaMatrix [ %s %s %s 0 0 0 ] }" % (
        mass, mass * 0.1, mass * 0.1, mass * 0.1)


def gantry_chain(prefix, x_range, x_start, beam_colour, pad_colour, devices, dynamic=False,
                 vacuum=False):
    """SliderJoint x -> bridge -> SliderJoint y -> trolley -> SliderJoint z -> pad."""
    n = (prefix.lower() + "_") if prefix else ""

    def slider(axis, name, lo, hi, start, end_point):
        defn = "DEF %s_%s_JOINT " % (prefix, name.upper()) if prefix else ""
        if devices:
            force = "maxForce 400 acceleration 2" if dynamic else ""
            dev = ('device [ LinearMotor { name "%s_motor" maxVelocity 1 minPosition %s maxPosition %s %s } '
                   'PositionSensor { name "%s_sensor" } ]' % (name, lo, hi, force, name))
            params = "axis %s position %s" % (axis, start)
        else:
            dev = ""
            params = "axis %s position %s minStop %s maxStop %s" % (axis, start, lo, hi)
        return "%sSliderJoint {\njointParameters JointParameters { %s }\n%s\nendPoint %s\n}" % (
            defn, params, dev, end_point)

    def solid(kind, translation, children, name, extra=""):
        return "%s {\ntranslation %s\nchildren [\n%s\n]\nname \"%s\"\n%s\n}" % (
            kind, translation, "\n".join(children), name, extra)

    if vacuum:
        pad = solid("VacuumGripper", "0 0 -0.15",
                    ["Pose { rotation 0 1 0 -1.5708 children [ %s ] }" % pad_children(pad_colour)],
                    "suction",
                    "rotation 0 1 0 1.5708\nboundingObject Box { size 0.03 0.09 0.09 }\n%s\n"
                    "tensileStrength 60\nshearStrength 30\ncontactPoints 3" % physics(0.5, dynamic))
    else:
        pad = solid("Solid", "0 0 -0.15", [pad_children(pad_colour)], n + "suction_pad",
                    physics(0.5, dynamic))
    trolley = solid("Solid", "0 0 -0.06", [
        "Shape { appearance %s geometry Box { size 0.14 0.12 0.06 } }" % pbr("0.12 0.12 0.14", 0.5, 0.2),
        slider("0 0 -1", "z", 0, 0.95, 0, pad)], n + "trolley", physics(2, dynamic))
    bridge = solid("Solid", "0 0 1.25", [
        "Shape { appearance %s geometry Box { size 0.08 1.36 0.06 } }" % pbr(beam_colour, 0.4, 0.1),
        slider("0 1 0", "y", -0.5, 0.5, 0, trolley)], n + "bridge", physics(8, dynamic))
    return slider("1 0 0", "x", x_range[0], x_range[1], x_start, bridge)


def main_robot(x_range=(-0.8, 0.9), x_start=0, dynamic=False, vacuum=False, controller_args=None):
    args = ""
    if controller_args:
        args = "controllerArgs [ %s ]" % " ".join('"%s"' % a for a in controller_args)
    return """# Robot A: x/y/z gantry with a suction pad. Pad bottom height = 1.025 - z_motor.
Robot {
  children [
    %s
  ]
  name "gantry_robot"
  controller "stockroom_brain"
  %s
  supervisor TRUE
  window "shelfmind_panel"
}
""" % (gantry_chain("", x_range, x_start, "1 0.75 0", "0.46 0.73 0", True, dynamic, vacuum), args)


def robot_b():
    return """# Robot B: a second gantry on the same rails, moved by robot A's supervisor.
Robot {
  children [
    %s
  ]
  name "gantry_robot_b"
  controller "<none>"
}
""" % gantry_chain("B", (-0.8, 1.2), 1.2, "0.1 0.75 0.85", "0.1 0.75 0.85", False)


def humanoid():
    """A small humanoid inspector robot (about 0.95 m, slim enough for the side lanes and short
    enough to pass under the gantry bridge). It has no controller of its own: robot A's
    supervisor walks it around and reads its head camera."""
    white = "DEF HUMANOID_WHITE " + pbr("0.86 0.88 0.9", 0.35, 0.1)
    dark = "DEF HUMANOID_DARK " + pbr("0.15 0.16 0.18", 0.5, 0.3)
    green = "DEF HUMANOID_GREEN " + pbr("0.46 0.73 0", 0.3, 0, "emissiveColor 0.46 0.73 0")

    def leg(side, y):
        return """DEF INSPECTOR_LEG_%s Pose {
  translation 0 %s 0.48
  children [
    %s
    %s
  ]
}""" % (side, y, pose("0 0 -0.21", shape("USE HUMANOID_WHITE", "Capsule { radius 0.028 height 0.36 }")),
            pose("0.025 0 -0.455", shape("USE HUMANOID_DARK", "Box { size 0.11 0.06 0.03 }")))

    def arm(side, y):
        return """DEF INSPECTOR_ARM_%s Pose {
  translation 0 %s 0.765
  children [
    %s
    %s
    %s
  ]
}""" % (side, y, shape("USE HUMANOID_DARK", "Sphere { radius 0.022 }"),
            pose("0 0 -0.14", shape("USE HUMANOID_WHITE", "Capsule { radius 0.018 height 0.22 }")),
            pose("0 0 -0.275", shape("USE HUMANOID_DARK", "Sphere { radius 0.02 }")))

    return """# Humanoid inspector: walks the side lanes, looks at the shelves, raises an alert when something is wrong
DEF INSPECTOR Robot {
  translation -1.2 -0.515 0
  rotation 0 0 1 0
  children [
    DEF INSPECTOR_BODY Pose {
      translation 0 0 0
      children [
        %s
        %s
        %s
        %s
        %s
        %s
        %s
        %s
        DEF INSPECTOR_HEAD Pose {
          translation 0 0 0.86
          children [
            %s
            %s
            %s
          ]
        }
      ]
    }
  ]
  name "humanoid_inspector"
  controller "<none>"
}
""" % (pose("0 0 0.5", shape(dark, "Box { size 0.09 0.13 0.07 }")),
       pose("0 0 0.66", shape(white, "Box { size 0.11 0.13 0.24 }")),
       pose("0.057 0 0.7", shape(green, "Box { size 0.004 0.07 0.03 }")),
       leg("L", 0.045), leg("R", -0.045),
       arm("L", 0.082), arm("R", -0.082),
       pose("0 0 0.795", shape("USE HUMANOID_DARK", "Cylinder { radius 0.022 height 0.04 }")),
       shape("USE HUMANOID_WHITE", "Capsule { radius 0.062 height 0.03 }"),
       pose("0.045 0 0.01", shape(pbr("0.04 0.05 0.07", 0.1, 0.5), "Box { size 0.04 0.11 0.045 }")),
       pose("0.066 0 0.012", shape("USE HUMANOID_GREEN", "Box { size 0.003 0.09 0.012 }")))


# ---------------------------------------------------------------------------
# Worlds
# ---------------------------------------------------------------------------
VIEW_MAIN = ((-2.8, -0.8, 2.3), (0.1, 0, 0.6), 0.9)
VIEW_DUO = ((-1.0, -3.3, 2.5), (0.25, 0, 0.45), 1.0)
VIEW_INSPECTOR = ((-3.1, -1.5, 2.4), (0.1, 0, 0.35), 0.95)


def scene(view, title, post_x=1.0):
    return (header(title, view) + floor() + table()
            + "# Low shelf (top at z = 0.30), for heavy boxes\n"
            + shelf("low_shelf", 0.35, 0.30, "0.2 0.35 0.6", "LOW_BOARD", True)
            + "# High shelf (top at z = 0.60), for light boxes\n"
            + shelf("high_shelf", 0.75, 0.60, "0.85 0.45 0.1", "HIGH_BOARD", False)
            + frame(post_x))


WORLDS = {
    "stockroom.wbt": lambda: (scene(VIEW_MAIN, "ShelfMind") + markers("A", "1 0.85 0.1")
                              + main_robot() + standard_boxes()),
    "stockroom_duo.wbt": lambda: (scene(VIEW_DUO, "ShelfMind duo", post_x=1.4)
                                  + markers("A", "1 0.85 0.1") + markers("B", "0.1 0.85 1")
                                  + dock() + main_robot(x_range=(-1.15, 0.9), x_start=-1.15)
                                  + robot_b() + duo_boxes()),
    "stockroom_plus.wbt": lambda: (scene(VIEW_MAIN, "ShelfMind plus") + markers("A", "1 0.85 0.1")
                                   + bin_with_lid()
                                   + main_robot(controller_args=["--task", "task_plus.txt"])
                                   + plus_boxes()),
    "stockroom_physics.wbt": lambda: (scene(VIEW_MAIN, "ShelfMind physics") + markers("A", "1 0.85 0.1")
                                      + main_robot(dynamic=True, vacuum=True) + standard_boxes()),
    "stockroom_humanoid.wbt": lambda: (scene(VIEW_INSPECTOR, "ShelfMind humanoid inspector")
                                       + markers("A", "1 0.85 0.1") + main_robot() + humanoid()
                                       + standard_boxes()),
}


if __name__ == "__main__":
    for name, build in WORLDS.items():
        path = os.path.join(ROOT, "worlds", name)
        with open(path, "w") as f:
            f.write(indent(build()))
        print("wrote", os.path.relpath(path, ROOT))
