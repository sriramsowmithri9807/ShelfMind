"""ShelfMind supervisor controller.

Reads a job (task.txt, the control panel, or a stock order), asks the brain
(NVIDIA Nemotron via Nebius Token Factory) for a JSON plan, runs it on one or
two gantry robots, and replans when something goes wrong. After a finished
task it keeps watching the shelves.

Keys (click the 3D view first):
  T run task   D drop box   K knock box   S self-recovery on/off
  M mess up the boxes   R reset   O next stock order   N switch model
  V vision check   P plan from the camera on/off   B benchmark
Double-click the robot to open the control panel (task box, voice, live stats).
"""

import base64
import json
import os
import random
import sys
import textwrap
import threading
import time
from typing import Any, cast
import urllib.parse
from datetime import datetime

from controller import Keyboard, Supervisor, VacuumGripper

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import planning  # noqa: E402
import vision  # noqa: E402
from brain import SLOT_INFO, SLOTS, Brain, build_system_prompt  # noqa: E402
from gantry import PAD0, Gantry, JointGantry, MotorGantry  # noqa: E402
from inspector import ZONES, Inspector  # noqa: E402
from memory import Memory  # noqa: E402

PROJECT = os.path.abspath(os.path.join(HERE, "..", ".."))
LOG_DIR = os.path.join(PROJECT, "logs")
INI_FILE = os.path.join(HERE, "runtime.ini")
MEMORY_FILE = os.path.join(PROJECT, "memory", "memory.json")
ORDERS_DIR = os.path.join(PROJECT, "orders")
BENCH_FILE = os.path.join(PROJECT, "benchmark", "tasks.json")
SHIFT_FILE = os.path.join(PROJECT, "scenarios", "full_shift.json")

SAFE_Z = 0.05          # z_motor value for travelling
TOL = 0.003            # sensor tolerance
HEAVY_KG = 1.0
MAX_ATTEMPTS = 4
SETTLE_S = 1.5
FALL_S = 1.5
CLAIM_W = 0.11         # half width of rail a gantry needs (duo world)
FRAGILE_SPEED = 0.5    # speed factor while carrying a fragile box
BREAK_FALL = 0.12      # a fragile box breaks if it falls further than this
BIN_XY = (0.75, -0.47)

NVIDIA_GREEN = 0x76B900
YELLOW = 0xFFD700
LIGHT_BLUE = 0x8FD3FF
MAGENTA = 0xFF77FF
ORANGE = 0xFFA640
WHITE = 0xFFFFFF
GREY = 0xCCCCCC
RED = 0xFF5555
FONT = "Arial"


class SimEnd(Exception):
    """The simulation was stopped or reset."""


def wrap(text, width, max_lines):
    lines = textwrap.wrap(text, width) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:width - 3] + "..."
    return "\n".join(lines)


