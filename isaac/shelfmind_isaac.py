"""ShelfMind on NVIDIA Isaac Sim with a Franka arm (starter port, NOT TESTED on a Mac).

Same brain as the Webots demo: Nemotron (via Nebius Token Factory) turns the task
into {"action": "pick"/"place"} steps using the same slot names. Here each
pick+place pair is carried out by an arm skill:
  - GR00T (if a GR00T policy server is running, see groot_skill.py), or
  - Isaac Sim's built-in Franka PickPlaceController (default).
Errors (box not where expected after a place) are sent back to Nemotron, which replans.

Run (Linux/Windows with an RTX GPU and Isaac Sim 4.5 or newer):
  ./python.sh isaac/shelfmind_isaac.py            # from your Isaac Sim folder
  ./python.sh isaac/shelfmind_isaac.py --groot    # use a GR00T policy server
"""

import os
import sys

from isaacsim import SimulationApp

simulation_app = SimulationApp({"headless": "--headless" in sys.argv})

import numpy as np  # noqa: E402  (Isaac Sim must start before these imports)
from isaacsim.core.api import World  # noqa: E402
from isaacsim.core.api.objects import DynamicCuboid, FixedCuboid  # noqa: E402
from isaacsim.robot.manipulators.examples.franka import Franka  # noqa: E402
from isaacsim.robot.manipulators.examples.franka.controllers import PickPlaceController  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "controllers", "stockroom_brain"))
from brain import SLOT_INFO, Brain, build_system_prompt  # noqa: E402

TASK = ("Restock the shelves. Heavy boxes go on the low shelf and light boxes go on the high "
        "shelf. On each shelf, order them left to right: red, green, blue.")
CUBE = 0.05  # 5 cm cubes: the Franka gripper opens to 8 cm
SURFACE = {"table": 0.0, "low_shelf": 0.10, "high_shelf": 0.20}

# Scaled-down stockroom inside the Franka's reach. Same slot names as Webots.
SLOTS = {}
for i, y in ((1, 0.25), (2, 0.0), (3, -0.25)):
    SLOTS["table_%d" % i] = np.array([0.30, y, SURFACE["table"]])
    SLOTS["table_%d" % (i + 3)] = np.array([0.42, y, SURFACE["table"]])
    SLOTS["low_shelf_%d" % i] = np.array([0.55, y * 0.8, SURFACE["low_shelf"]])
    SLOTS["high_shelf_%d" % i] = np.array([0.66, y * 0.8, SURFACE["high_shelf"]])

BOXES = [  # name, mass kg, colour, start slot
    ("red_box_1", 2.0, (0.55, 0.04, 0.04), "table_1"),
    ("blue_box_1", 0.3, (0.62, 0.76, 1.0), "table_2"),
    ("green_box_1", 2.0, (0.03, 0.35, 0.08), "table_3"),
    ("red_box_2", 0.3, (1.0, 0.6, 0.6), "table_4"),
    ("blue_box_2", 2.0, (0.04, 0.1, 0.5), "table_5"),
    ("green_box_2", 0.3, (0.62, 0.95, 0.62), "table_6"),
]


def build_world():
    world = World(stage_units_in_meters=1.0)
    world.scene.add_default_ground_plane()
    for name, x, top in (("low_shelf", 0.55, SURFACE["low_shelf"]), ("high_shelf", 0.66, SURFACE["high_shelf"])):
        world.scene.add(FixedCuboid(prim_path="/World/" + name, name=name,
                                    position=np.array([x, 0.0, top / 2]),
                                    scale=np.array([0.09, 0.5, top]), color=np.array([0.3, 0.4, 0.6])))
    boxes = {}
    for name, mass, colour, slot in BOXES:
        boxes[name] = world.scene.add(DynamicCuboid(
            prim_path="/World/" + name, name=name, mass=mass * 0.1,  # scaled for the small arm
            position=SLOTS[slot] + np.array([0, 0, CUBE / 2]),
            scale=np.array([CUBE] * 3), color=np.array(colour)))
    franka = world.scene.add(Franka(prim_path="/World/franka", name="franka"))
    return world, franka, boxes


def location(pos):
    for slot, p in SLOTS.items():
        if abs(pos[0] - p[0]) < 0.04 and abs(pos[1] - p[1]) < 0.04 and abs(pos[2] - (p[2] + CUBE / 2)) < 0.03:
            return slot
    return "floor" if pos[2] < 0.04 else "loose"


def world_state(boxes):
    out, taken = [], set()
    for name, mass, _, _ in BOXES:
        pos, _ = boxes[name].get_world_pose()
        where = location(pos)
        taken.add(where)
        out.append({"name": name, "colour": name.split("_")[0], "weight_kg": mass,
                    "weight_class": "heavy" if mass >= 1 else "light", "location": where})
    return {"robots": [{"name": "A", "holding": None}], "holding": None, "boxes": out,
            "slots": SLOT_INFO, "empty_slots": [s for s in SLOTS if s not in taken]}


class PickPlaceSkill:
    """Isaac Sim's scripted Franka pick-and-place (RMPflow)."""

    def __init__(self, franka):
        self.franka = franka
        self.ctrl = PickPlaceController(name="pp", gripper=franka.gripper, robot_articulation=franka)

    def run(self, world, box, target):
        self.ctrl.reset()
        while simulation_app.is_running():
            world.step(render=True)
            pos, _ = box.get_world_pose()
            actions = self.ctrl.forward(picking_position=pos,
                                        placing_position=target + np.array([0, 0, CUBE / 2]),
                                        current_joint_positions=self.franka.get_joint_positions(),
                                        end_effector_offset=np.array([0, 0.005, 0]))
            self.franka.apply_action(actions)
            if self.ctrl.is_done():
                return


def main():
    world, franka, boxes = build_world()
    world.reset()
    skill = PickPlaceSkill(franka)
    if "--groot" in sys.argv:
        from groot_skill import GrootSkill
        skill = GrootSkill(franka, fallback=skill)
    brain = Brain(os.path.join(HERE, "..", "controllers", "stockroom_brain", "runtime.ini"))
    prompt = build_system_prompt()
    last_error = None
    for attempt in range(1, 5):
        result = brain.plan(TASK, world_state(boxes), last_error, "", prompt)
        plan = result.get("plan") or []
        print("[BRAIN] (%s) %s" % (result["source"], result.get("reasoning")))
        if not plan:
            print("[ROBOT] Nothing left to do.")
            break
        last_error = None
        for pick, place in zip(plan[0::2], plan[1::2]):
            name, slot = pick.get("object"), place.get("slot")
            if name not in boxes or slot not in SLOTS:
                last_error = "Unknown box or slot in step %s -> %s." % (name, slot)
                break
            print("[ROBOT] %s -> %s" % (name, slot))
            skill.run(world, boxes[name], SLOTS[slot])
            for _ in range(30):
                world.step(render=True)
            now = location(boxes[name].get_world_pose()[0])
            if now != slot:
                last_error = "%s did not end up in %s; it is at %s." % (name, slot, now)
                print("[ROBOT] Problem: " + last_error)
                break
        if last_error is None:
            print("[ROBOT] Task done in %d attempt(s)." % attempt)
            break
    while simulation_app.is_running() and "--headless" not in sys.argv:
        world.step(render=True)
    simulation_app.close()


if __name__ == "__main__":
    main()