def parse_args(argv):
    args = {"task": "task.txt", "benchmark": False, "shift": False, "quit_after": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--task" and i + 1 < len(argv):
            args["task"] = argv[i + 1]
            i += 1
        elif a == "--benchmark":
            args["benchmark"] = True
        elif a == "--shift":
            args["shift"] = True
        elif a == "--quit-after":
            args["quit_after"] = True
        i += 1
    return args


def describe(step):
    a = step.get("action", "?")
    if a == "pick":
        text = "pick %s" % step.get("object")
    elif a == "place":
        text = "place on %s" % step.get("slot")
    elif a == "stack":
        text = "stack on %s" % step.get("on")
    else:
        text = a.replace("_", " ")
    return text


class ShelfMind(Supervisor):
    def __init__(self):
        super().__init__()
        self.ts = int(self.getBasicTimeStep())
        self.args = parse_args(sys.argv[1:])
        self.task_file = os.path.join(PROJECT, self.args["task"])

        devices = {self.getDeviceByIndex(i).getName() for i in range(self.getNumberOfDevices())}
        self.duo = self.getFromDef("B_X_JOINT") is not None
        a = MotorGantry(self, "A", self.ts, home_x=-1.15 if self.duo else 0.0)
        self.gantries: list[Gantry] = [a]
        if self.duo:
            self.gantries.append(JointGantry(
                self, "B", "B", {"x": (-0.8, 1.2), "y": (-0.5, 0.5), "z": (0.0, 0.95)}, home_x=1.2))
        if "suction" in devices:
            a.vacuum = cast(VacuumGripper, self.getDevice("suction"))
            a.vacuum.enablePresence(self.ts)
        self.grip = "vacuum" if a.vacuum else "lock"
        self.viewpoint = next((n for n in self.root_nodes() if n.getTypeName() == "Viewpoint"), None)
        self.camera = self.viewpoint is not None  # vision snapshots use the 3D view
        self.keyboard = Keyboard()
        self.keyboard.enable(self.ts)

        self.boxes = self.find_boxes()
        self.slots = dict(SLOTS)
        self.slot_info = dict(SLOT_INFO)
        self.lid = self.getFromDef("BIN_LID")
        self.lid_open = False
        extras = ["stack"]
        if self.getFromDef("INBOUND_DOCK") is not None:
            for i, y in ((1, 0.25), (2, 0.0), (3, -0.25)):
                self.slots["dock_%d" % i] = (1.1, y, 0.40)
                self.slot_info["dock_%d" % i] = "inbound dock at the east end (robot B's side), %s" % (
                    ("left", "middle", "right")[i - 1])
            extras.append("dock")
        if self.getFromDef("PARTS_BIN") is not None:
            self.slots["bin"] = (BIN_XY[0], BIN_XY[1], 0.11)
            self.slot_info["bin"] = "inside the parts bin (open the lid first)"
            extras.append("bin")
        self.system_prompt = build_system_prompt([g.name for g in self.gantries], extras)
        self.markers = {}
        for g in self.gantries:
            box_node = self.getFromDef("%s_BOX_MARKER" % g.name)
            size = None
            if box_node:
                shape = box_node.getField("children").getMFNode(0)  # pyrefly: ignore[missing-attribute]
                geom = shape.getField("geometry").getSFNode()  # pyrefly: ignore[missing-attribute]
                size = geom.getField("size")
            self.markers[g.name] = {
                "box_node": box_node, "box_size": size,
                "slot_node": self.getFromDef("%s_SLOT_MARKER" % g.name),
                "box": None, "slot": None}

        self.brain = Brain(INI_FILE)
        self.memory = Memory(MEMORY_FILE)
        self.orders = planning.load_orders(ORDERS_DIR)
        self.order_idx = 0

        self.busy = False
        self.recovery = True
        self.perception = "simulator"
        self.pending_task = None     # (trigger, reason, task_text)
        self.watch = None            # {box: location} after a finished task
        self.next_watch_check = 0.0
        self.disturbance = []        # (box, from) knocked while a task runs
        self.question = None
        self.keys_down = set()
        self.task_id = 0
        self.task_text = self.read_task()
        self.status_text = ""
        self.reasoning = ""
        self.plan_view = []
        self.events = []
        self.bench = None
        self.shift = None          # progress of the full-shift scenario (key G)
        self.auto_drop = False     # shift step: make the next carried box slip
        self.last_result = None    # (trigger, outcome) of the last finished task
        self.stats = {"tasks": 0, "done": 0, "gave_up": 0, "attempts": 0, "recoveries": 0,
                      "nemo_calls": 0, "latency": 0.0, "tokens": 0}
        self.labels = {}
        self.label_pos: dict[int, tuple[float, float, float]] = {}
        self.last_push = ""
        self.next_push = 0.0
        self.drop_height = {}
        self.initial = {n: self.pos(n) for n in self.boxes}
        self.inspector = Inspector(self) if self.getFromDef("INSPECTOR") is not None else None
        self.change_time = {}      # box -> sim time it was knocked (for detection delay)
        self.inspector_reported = set()
        self.thuds = []            # (box, time) crashes the inspector will hear

        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_path = os.path.join(LOG_DIR, datetime.now().strftime("run_%Y%m%d_%H%M%S.jsonl"))
        self.log_file = open(self.log_path, "a", encoding="utf-8")

    # ------------------------------------------------------------------ setup
    def root_nodes(self):
        """All top-level nodes of the world."""
        field = self.getRoot().getField("children")
        # Webots adds getMFNode/getSFNode to Field at run time, so the type checker cannot see them.
        return [field.getMFNode(i) for i in range(field.getCount())]  # pyrefly: ignore[missing-attribute]

    def find_boxes(self):
        boxes = {}
        for node in self.root_nodes():
            if node.getTypeName() != "Solid":
                continue
            name = node.getField("name").getSFString()
            if "_box_" not in name:
                continue
            physics = node.getField("physics").getSFNode()
            mass = physics.getField("mass").getSFFloat() if physics else 0.0
            size = 0.1
            bo = node.getField("boundingObject").getSFNode()
            if bo and bo.getTypeName() == "Box":
                size = bo.getField("size").getSFVec3f()[2]
            colour_field = None
            shape = node.getField("children").getMFNode(0)
            if shape and shape.getTypeName() == "Shape":
                app = shape.getField("appearance").getSFNode()
                if app:
                    colour_field = app.getField("baseColor")
            boxes[name] = {
                "name": name,
                "node": node,
                "translation": node.getField("translation"),
                "rotation": node.getField("rotation"),
                "colour_field": colour_field,
                "colour": name.split("_")[0],
                "mass": round(mass, 2),
                "class": "heavy" if mass >= HEAVY_KG else "light",
                "half": size / 2.0,
                "size": size,
                "fragile": name.startswith("glass"),
                "broken": False,
            }
        return boxes

    def read_task(self):
        try:
            with open(self.task_file, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        except OSError:
            lines = []
        return " ".join(lines) or "Restock the shelves."

    def write_task(self, text):
        try:
            with open(self.task_file, "w", encoding="utf-8") as f:
                f.write("# ShelfMind job (written from the control panel). Lines starting with # "
                        "are ignored.\n%s\n" % text.strip())
        except OSError as e:
            print("[ROBOT] Could not save the task: %s" % e)

    # ---------------------------------------------------------------- logging
    def log(self, event, **data):
        rec = {"event": event, "time": datetime.now().isoformat(timespec="seconds"),
               "sim_time": round(self.getTime(), 2), "task_id": self.task_id,
               "recovery": self.recovery, "model": self.brain.model if self.brain.uses_nemotron
               else "backup", "world": os.path.basename(self.getWorldPath())}
        rec.update(data)
        self.log_file.write(json.dumps(rec) + "\n")
        self.log_file.flush()

    def event(self, text):
        self.events.append("%6.1fs  %s" % (self.getTime(), text))
        self.events = self.events[-14:]

    # ----------------------------------------------------------------- labels
    def label(self, idx, text, x, y, size, color):
        text = text.replace('"', "'")  # Webots does not escape quotes in recorded web animations
        self.label_pos[idx] = (x, y, size)
        if self.labels.get(idx) == (text, color):
            return
        self.labels[idx] = (text, color)
        self.setLabel(idx, text, x, y, size, color, 0.0, FONT)

    def draw_static(self):
        model = (" (%s)" % self.brain.model.split("/")[-1]) if self.brain.uses_nemotron else ""
        robots = "  |  robots A + B" if self.duo else ""
        self.label(0, "ShelfMind  |  brain: %s%s%s" % (self.brain.name, model, robots),
                   0.01, 0.01, 0.10, NVIDIA_GREEN)
        self.label(1, "Task: " + wrap(self.task_text, 95, 2), 0.01, 0.065, 0.07, WHITE)
        watcher = "humanoid inspector" if self.inspector and self.inspector.on else "watch mode"
        mode = "Self-recovery: %s" % ("ON (up to %d attempts + %s)" % (MAX_ATTEMPTS, watcher)
                                      if self.recovery else "OFF (1 attempt, no watch mode)")
        mode += "   |   grip: %s   |   plan from: %s   |   memory: %d lessons" % (
            "vacuum (physics)" if self.grip == "vacuum" else "lock",
            self.perception, len(self.memory.lessons))
        self.label(4, mode, 0.01, 0.895, 0.06, NVIDIA_GREEN if self.recovery else RED)
        self.label(5, "Keys: T run  D drop  K knock  S recovery  M mess up  R reset  O order  "
                      "N model  V vision  P camera plan  B benchmark  G full shift" + ("  H inspector" if self.inspector else ""),
                   0.01, 0.945, 0.06, GREY)

    def status(self, text, prefix="[ROBOT]", color=YELLOW):
        print("%s %s" % (prefix, text))
        self.status_text = text
        self.label(2, wrap(text, 105, 1), 0.01, 0.135, 0.075, color)

    def show_reasoning(self, source, text):
        who = "Nemotron" if source == "nemotron" else "Backup planner"
        self.reasoning = "%s: %s" % (who, text)
        self.label(3, wrap(self.reasoning, 120, 3), 0.01, 0.18, 0.06, LIGHT_BLUE)

    def draw_plan(self):
        marks = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "failed": "[!]"}
        lines = []
        for i, p in enumerate(self.plan_view[:14]):
            who = ("%s " % p["robot"]) if self.duo else ""
            lines.append("%s %d. %s%s" % (marks[p["state"]], i + 1, who, p["text"]))
        if len(self.plan_view) > 14:
            lines.append("    ... %d more" % (len(self.plan_view) - 14))
        text = ("PLAN\n" + "\n".join(lines)) if lines else ""
        self.label(6, text, 0.74, 0.30, 0.06, WHITE)

    # ------------------------------------------------------------ the panel
    def push_state(self, force=False):
        if not force and self.getTime() < self.next_push:
            return
        self.next_push = self.getTime() + 0.25
        locs = self.locations()
        state = {
            "brain": self.brain.name, "uses_nemotron": self.brain.uses_nemotron,
            "model": self.brain.model, "models": self.brain.models,
            "vision_model": self.brain.cfg["vision_model"],
            "task": self.task_text, "task_file": self.read_task(),
            "status": self.status_text, "reasoning": self.reasoning,
            "plan": self.plan_view, "question": self.question and self.question["q"],
            "busy": self.busy, "recovery": self.recovery, "perception": self.perception,
            "grip": self.grip, "duo": self.duo, "camera": self.camera,
            "bin": "bin" in self.slots, "lid": "open" if self.lid_open else "closed",
            "robots": [{"name": g.name, "holding": g.held, "x": round(g.pos("x"), 2),
                        "waiting": g.waiting} for g in self.gantries],
            "boxes": [{"name": n, "location": locs[n], "class": b["class"], "colour": b["colour"],
                       "broken": b["broken"]} for n, b in self.boxes.items()],
            "stats": self.stats, "events": self.events,
            "orders": [{"id": o.get("order_id"), "type": o.get("type", ""),
                        "lines": o.get("lines", [])} for o in self.orders],
            "lessons": self.memory.lessons, "bench": self.bench, "shift": self.shift,
            "inspector": self.inspector and {"on": self.inspector.on, "status": self.inspector.describe(),
                                             "reports": self.inspector.reports,
                                             "last": self.inspector.last},
            "sim_time": round(self.getTime(), 1),
        }
        text = json.dumps(state)
        if text != self.last_push or force:
            self.last_push = text
            self.wwiSendText("state:" + urllib.parse.quote(text, safe=""))

    def poll_panel(self):
        msg = self.wwiReceiveText()
        while msg is not None:
            self.handle_panel(msg)
            msg = self.wwiReceiveText()

    def handle_panel(self, msg):
        if not msg.startswith("cmd:"):
            self.push_state(force=True)  # "init robot window" or anything else: send everything
            return
        _, name, arg = (msg.split(":", 2) + [""])[:3]
        arg = urllib.parse.unquote(arg)
        print("[ROBOT] Panel: %s %s" % (name, arg[:80]))
        if name == "run" and arg.strip():
            if self.busy:
                print("[ROBOT] Busy with a task, try again when it is done.")
            else:
                self.write_task(arg)
                self.question = None
                self.pending_task = ("panel", None, arg.strip())
        elif name == "answer" and self.question and not self.busy:
            q = self.question
            self.question = None
            self.pending_task = ("answer", None, "%s\nClarification. Question: %s Answer: %s"
                                 % (q["task"], q["q"], arg.strip()))
        else:
            self.command(name, arg)
        self.push_state(force=True)

    def command(self, name, arg=""):
        """Shared by keys and panel buttons."""
        if name == "run" and not self.busy:
            self.pending_task = ("user", None, None)
        elif name == "run":
            print("[ROBOT] Busy with a task, T ignored.")
        elif name == "drop":
            self.drop()
        elif name == "knock":
            self.knock()
        elif name == "recovery":
            self.recovery = not self.recovery
            if not self.recovery:
                self.watch = None
            print("[ROBOT] Self-recovery is now %s." % ("ON" if self.recovery else "OFF"))
            self.log("recovery_toggled", enabled=self.recovery)
        elif name in ("scatter", "reset") and self.busy:
            print("[ROBOT] Busy with a task; %s ignored." % name)
        elif name == "scatter":
            seed = int(arg) if arg.isdigit() else random.randrange(10000)
            self.apply_layout(planning.scatter_layout(list(self.boxes), list(self.slots), seed))
            self.event("boxes scattered (seed %d)" % seed)
            self.log("scatter", seed=seed)
            print("[SIM] Boxes scattered (seed %d). Press T to tidy up." % seed)
        elif name == "reset":
            self.apply_layout({n: ("pos",) + tuple(p) for n, p in self.initial.items()})
            self.event("boxes reset")
            self.log("reset")
            print("[SIM] Boxes back in their starting places.")
        elif name == "order" and not self.busy:
            if not self.orders:
                print("[ROBOT] No orders in orders/.")
            else:
                if arg.isdigit():
                    self.order_idx = int(arg) % len(self.orders)
                self.pending_task = ("order", self.order_idx, None)
                self.order_idx = (self.order_idx + 1) % len(self.orders)
        elif name == "model":
            model = self.brain.set_model(arg) if arg else self.brain.next_model()
            print("[BRAIN] Model is now %s" % model)
            self.event("model: %s" % model.split("/")[-1])
            self.log("model_switched", to=model)
        elif name == "vision" and not self.busy:
            self.pending_task = ("vision", None, None)
        elif name == "perception":
            self.perception = "camera" if self.perception == "simulator" else "simulator"
            print("[ROBOT] Plans now use the %s view of the world." % self.perception)
            self.log("perception_toggled", perception=self.perception)
        elif name == "shift" and not self.busy and not (self.shift and self.shift.get("running")):
            self.pending_task = ("shift", None, None)
        elif name == "benchmark" and not self.busy:
            self.pending_task = ("benchmark", None, None)
        elif name == "inspector" and self.inspector:
            self.inspector.on = not self.inspector.on
            if not self.inspector.on and self.inspector.request:
                self.inspector.request = None
                self.inspector.next()
            print("[INSPECTOR] The humanoid inspector is %s." % ("patrolling" if self.inspector.on else "standing by"))
            self.event("inspector %s" % ("on patrol" if self.inspector.on else "standing by"))
            self.log("inspector_toggled", on=self.inspector.on)
        elif name == "forget":
            self.memory.clear()
            print("[ROBOT] Memory cleared.")
            self.event("memory cleared")
        self.draw_static()

    # ----------------------------------------------------------- stepping
    def tick(self):
        if self.step(self.ts) == -1:
            raise SimEnd()
        dt = self.ts / 1000.0
        for g in self.gantries:
            g.update(dt)
        if self.inspector:
            for name, t in [th for th in self.thuds if th[1] <= self.getTime()]:
                self.thuds.remove((name, t))
                self.inspector.hear(self.pos(name)[0])
            self.inspector.update(dt, can_inspect=not self.busy)
            self.label(7, "Humanoid inspector: %s   |   reports: %d   |   %s" % (
                self.inspector.describe(), self.inspector.reports, self.inspector.last), 0.01, 0.845, 0.06, ORANGE)
        self.apply_grips()
        if self.auto_drop:  # scripted grip failure: drop the box mid-carry between table and shelf
            g = next((g for g in self.gantries if g.held and g.pos("z") < 0.1 and -0.15 < g.pos("x") < 0.15), None)
            if g:
                self.auto_drop = False
                self.drop()
        self.update_markers()
        self.poll_keys()
        self.poll_panel()
        self.push_state()

    def wait(self, seconds):
        end = self.getTime() + seconds
        while self.getTime() < end:
            self.tick()

    def poll_keys(self):
        pressed = set()
        key = self.keyboard.getKey()
        while key != -1:
            pressed.add(key & 0xFFFF)
            key = self.keyboard.getKey()
        new = pressed - self.keys_down
        self.keys_down = pressed
        keymap = {"T": "run", "D": "drop", "K": "knock", "S": "recovery", "M": "scatter",
                  "R": "reset", "O": "order", "N": "model", "V": "vision", "P": "perception",
                  "B": "benchmark", "H": "inspector", "G": "shift"}
        for key in new:
            ch = chr(key) if 32 <= key < 127 else ""
            if ch in keymap:
                self.command(keymap[ch])

    # ------------------------------------------------------------- the world
    def pos(self, name):
        return self.boxes[name]["node"].getPosition()

    def holder(self, name):
        for g in self.gantries:
            if g.held == name:
                return g
        return None

    def location(self, name):
        g = self.holder(name)
        if g:
            return "gripper" if not self.duo else "gripper %s" % g.name
        b = self.boxes[name]
        x, y, z = self.pos(name)
        for slot, (sx, sy, surface) in self.slots.items():
            if abs(x - sx) < 0.09 and abs(y - sy) < 0.09 and abs(z - (surface + b["half"])) < 0.04:
                return slot
        for other, o in self.boxes.items():
            if other == name or self.holder(other):
                continue
            ox, oy, oz = self.pos(other)
            if abs(x - ox) < 0.06 and abs(y - oy) < 0.06 and abs(z - (oz + o["half"] + b["half"])) < 0.03:
                return "on:" + other
        return "floor" if z - b["half"] < 0.07 else "loose"

    def locations(self):
        return {name: self.location(name) for name in self.boxes}

    def occupant(self, slot):
        for name in self.boxes:
            if not self.holder(name) and self.location(name) == slot:
                return name
        return None

    def on_top_of(self, name):
        for other in self.boxes:
            if other != name and not self.holder(other) and self.location(other) == "on:" + name:
                return other
        return None

    def world_state(self, locs=None):
        locs = locs or self.locations()
        boxes = []
        for name, b in self.boxes.items():
            item = {"name": name, "colour": b["colour"], "weight_kg": b["mass"],
                    "weight_class": b["class"], "location": locs[name]}
            if self.perception == "simulator":
                item["position"] = [round(v, 3) for v in self.pos(name)]
            if abs(b["size"] - 0.1) > 0.005:
                item["size_cm"] = round(b["size"] * 100)
            if b["fragile"]:
                item["fragile"] = True
            if b["broken"]:
                item["broken"] = True
            boxes.append(item)
        taken = set(locs.values())
        state = {
            "robots": [{"name": g.name, "holding": g.held} for g in self.gantries],
            "holding": self.gantries[0].held,
            "boxes": boxes,
            "slots": self.slot_info,
            "empty_slots": [s for s in self.slots if s not in taken],
            "perception": self.perception,
        }
        if "bin" in self.slots:
            state["bin"] = {"lid": "open" if self.lid_open else "closed"}
        return state

    def apply_layout(self, layout):
        """Teleport boxes. Values: slot name, ("floor", x, y), ("pos", x, y, z), "on:<box>"."""
        for g in self.gantries:
            if g.held:
                if g.vacuum:
                    g.vacuum.turnOff()
                g.held = None
                g.set_speed(1.0)
        later = []
        for name, where in layout.items():
            b = self.boxes[name]
            if isinstance(where, tuple) and where[0] == "floor":
                p = [where[1], where[2], b["half"] + 0.002]
            elif isinstance(where, tuple) and where[0] == "pos":
                p = list(where[1:])
            elif str(where).startswith("on:"):
                later.append((name, where[3:]))
                continue
            else:
                sx, sy, surface = self.slots[where]
                p = [sx, sy, surface + b["half"] + 0.002]
            b["translation"].setSFVec3f(p)
            b["rotation"].setSFRotation([0, 0, 1, 0])
            b["node"].resetPhysics()
        self.step(self.ts)
        for name, base in later:
            b, o = self.boxes[name], self.boxes[base]
            x, y, z = self.pos(base)
            b["translation"].setSFVec3f([x, y, z + o["half"] + b["half"] + 0.002])
            b["rotation"].setSFRotation([0, 0, 1, 0])
            b["node"].resetPhysics()
        self.watch = None

    # --------------------------------------------------------------- gripper
    def other(self, g):
        return self.gantries[1] if g is self.gantries[0] else self.gantries[0]

    def apply_grips(self):
        for g in self.gantries:
            if not g.held:
                continue
            b = self.boxes[g.held]
            x, y, pad = g.pad()
            if g.vacuum:
                bx, by, bz = self.pos(g.held)
                if abs(bx - x) > 0.04 or abs(by - y) > 0.04 or abs(bz - (pad - b["half"])) > 0.04:
                    name = g.held
                    g.vacuum.turnOff()
                    g.held = None
                    g.set_speed(1.0)
                    g.drop_event = name
                    self.drop_height[name] = bz
                    print("[SIM] The suction link broke! %s slipped off robot %s." % (name, g.name))
                continue
            b["translation"].setSFVec3f([x, y, pad - b["half"]])
            b["rotation"].setSFRotation([0, 0, 1, 0])
            b["node"].resetPhysics()

    def update_markers(self):
        for g in self.gantries:
            m = self.markers[g.name]
            if m["box_node"]:
                if m["box"] and m["box"] in self.boxes:
                    x, y, z = self.pos(m["box"])
                    m["box_node"].getField("translation").setSFVec3f([x, y, z])
                    m["box_size"].setSFVec3f([self.boxes[m["box"]]["size"] + 0.03] * 3)
                else:
                    m["box_node"].getField("translation").setSFVec3f([0, 0, -2])
            if m["slot_node"]:
                p = m["slot"] or (0, 0, -2)
                m["slot_node"].getField("translation").setSFVec3f([p[0], p[1], p[2] + 0.004])

    def drop(self):
        g = next((g for g in self.gantries if g.held), None)
        if not g:
            print("[SIM] D pressed, but no gripper is holding a box.")
            return
        name, g.held = g.held, None
        if g.vacuum:
            g.vacuum.turnOff()
        g.set_speed(1.0)
        self.boxes[name]["node"].resetPhysics()
        self.drop_height[name] = self.pos(name)[2]
        g.drop_event = name
        print("[SIM] Grip failure! %s slipped from robot %s's suction pad." % (name, g.name))

    def knock(self):
        targets = [n for n in self.boxes
                   if not self.holder(n) and self.location(n).startswith("low_shelf")]
        if not targets:
            print("[SIM] K pressed, but the low shelf is empty.")
            return
        name = random.choice(targets)
        where = self.location(name)
        node = self.boxes[name]["node"]
        node.setVelocity([-1.2, random.choice((0.15, -0.15)), 0.6, 0, 0, 0])
        node.addForce([0, 0, 0.001], False)  # wakes the box if the physics engine put it to sleep
        print("[SIM] Knock! %s was pushed off %s." % (name, where))
        self.event("knock: %s off %s" % (name, where))
        self.log("knock", box=name, from_location=where)
        self.memory.failure("knocked_off", "%s was knocked off %s" % (name, where), box=name, slot=where)
        self.change_time[name] = self.getTime()
        if self.inspector:  # the inspector hears the thud when the box lands
            self.thuds.append((name, self.getTime() + 0.6))
        if self.busy:
            self.disturbance.append((name, where))

    def break_box(self, name):
        b = self.boxes[name]
        b["broken"] = True
        if b["colour_field"]:
            b["colour_field"].setSFColor([0.25, 0.25, 0.25])
        print("[SIM] %s is fragile and broke!" % name)
        self.log("broken", box=name)

    # ------------------------------------------------------ motion (generators)
    def g_wait(self, seconds):
        end = self.getTime() + seconds
        while self.getTime() < end:
            yield

    def g_move(self, g, targets):
        """Drive axes and wait. Returns None, "dropped" or an error."""
        for a, v in targets.items():
            g.command(a, v)
        end = self.getTime() + 25.0
        while True:
            yield
            if g.drop_event:
                for a in targets:
                    g.stop(a)
                return "dropped"
            if all(abs(g.pos(a) - v) < TOL for a, v in targets.items()):
                return None
            if self.getTime() > end:
                return "the gripper did not reach its target in time"

    def stationary_claim(self, g):
        x = g.pos("x")
        g.claim = (x - CLAIM_W, x + CLAIM_W)

    def g_claim(self, g, x_target):
        """Duo world: reserve the stretch of rail between here and x_target.
        Robot A always stays west of robot B. If both wait, B gives way."""
        if not self.duo:
            return None
        other = self.other(g)
        while True:
            lo = min(g.pos("x"), x_target) - CLAIM_W
            hi = max(g.pos("x"), x_target) + CLAIM_W
            if other.claim is None:
                self.stationary_claim(other)
            olo, ohi = other.claim or (other.pos("x") - CLAIM_W, other.pos("x") + CLAIM_W)
            if (hi < olo) if g.name == "A" else (lo > ohi):
                g.claim = (lo, hi)
                g.waiting = False
                return None
            if not g.waiting:
                print("[ROBOT] Robot %s waits for robot %s." % (g.name, other.name))
            g.waiting = True
            if g.name == "B" and other.waiting:
                g.waiting = False
                print("[ROBOT] Robot B gives way to robot A.")
                r = yield from self.g_park(g)
                if r:
                    return r
                continue
            yield

    def g_park(self, g):
        """Move to the robot's home end of the rail (safe height)."""
        if g.pos("z") > SAFE_Z + TOL:
            r = yield from self.g_move(g, {"z": SAFE_Z})
            if r:
                return r
        x = g.pos("x")
        g.claim = (min(x, g.home_x) - CLAIM_W, max(x, g.home_x) + CLAIM_W)
        r = yield from self.g_move(g, {"x": g.home_x})
        self.stationary_claim(g)
        return r

    def g_travel(self, g, x, y, z):
        """Lift to the safe height, travel in x/y, then lower."""
        if abs(g.pos("x") - x) > TOL or abs(g.pos("y") - y) > TOL:
            if g.pos("z") > SAFE_Z + TOL:
                r = yield from self.g_move(g, {"z": SAFE_Z})
                if r:
                    return r
            r = yield from self.g_claim(g, x)
            if r:
                return r
            r = yield from self.g_move(g, {"x": x, "y": y})
            if self.duo:
                self.stationary_claim(g)
            if r:
                return r
        return (yield from self.g_move(g, {"z": z}))

    def g_after_drop(self, g):
        name, g.drop_event = g.drop_event, None
        self.markers[g.name]["slot"] = None
        self.status("Grip failure: %s slipped! Watching where it lands..." % name, "[SIM]")
        start_z = self.drop_height.pop(name, self.pos(name)[2])
        yield from self.g_wait(FALL_S)
        where = self.location(name)
        x, y, z = self.pos(name)
        b = self.boxes[name]
        broke = b["fragile"] and not b["broken"] and start_z - z > BREAK_FALL
        if broke:
            self.break_box(name)
        self.log("drop", box=name, robot=g.name, landed=where, broke=broke,
                 position=[round(x, 3), round(y, 3)])
        self.event("drop: %s landed at %s" % (name, where))
        print("[SIM] %s landed at %s (x=%.2f, y=%.2f)." % (name, where, x, y))
        self.memory.failure("grip_failure", "%s slipped while carried" % name, box=name)
        msg = ("Grip failure: %s slipped out of robot %s's suction gripper while it was being "
               "carried and fell. It landed at %s (x=%.2f, y=%.2f). The gripper is now empty."
               % (name, g.name, where, x, y))
        if broke:
            msg += " %s is fragile and BROKE; it is now marked broken, leave it alone." % name
        return msg

    # --------------------------------------------------------------- actions
    def g_pick(self, g, name):
        if name not in self.boxes:
            return "Unknown box '%s'. Known boxes: %s." % (name, ", ".join(self.boxes))
        b = self.boxes[name]
        if b["broken"]:
            return "%s is broken; leave it alone." % name
        if g.held:
            return "Robot %s is already holding %s. Place it first." % (g.name, g.held)
        h = self.holder(name)
        if h:
            return "%s is being held by robot %s." % (name, h.name)
        top = self.on_top_of(name)
        if top:
            return "%s has %s on top of it. Move %s first." % (name, top, top)
        where = self.location(name)
        if where == "bin" and not self.lid_open:
            return "The parts bin lid is closed. Open it first."
        x, y, z = self.pos(name)
        zt = PAD0 - (z + b["half"])
        if not g.reachable(x, y, zt):
            return "%s is out of reach at (x=%.2f, y=%.2f, z=%.2f)." % (name, x, y, z)
        self.markers[g.name]["box"] = name
        self.status("Robot %s: picking %s from %s" % (g.name, name, where) if self.duo
                    else "Picking %s from %s" % (name, where))
        if g.vacuum:
            g.vacuum.turnOn()
        r = yield from self.g_travel(g, x, y, zt + (0.004 if g.vacuum else 0.0))
        if r:
            if g.vacuum:
                g.vacuum.turnOff()
            return r
        nx, ny, nz = self.pos(name)
        if abs(nx - x) > 0.03 or abs(ny - y) > 0.03 or abs(nz - z) > 0.03:
            if g.vacuum:
                g.vacuum.turnOff()
            return "%s moved before the gripper could reach it." % name
        if g.vacuum:
            for _ in range(20):
                if g.vacuum.getPresence():
                    break
                yield
            if not g.vacuum.getPresence():
                g.vacuum.turnOff()
                return "The suction cup could not grip %s." % name
        g.held = name
        if b["fragile"]:
            g.set_speed(FRAGILE_SPEED)
        self.apply_grips()
        r = yield from self.g_move(g, {"z": SAFE_Z})
        if r == "dropped":
            return (yield from self.g_after_drop(g))
        return r

    def g_put(self, g, where, surface_xy, surface_z, expect):
        """Shared by place and stack."""
        name = g.held
        b = self.boxes[name]
        sx, sy = surface_xy
        zt = PAD0 - (surface_z + 2 * b["half"] + 0.002)
        if not g.reachable(sx, sy, zt):
            return "%s is out of reach." % where
        self.markers[g.name]["slot"] = (sx, sy, surface_z)
        self.status("Robot %s: putting %s on %s" % (g.name, name, where) if self.duo
                    else "Placing %s on %s" % (name, where))
        r = yield from self.g_travel(g, sx, sy, zt)
        if r == "dropped":
            return (yield from self.g_after_drop(g))
        if r:
            return r
        g.held = None
        g.set_speed(1.0)
        if g.vacuum:
            g.vacuum.turnOff()
        else:  # release exactly on the surface
            b["translation"].setSFVec3f([sx, sy, surface_z + b["half"] + 0.002])
            b["rotation"].setSFRotation([0, 0, 1, 0])
            b["node"].resetPhysics()
        r = yield from self.g_move(g, {"z": SAFE_Z})
        self.markers[g.name]["slot"] = None
        self.markers[g.name]["box"] = None
        if r:
            return r
        yield from self.g_wait(0.3)
        now = self.location(name)
        if now != expect:
            self.memory.failure("did_not_settle", "%s did not settle on %s" % (name, where),
                                box=name, slot=where)
            return "%s did not settle on %s; it is now at %s." % (name, where, now)
        return None

    def g_place(self, g, slot):
        if slot not in self.slots:
            return "Unknown slot '%s'. Known slots: %s." % (slot, ", ".join(self.slots))
        if not g.held:
            return "Robot %s's gripper is empty. Pick up a box before placing it." % g.name
        other = self.occupant(slot)
        if other:
            return "Slot %s is taken by %s. Clear it first or use an empty slot." % (slot, other)
        if slot == "bin" and not self.lid_open:
            return "The parts bin lid is closed. Open it first."
        sx, sy, surface = self.slots[slot]
        return (yield from self.g_put(g, slot, (sx, sy), surface, slot))

    def g_stack(self, g, base):
        if base not in self.boxes:
            return "Unknown box '%s'." % base
        if not g.held:
            return "Robot %s's gripper is empty. Pick up a box before stacking it." % g.name
        if base == g.held or self.holder(base):
            return "Cannot stack on %s: it is being held." % base
        where = self.location(base)
        if where not in self.slots:
            return "Can only stack on a box that sits in a slot; %s is at %s." % (base, where)
        top = self.on_top_of(base)
        if top:
            return "%s already has %s on top of it." % (base, top)
        x, y, z = self.pos(base)
        return (yield from self.g_put(g, "top of " + base, (x, y),
                                      z + self.boxes[base]["half"], "on:" + base))

    def g_lid(self, g, opening):
        if not self.lid:
            return "There is no parts bin with a lid in this world."
        if self.lid_open == opening:
            return None
        self.status("Robot %s: %s the parts bin lid" % (g.name, "opening" if opening else "closing"))
        r = yield from self.g_travel(g, BIN_XY[0], BIN_XY[1] - 0.02, SAFE_Z)
        if r:
            return r
        start, end = (0.0, 1.9) if opening else (1.9, 0.0)
        t0 = self.getTime()
        while True:
            k = min(1.0, (self.getTime() - t0) / 0.6)
            self.lid.getField("rotation").setSFRotation([1, 0, 0, start + (end - start) * k])
            if k >= 1.0:
                break
            yield
        self.lid_open = opening
        self.log("lid", open=opening)
        return None

    def g_step(self, s):
        step, g = s["step"], self.gantry(s["robot"])
        action = str(step.get("action", "")).lower()
        if action == "pick":
            r = yield from self.g_pick(g, step.get("object") or step.get("box"))
        elif action == "place":
            r = yield from self.g_place(g, step.get("slot"))
        elif action == "stack":
            r = yield from self.g_stack(g, step.get("on"))
        elif action in ("open_lid", "close_lid"):
            r = yield from self.g_lid(g, action == "open_lid")
        else:
            r = "Unknown action '%s'. Use only the actions listed." % action
        if r:
            self.markers[g.name]["box"] = None
            self.markers[g.name]["slot"] = None
        return r

    def gantry(self, name):
        return next(g for g in self.gantries if g.name == name)

    def execute(self, plan):
        """Run a plan. Steps whose robot, box and slot are free run in parallel."""
        names = [g.name for g in self.gantries]
        steps, err = planning.analyse_plan(plan, self.locations(),
                                           {g.name: g.held for g in self.gantries}, names)
        if err:
            return err
        self.plan_view = [{"text": describe(s["step"]), "robot": s["robot"], "state": "todo"}
                          for s in steps]
        self.draw_plan()
        done, running, service = set(), {}, {}
        first_error = None
        while True:
            if first_error is None:
                for s in steps:
                    i = s["index"]
                    if (self.plan_view[i]["state"] != "todo" or s["robot"] in running
                            or s["robot"] in service or not all(d in done for d in s["deps"])):
                        continue
                    running[s["robot"]] = (i, self.g_step(s))
                    self.plan_view[i]["state"] = "doing"
                    self.draw_plan()
            if self.duo:
                for g in self.gantries:
                    o = self.other(g)
                    if g.name not in running and g.name not in service and o.waiting:
                        service[g.name] = self.g_park(g)
            if not running and not service:
                break
            self.tick()
            for name, (i, gen) in list(running.items()):
                try:
                    next(gen)
                except StopIteration as e:
                    del running[name]
                    if e.value:
                        self.plan_view[i]["state"] = "failed"
                        if first_error is None:
                            first_error = "Step %d %s failed: %s" % (
                                i + 1, json.dumps(steps[i]["step"]), e.value)
                    else:
                        done.add(i)
                        self.plan_view[i]["state"] = "done"
                    self.draw_plan()
            for name, gen in list(service.items()):
                try:
                    next(gen)
                except StopIteration:
                    del service[name]
        for g in self.gantries:
            g.waiting = False
        if first_error:
            return first_error
        held = ["robot %s holds %s" % (g.name, g.held) for g in self.gantries if g.held]
        if held:
            return ("The plan ended while %s. Every picked box must be placed." % ", ".join(held))
        return None

    # ------------------------------------------------------------- the brain
    def call_in_thread(self, fn, label):
        result = {}

        def work():
            try:
                result["r"] = fn()
            except Exception as e:  # report, don't crash the robot
                result["e"] = e

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        t0, shown = time.time(), -1
        while worker.is_alive():
            secs = int(time.time() - t0)
            if secs != shown:
                shown = secs
                self.label(2, "%s... %ds" % (label, secs), 0.01, 0.135, 0.075, YELLOW)
                self.status_text = "%s... %ds" % (label, secs)
            self.tick()
            time.sleep(0.002)  # give the HTTP thread time to run
        if "e" in result:
            raise result["e"]
        return result["r"]

    def camera_jpg(self, pos=None, target=None, fov=None, name="camera_latest.jpg"):
        """Snapshot from a camera pose (default: the overview camera): move the 3D view
        there for a moment, hide the labels, save the image, then put everything back."""
        pos = pos or vision.CAMERA_POS
        target = target or vision.CAMERA_TARGET
        vp = self.viewpoint
        assert vp is not None, "the world has no Viewpoint"
        fields = {f: vp.getField(f) for f in ("position", "orientation", "fieldOfView")}
        saved = (fields["position"].getSFVec3f(), fields["orientation"].getSFRotation(),
                 fields["fieldOfView"].getSFFloat())
        fields["position"].setSFVec3f(list(pos))
        fields["orientation"].setSFRotation(vision.lookat(pos, target))
        fields["fieldOfView"].setSFFloat(fov or vision.CAMERA_FOV)
        labels = dict(self.labels)
        for idx in labels:
            self.setLabel(idx, "", 0, 0, 0.1, 0, 0, FONT)
        path = os.path.join(LOG_DIR, name)
        self.tick()
        self.exportImage(path, 85)
        self.tick()
        self.tick()
        fields["position"].setSFVec3f(saved[0])
        fields["orientation"].setSFRotation(saved[1])
        fields["fieldOfView"].setSFFloat(saved[2])
        self.labels = {}
        for idx, (text, color) in labels.items():
            self.label(idx, text, *self.label_pos[idx], color)
        with open(path, "rb") as f:
            return f.read()

    def vision_check(self):
        """Ask the vision model what is in each slot; compare with the simulator."""
        if not self.camera:
            print("[ROBOT] This world has no camera.")
            return None
        jpg = self.camera_jpg()
        self.wwiSendText("image:" + base64.b64encode(jpg).decode("ascii"))
        boxes = [dict(b, name=n) for n, b in self.boxes.items()]
        looks = [vision.look(b) for b in boxes]
        prompt = vision.vision_prompt({s: d for s, d in self.slot_info.items()}, looks)
        try:
            answer, usage, latency = self.call_in_thread(
                lambda: self.brain.see(jpg, prompt),
                "Asking the vision model (%s) what it sees" % self.brain.cfg["vision_model"].split("/")[-1])
        except (RuntimeError, ValueError, OSError) as e:
            self.status("Vision check failed: %s" % e, color=RED)
            self.log("vision", ok=False, error=str(e))
            return None
        truth = {}
        for slot in self.slots:
            occ = self.occupant(slot)
            truth[slot] = vision.look(dict(self.boxes[occ], name=occ)) if occ else "empty"
        correct, total, mistakes = vision.score(answer, truth)
        self.status("Vision: %d of %d slots right (%.1fs)%s" % (
            correct, total, latency, ("; " + "; ".join(mistakes[:2])) if mistakes else ""),
            "[BRAIN]")
        self.event("vision: %d/%d slots right" % (correct, total))
        self.log("vision", ok=True, correct=correct, total=total, mistakes=mistakes,
                 latency_s=latency, usage=usage, model=self.brain.cfg["vision_model"])
        return vision.locations_from_vision(answer, boxes)

    def ask_brain(self, task, world, last_error, attempt, goal=None):
        who = ("Nemotron (%s)" % self.brain.model.split("/")[-1]) if self.brain.uses_nemotron \
            else "backup planner"
        return self.call_in_thread(
            lambda: self.brain.plan(task, world, last_error, self.memory.summary(),
                                    self.system_prompt, goal),
            "Asking %s for a plan (attempt %d)" % (who, attempt))

    def run_task(self, trigger, reason=None, task_text=None, goal=None, max_attempts=None,
                 solver_goal=False) -> dict[str, Any]:
        """Plan, act, recover. `solver_goal`: let the backup planner use `goal` (orders and
        shift steps); Nemotron always works from the plain-English task."""
        self.task_text = task_text or self.read_task()
        self.task_id += 1
        self.busy = True
        self.watch = None
        self.disturbance = []
        self.question = None
        self.plan_view = []
        self.draw_plan()
        self.draw_static()
        before = self.locations()
        max_attempts = max_attempts or (MAX_ATTEMPTS if self.recovery else 1)
        print("[ROBOT] ===== Task %d (%s): %s" % (self.task_id, trigger, self.task_text))
        self.event("task %d (%s) started" % (self.task_id, trigger))
        self.log("task_start", task=self.task_text, trigger=trigger, reason=reason,
                 max_attempts=max_attempts, brain=self.brain.name)
        self.stats["tasks"] += 1
        last_error = reason
        outcome: dict[str, Any] = {"outcome": "gave_up", "attempts": max_attempts}
        try:
            for attempt in range(1, max_attempts + 1):
                if self.disturbance:
                    self.wait(SETTLE_S)  # let knocked boxes land before looking
                    self.disturbance = []
                self.status("Planning (attempt %d/%d)" % (attempt, max_attempts))
                if last_error:
                    print("[ROBOT] Telling the brain: %s" % last_error)
                locs = None
                if self.perception == "camera" and self.camera:
                    locs = self.vision_check()
                    if locs:
                        for g in self.gantries:
                            if g.held:
                                locs[g.held] = self.location(g.held)
                result = self.ask_brain(self.task_text, self.world_state(locs), last_error, attempt,
                                        goal if solver_goal else None)
                plan = result.get("plan")
                usage = result.get("usage") or {}
                if result["source"] == "nemotron" and not result.get("api_error"):
                    self.stats["nemo_calls"] += 1
                    self.stats["latency"] = round(self.stats["latency"] + result["latency_s"], 2)
                self.stats["tokens"] += int(usage.get("total_tokens", 0))
                print("[BRAIN] (%s, %.1fs) %s" % (result["source"], result["latency_s"],
                                                  result.get("reasoning", "")))
                print("[BRAIN] Plan: %s" % json.dumps(plan))
                self.show_reasoning(result["source"], result.get("reasoning", ""))
                self.log("plan", attempt=attempt, error_given=last_error,
                         source=result["source"], model=result.get("model"),
                         latency_s=result["latency_s"], reasoning=result.get("reasoning"),
                         plan=plan, usage=usage, question=result.get("question"),
                         lesson=result.get("lesson"), perception=self.perception,
                         json_retry=result.get("json_retry"), api_error=result.get("api_error"))
                if result.get("lesson"):
                    self.memory.lesson(result["lesson"])
                if result.get("question") and not plan:
                    self.question = {"q": result["question"], "task": self.task_text}
                    self.status("Nemotron asks: %s (answer in the control panel, or edit the "
                                "task and press T)" % result["question"], "[BRAIN]", MAGENTA)
                    self.event("question: %s" % result["question"])
                    self.log("question", question=result["question"])
                    outcome = {"outcome": "question", "attempts": attempt}
                    return outcome

                held = [g for g in self.gantries if g.held]
                if plan is None:
                    err = "The brain could not make a plan: %s" % result.get("api_error")
                elif not plan and held:
                    err = "The plan is empty, but robot %s is still holding %s." % (
                        held[0].name, held[0].held)
                else:
                    err = self.execute(plan) if plan else None
                if err is None and self.disturbance:
                    self.wait(SETTLE_S)
                    err = "While the plan was running, " + "; ".join(
                        "%s was knocked off %s and is now at %s" % (n, a, self.location(n))
                        for n, a in self.disturbance) + "."
                    self.disturbance = []
                if err is None and goal:
                    # Orders and benchmark tasks have a checkable goal: verify it.
                    passed, reasons = planning.check_goal(goal, self.locations(), before)
                    self.log("goal_check", passed=passed, reasons=reasons)
                    if not passed:
                        err = ("The plan finished, but the job is not done yet: %s."
                               % "; ".join(reasons))
                if err is None:
                    outcome = {"outcome": "done", "attempts": attempt}
                    if goal:
                        outcome.update(goal_passed=True, reasons=[])
                    self.status("Task done in %d attempt(s).%s" % (
                        attempt, " Watching the shelves." if self.recovery and trigger != "benchmark"
                        else ""))
                    self.event("task %d done (%d attempts)" % (self.task_id, attempt))
                    self.stats["done"] += 1
                    self.stats["attempts"] += attempt
                    if attempt > 1 or trigger in ("watch", "inspector"):
                        self.stats["recoveries"] += 1
                    self.memory.task_done()
                    self.log("task_done", attempts=attempt, final=self.locations())
                    if self.recovery and trigger != "benchmark":
                        self.watch = self.locations()
                    self.inspector_reported = set()
                    return outcome
                self.status("Problem: " + err, color=RED)
                self.event("problem: " + err[:70])
                self.log("failure", attempt=attempt, error=err)
                last_error = err
            self.status("Gave up after %d attempt(s)." % max_attempts, color=RED)
            self.stats["gave_up"] += 1
            self.stats["attempts"] += max_attempts
            self.log("gave_up", attempts=max_attempts, last_error=last_error,
                     final=self.locations())
            if goal:
                passed, reasons = planning.check_goal(goal, self.locations(), before)
                outcome.update(goal_passed=False, reasons=reasons)
            return outcome
        finally:
            self.busy = False
            self.last_result = (trigger, outcome["outcome"])
            self.draw_static()

    def check_watch(self):
        watch = self.watch
        if watch is None or self.getTime() < self.next_watch_check:
            return
        self.next_watch_check = self.getTime() + 0.5
        if any(watch[n] != loc for n, loc in self.locations().items()):
            self.status("Something changed on the shelves, waiting for it to settle...")
            self.wait(SETTLE_S)
            watch = self.watch
            if watch is None:  # S was pressed while waiting
                return
            changes = [(n, watch[n], loc) for n, loc in self.locations().items() if watch[n] != loc]
            if not changes:
                return
            desc = "; ".join("%s moved from %s to %s" % c for c in changes)
            print("[ROBOT] Change detected: " + desc)
            self.event("change: " + desc[:70])
            self.log("change_detected", changes=[list(c) for c in changes])
            self.run_repair("watch", "The task was finished, then something changed: %s. "
                                     "Put things right again." % desc,
                            {n: was for n, was, _ in changes})

    def run_repair(self, trigger, reason, put_back):
        """Repair after a change: tell the brain exactly which boxes go back where (and check
        it), instead of re-running whatever job happens to be in task.txt."""
        put_back = {b: w for b, w in put_back.items() if w in self.slots or str(w).startswith("on:")}
        if not put_back:
            return self.run_task(trigger, reason)
        text = ("Repair job: put things back exactly as they were. Move %s. Leave every other box "
                "where it is." % ", ".join("%s back to %s" % (b, w.replace("on:", "the top of "))
                                           for b, w in put_back.items()))
        return self.run_task(trigger, reason, task_text=text,
                             goal={"where": put_back, "same": True}, solver_goal=True)

    def inspect_zone(self):
        """The inspector reached an inspection point: check its zone, raise an alert if something
        is wrong."""
        bot = self.inspector
        assert bot is not None, "inspect_zone needs the humanoid inspector"
        zone = bot.request
        lo, hi = ZONES[zone][1]
        locs = self.locations()
        problems, boxes = [], []
        for name in self.boxes:
            x = self.pos(name)[0]
            expected = (self.watch or {}).get(name)
            ex = self.slots[expected][0] if expected in self.slots else None
            if not (lo <= x <= hi or (ex is not None and lo <= ex <= hi)):
                continue
            where = locs[name]
            if where in ("floor", "loose"):
                near = min(ZONES, key=lambda z: abs(ZONES[z][0] - x))
                problems.append("%s is lying %s near the %s%s" % (
                    name, "on the floor" if where == "floor" else "loose", near,
                    (" (it belongs on %s)" % expected) if expected else ""))
                boxes.append(name)
            elif expected and where != expected and not where.startswith("gripper"):
                problems.append("%s is at %s but should be at %s" % (name, where, expected))
                boxes.append(name)
        seen = None
        if self.perception == "camera" and self.brain.uses_nemotron:
            pos, target = bot.eye()
            jpg = self.camera_jpg(pos, target, 1.2, "inspector_camera.jpg")
            self.wwiSendText("image:" + base64.b64encode(jpg).decode("ascii"))
            prompt = ("You are the head camera of a small humanoid robot inspecting a stockroom. Look at the %s. "
                      "Is any box lying on the floor, tipped over, hanging off a shelf edge or clearly "
                      'out of place? Reply with ONLY {"problem": true or false, "what": "<short '
                      'description>"}.' % zone)
            try:
                seen, usage, latency = self.call_in_thread(
                    lambda: self.brain.see(jpg, prompt),
                    "Inspector camera: asking the vision model (%s)" % self.brain.cfg["vision_model"].split("/")[-1])
                self.log("inspector_vision", zone=zone, said_problem=bool(seen.get("problem")),
                         truth_problem=bool(problems), what=seen.get("what"), latency_s=latency,
                         usage=usage, model=self.brain.cfg["vision_model"])
            except (RuntimeError, ValueError, OSError) as e:
                print("[INSPECTOR] Vision failed (%s); using the inspector's simple sensors." % e)
        if seen is not None:  # in camera mode the inspector trusts what the vision model saw
            if seen.get("problem"):
                problems = problems or [str(seen.get("what") or "something looks wrong")]
            else:
                problems = []
        new = [p for p in problems if p not in self.inspector_reported]
        bot.request = None
        if not new:
            bot.last = "%s looks fine" % zone
            bot.next()
            return
        self.inspector_reported.update(new)
        delays = [self.getTime() - self.change_time.pop(b) for b in boxes if b in self.change_time]
        bot.reports += 1
        bot.last = "ALERT! " + new[0]
        self.status("Inspector: ALERT! " + "; ".join(new), "[INSPECTOR]", ORANGE)
        self.event("inspector: " + new[0][:70])
        self.log("inspector_report", zone=zone, problems=new, by="vision" if seen is not None else "sensors",
                 detect_delay_s=round(min(delays), 2) if delays else None)
        bot.next()
        if self.recovery:
            self.run_repair("inspector", "The humanoid inspector reports: %s. Put things right again."
                            % "; ".join(new),
                            {b: self.watch[b] for b in (boxes or [
                                n for n, where in self.locations().items()
                                if self.watch and self.watch.get(n) != where])
                             if self.watch and b in self.watch})

    def run_order(self, idx):
        order = self.orders[idx]
        text, goal = planning.order_to_task(order)
        print("[ROBOT] Stock order %s" % order.get("order_id"))
        self.log("order_start", order=order)
        result = self.run_task("order", task_text=text, goal=goal, solver_goal=True)
        self.log("order_result", order_id=order.get("order_id"), **result)

    def run_benchmark(self):
        tasks = planning.load_benchmark(BENCH_FILE)
        if not tasks:
            return
        model = self.brain.model if self.brain.uses_nemotron else "backup"
        self.bench = {"total": len(tasks), "done": 0, "passed": 0, "model": model, "results": []}
        print("[ROBOT] ===== Benchmark: %d tasks with %s" % (len(tasks), model))
        self.log("benchmark_start", tasks=len(tasks), model=model)
        for t in tasks:
            start = t.get("start", "initial")
            if start == "initial":
                layout = {n: ("pos",) + tuple(p) for n, p in self.initial.items()}
            elif start == "restocked":
                layout = {n: s for n, s in planning.RESTOCKED.items() if n in self.boxes}
            elif start.startswith("scatter:"):
                layout = planning.scatter_layout(list(self.boxes), list(self.slots),
                                                 int(start.split(":")[1]))
            else:
                layout = t.get("layout", {})
            layout.update(t.get("layout_extra", {}))
            self.apply_layout(layout)
            self.wait(1.0)
            result = self.run_task("benchmark", task_text=t["task"], goal=t.get("goal"))
            if t.get("expect_question"):
                passed = result["outcome"] == "question"
            else:
                passed = result["outcome"] == "done" and result.get("goal_passed", True)
            self.question = None
            self.bench["done"] += 1
            self.bench["passed"] += int(passed)
            self.bench["results"].append({"id": t["id"], "passed": passed,
                                          "outcome": result["outcome"],
                                          "attempts": result["attempts"]})
            print("[ROBOT] Benchmark %s: %s (%s)" % (t["id"], "PASS" if passed else "FAIL",
                                                     "; ".join(result.get("reasons", []))[:150]))
            self.log("benchmark_result", id=t["id"], passed=passed, outcome=result["outcome"],
                     attempts=result["attempts"], reasons=result.get("reasons", []))
        print("[ROBOT] Benchmark finished: %d of %d passed with %s"
              % (self.bench["passed"], len(tasks), model))
        self.status("Benchmark: %d of %d passed (%s)" % (self.bench["passed"], len(tasks), model))
        self.log("benchmark_done", passed=self.bench["passed"], total=len(tasks), model=model)

    # ------------------------------------------------------------------ shift
    def idle_step(self):
        """One step of the idle loop: run pending jobs, inspections and watch mode."""
        self.tick()
        if self.pending_task:
            trigger, extra, text = self.pending_task
            self.pending_task = None
            if trigger == "order":
                self.run_order(extra)
            elif trigger == "vision":
                self.vision_check()
            elif trigger == "benchmark":
                self.run_benchmark()
                if self.args["quit_after"]:
                    self.simulationQuit(0)
            elif trigger == "shift":
                if not (self.shift and self.shift.get("running")):
                    self.run_shift()
                    if self.args["quit_after"]:
                        self.simulationQuit(0)
            else:
                self.run_task(trigger, extra, task_text=text)
        elif self.inspector and self.inspector.request:
            self.inspect_zone()
        elif self.watch is not None and self.recovery and not (self.inspector and self.inspector.on):
            self.check_watch()

    def run_shift(self):
        """The full-shift scenario: many jobs and incidents in a row, then a report card."""
        sc = planning.load_scenario(SHIFT_FILE)
        if not sc:
            return
        steps = sc["steps"]
        if not self.recovery:
            self.command("recovery")  # a shift needs self-recovery and watching
        t_start = time.time()
        sim_start = self.getTime()
        self.shift = {"name": sc.get("name", "Shift"), "total": len(steps), "done": 0, "passed": 0,
                      "results": [], "running": True}
        print("[ROBOT] ===== %s: %d steps" % (self.shift["name"], len(steps)))
        self.log("shift_start", name=self.shift["name"], steps=len(steps))
        for i, st in enumerate(steps, 1):
            say = st.get("say", "step %d" % i)
            self.status("Shift step %d/%d: %s" % (i, len(steps), say), color=NVIDIA_GREEN)
            self.event("shift %d/%d: %s" % (i, len(steps), say))
            t0 = self.getTime()
            passed, note, attempts = True, "", 0
            missing = [b for b in (st.get("goal") or {}).get("where", {}) if b not in self.boxes]
            if missing:
                passed, note = False, "skipped: this world has no %s" % ", ".join(missing)
            elif st.get("event") == "scatter":
                self.command("scatter", str(st.get("seed", 42)))
                self.wait(1.0)
                note = "boxes scattered"
            elif st.get("event") == "knock":
                before = self.locations()
                start_id = self.task_id
                self.knock()
                end = self.getTime() + st.get("timeout", 120)
                while self.getTime() < end:
                    self.idle_step()
                    if (self.task_id > start_id and not self.busy and self.last_result
                            and self.last_result[0] in ("watch", "inspector")):
                        break
                now = self.locations()
                passed = now == before
                fixed_by = self.last_result[0] if self.task_id > start_id and self.last_result else None
                note = ("found by %s and repaired" % ("the humanoid inspector" if fixed_by == "inspector"
                                                           else "watch mode")) if passed else                     "not repaired: " + ", ".join("%s at %s" % (b, now[b]) for b in now if now[b] != before[b])
            else:
                if st.get("order"):
                    order = next((o for o in self.orders if o.get("order_id") == st["order"]), None)
                    if order is None:
                        passed, note = False, "order %s not found in orders/" % st["order"]
                        text = goal = None
                    else:
                        text, goal = planning.order_to_task(order)
                else:
                    text, goal = st["task"], st.get("goal")
                if text:
                    self.auto_drop = st.get("disturb") == "drop"
                    result = self.run_task("shift", task_text=text, goal=goal, solver_goal=True)
                    self.auto_drop = False
                    attempts = result["attempts"]
                    passed = result["outcome"] == "done" and result.get("goal_passed", True)
                    note = "done in %d attempt%s" % (attempts, "" if attempts == 1 else "s") if passed \
                        else "%s: %s" % (result["outcome"], "; ".join(result.get("reasons", []))[:120])
                    if st.get("disturb") == "drop" and attempts > 1:
                        note += " (recovered from a grip failure)"
            secs = round(self.getTime() - t0, 1)
            self.shift["done"] += 1
            self.shift["passed"] += int(passed)
            self.shift["results"].append({"say": say, "passed": passed, "note": note, "secs": secs})
            print("[ROBOT] Shift step %d/%d %s: %s (%s, %.0f s)" % (
                i, len(steps), "PASS" if passed else "FAIL", say, note, secs))
            self.log("shift_step", step=i, say=say, passed=passed, note=note, attempts=attempts,
                     sim_s=secs)
        sim_total = self.getTime() - sim_start
        self.shift["running"] = False
        self.shift["sim_minutes"] = round(sim_total / 60, 1)
        summary = "%s finished: %d of %d steps passed in %.1f simulated minutes" % (
            self.shift["name"], self.shift["passed"], len(steps), sim_total / 60)
        self.status(summary, color=NVIDIA_GREEN)
        print("[ROBOT] ===== " + summary + " (%.0f s real time)" % (time.time() - t_start))
        for k, r in enumerate(self.shift["results"], 1):
            print("[ROBOT]   %2d. %-4s %-38s %s" % (k, "PASS" if r["passed"] else "FAIL", r["say"][:38], r["note"]))
        self.log("shift_report", name=self.shift["name"], passed=self.shift["passed"], total=len(steps),
                 sim_minutes=self.shift["sim_minutes"], results=self.shift["results"])

    # ------------------------------------------------------------------ main
    def run(self):
        print("[ROBOT] ShelfMind ready. Brain: %s" % self.brain.name)
        if self.brain.uses_nemotron:
            print("[BRAIN] Models %s at %s" % (", ".join(self.brain.models), self.brain.cfg["base_url"]))
        print("[ROBOT] Robots: %s | grip: %s | camera: %s | %d boxes: %s" % (
            ", ".join(g.name for g in self.gantries), self.grip, "yes" if self.camera else "no",
            len(self.boxes), ", ".join("%s (%s kg)" % (n, b["mass"]) for n, b in self.boxes.items())))
        print("[ROBOT] Logging to %s" % self.log_path)
        print("[ROBOT] Control panel: double-click the robot in the 3D view (or right-click it > "
              "Robot window).")
        self.memory.start_session()
        self.log("session_start", brain=self.brain.name, models=self.brain.models,
                 robots=[g.name for g in self.gantries], grip=self.grip,
                 boxes={n: b["mass"] for n, b in self.boxes.items()})
        try:
            self.tick()
            for g in self.gantries:
                self.stationary_claim(g)
            self.draw_static()
            self.status("Ready. Click the 3D view and press T to run the task")
            if self.args["benchmark"]:
                self.pending_task = ("benchmark", None, None)
            if self.args["shift"]:
                self.pending_task = ("shift", None, None)
            while True:
                self.idle_step()
        except SimEnd:
            pass
        finally:
            self.log_file.close()


ShelfMind().run()
