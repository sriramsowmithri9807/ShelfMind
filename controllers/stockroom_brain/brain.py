"""ShelfMind brain: asks NVIDIA Nemotron (via Nebius Token Factory) for a plan.

Standard library only. If there is no API key, or the API fails and
ALLOW_BACKUP_PLANNER=1, a simple rule-based "backup" planner is used instead.
"""

import base64
import configparser
import json
import os
import re
import time
import urllib.error
import urllib.request

import planning

# ---------------------------------------------------------------------------
# Slots (shared with the controller). name -> (x, y, surface height)
# Slot 1 = left (y +0.25), 2 = middle, 3 = right (y -0.25), seen from the camera.
# ---------------------------------------------------------------------------
SLOT_Y = {1: 0.25, 2: 0.0, 3: -0.25}
SIDE = {1: "left", 2: "middle", 3: "right"}

SLOTS = {}
SLOT_INFO = {}
for _i in (1, 2, 3):
    SLOTS["table_%d" % _i] = (-0.65, SLOT_Y[_i], 0.40)
    SLOT_INFO["table_%d" % _i] = "pickup table, back row, %s" % SIDE[_i]
for _i in (1, 2, 3):
    SLOTS["table_%d" % (_i + 3)] = (-0.40, SLOT_Y[_i], 0.40)
    SLOT_INFO["table_%d" % (_i + 3)] = "pickup table, front row (dispatch bay), %s" % SIDE[_i]
for _i in (1, 2, 3):
    SLOTS["low_shelf_%d" % _i] = (0.35, SLOT_Y[_i], 0.30)
    SLOT_INFO["low_shelf_%d" % _i] = "low shelf (for heavy boxes), %s" % SIDE[_i]
for _i in (1, 2, 3):
    SLOTS["high_shelf_%d" % _i] = (0.75, SLOT_Y[_i], 0.60)
    SLOT_INFO["high_shelf_%d" % _i] = "high shelf (for light boxes), %s" % SIDE[_i]

DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_SMALL_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
DEFAULT_VISION_MODEL = "google/gemma-3-27b-it"
DEFAULT_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"
PLACEHOLDER_KEY = "paste-your-key-here"

RETRY_MESSAGE = "That was not valid JSON. Reply with ONLY the JSON object."


def build_system_prompt(robots=("A",), extras=()):
    """The planner's instructions. `extras` may contain "stack", "bin"."""
    duo = len(robots) > 1
    lines = [
        "You are the planning brain of ShelfMind, a stockroom robot.",
        "",
        "THE ROBOT",
    ]
    if duo:
        lines += [
            "- TWO gantry robots, A and B, share the same rails above a pickup table and two shelves.",
            "  Each moves a suction gripper in x, y and z and holds at most ONE box at a time.",
            "- Every step must say which robot does it: add \"robot\": \"A\" or \"robot\": \"B\".",
            "- Steps of different robots run AT THE SAME TIME when they do not touch the same box or slot,",
            "  so split the work evenly between A and B to finish faster. They cannot pass each other:",
            "  A always stays on the table side (west), B on the high-shelf side (east). The controller",
            "  makes them wait for each other, so do not worry about collisions.",
        ]
    else:
        lines += [
            "- A gantry robot moves a suction gripper in x, y and z above a pickup table and two shelves.",
            "- It holds at most ONE box at a time.",
        ]
    lines += [
        "- Actions:",
        '  {"action": "pick", "object": "<box name>"}  picks up that box from wherever it is (table, shelf, floor).',
        '  {"action": "place", "slot": "<slot name>"}  puts the held box down in that empty slot.',
    ]
    if "stack" in extras:
        lines.append('  {"action": "stack", "on": "<box name>"}  puts the held box on top of that box '
                     '(the lower box must be in a slot with nothing on top; at most 2 boxes high).')
    if "dock" in extras:
        lines.append("- dock_1 .. dock_3 are slots on the inbound dock at the east end, next to robot B's "
                     "home. New stock arrives there.")
    if "bin" in extras:
        lines += [
            '  {"action": "open_lid"} and {"action": "close_lid"}  open/close the lid of the parts bin.',
            '  The slot "bin" is inside the parts bin. Its lid must be open to place into or pick from it.',
        ]
    lines += [
        "",
        "SLOTS",
        "- table_1 .. table_6 on the pickup table (table_4..6 is the front row, also called the dispatch bay),",
        "  low_shelf_1 .. low_shelf_3 on the low shelf, high_shelf_1 .. high_shelf_3 on the high shelf.",
        "- Slot 1 is left, slot 2 is middle, slot 3 is right (as seen from the camera).",
        "- Each slot holds at most one box (plus one stacked on top, if stacking is allowed).",
        "",
        "RULES",
        "1. Pick before place. Each robot alternates: pick, place, pick, place.",
        "2. Never place into a slot that is taken. If a slot you need is taken by the wrong box,",
        "   first move that box to an empty slot.",
        "3. Use only box names and slot names that appear in the world state.",
        "4. Leave boxes that are already in a correct slot alone.",
        '5. A box whose location is "floor" or "loose" is not in any slot: pick it up and place it.',
        "6. Every robot must end with an empty gripper.",
        "7. Boxes marked broken must be left alone. Carry fragile boxes like any other box.",
        "",
        "FEEDBACK AND MEMORY",
        "- Every message gives the task, the CURRENT world state, the last error (if any) and lessons from past runs.",
        "- The world state is always the truth. Earlier steps may already have run, so plan ONLY the steps",
        "  still needed from now on.",
        "- If a robot is holding a box, that robot's first step must place it.",
        "- If the task is already done, return an empty plan.",
        "- If you recover from an error, you may add a short \"lesson\" for future runs.",
        "",
        "QUESTIONS",
        "- Only if the task is truly ambiguous (you cannot tell which boxes or slots are meant), do not guess:",
        '  return an empty plan and one short "question" for the human.',
        "",
        "OUTPUT",
        "Reply with ONLY this JSON object and nothing else:",
        '{"reasoning": "<one or two short sentences>", "plan": [ ... ], "question": "", "lesson": ""}',
    ]
    return "\n".join(lines)


# Kept for backwards compatibility (single robot, basic actions).
SYSTEM_PROMPT = build_system_prompt()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(ini_path):
    """Read settings from the environment (Webots sets it from runtime.ini),
    falling back to parsing runtime.ini ourselves."""
    ini = {}
    if ini_path and os.path.exists(ini_path):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keep key case
        try:
            parser.read(ini_path)
            if parser.has_section("environment variables"):
                ini = dict(parser.items("environment variables"))
        except configparser.Error:
            pass

    def get(name, default=""):
        value = os.environ.get(name) or ini.get(name) or default
        return value.strip().strip('"').strip("'")

    base_url = get("NEBIUS_BASE_URL", DEFAULT_BASE_URL)
    if not base_url.endswith("/"):
        base_url += "/"
    key = get("NEBIUS_API_KEY")
    if key == PLACEHOLDER_KEY:
        key = ""
    return {
        "api_key": key,
        "model": get("NEBIUS_MODEL", DEFAULT_MODEL),
        "small_model": get("NEBIUS_MODEL_SMALL", DEFAULT_SMALL_MODEL),
        "vision_model": get("NEBIUS_VISION_MODEL", DEFAULT_VISION_MODEL),
        "base_url": base_url,
        "allow_backup": get("ALLOW_BACKUP_PLANNER", "1") == "1",
    }


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------
def extract_json(text):
    """Find the JSON object in a model reply. Raises ValueError."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    if "</think>" in t.lower():  # opening tag was cut off
        t = re.split(r"</think>", t, flags=re.I)[-1]
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S | re.I)
    if fence:
        t = fence.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    obj = json.loads(t[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def parse_reply(text):
    """Turn a model reply into {"reasoning", "plan", "question", "lesson"}. Raises ValueError."""
    obj = extract_json(text)
    question = str(obj.get("question") or "").strip()
    plan = obj.get("plan")
    if plan is None and question:
        plan = []
    if not isinstance(plan, list):
        raise ValueError('reply has no "plan" list')
    return {
        "reasoning": str(obj.get("reasoning", "")).strip(),
        "plan": [step for step in plan if isinstance(step, dict)],
        "question": question,
        "lesson": str(obj.get("lesson") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Backup planner (no LLM)
# ---------------------------------------------------------------------------
COLOUR_ORDER = {"red": 0, "green": 1, "blue": 2}


def backup_plan(world):
    """Heavy boxes -> low shelf, light boxes -> high shelf, each ordered
    left to right red, green, blue. Boxes on the wrong shelf move first.
    Parts boxes go into the parts bin. With two robots, moves to the high
    shelf go to robot B and the rest to robot A."""
    boxes = [b for b in world["boxes"] if not b.get("broken")]
    slot_names = list(world.get("slots") or SLOTS)
    loc = {b["name"]: b["location"] for b in world["boxes"]}
    occ = {where: name for name, where in loc.items() if where in slot_names}
    robots = [r["name"] for r in world.get("robots", [])] or ["A"]
    duo = len(robots) > 1

    target = {}
    parts = [b for b in boxes if "parts" in b["name"]]
    if "bin" in slot_names and parts:
        target[parts[0]["name"]] = "bin"
    for cls, shelf in (("heavy", "low_shelf"), ("light", "high_shelf")):
        group = sorted((b for b in boxes if b["weight_class"] == cls and b["name"] not in target),
                       key=lambda b: (COLOUR_ORDER.get(b["colour"], 9), b["name"]))
        for i, b in enumerate(group[:3]):
            target[b["name"]] = "%s_%d" % (shelf, i + 1)

    plan = []
    lid = {"open": (world.get("bin") or {}).get("lid") == "open"}

    def who(slot):
        return "B" if duo and slot.startswith("high_shelf") else "A"

    def step(action, robot, **kw):
        s = {"action": action}
        s.update(kw)
        if duo:
            s["robot"] = robot
        plan.append(s)

    def free_slot():
        wanted = set(target.values())
        for name in slot_names:  # table slots come first
            if name not in occ and name not in wanted and name != "bin":
                return name
        for name in slot_names:
            if name not in occ and name != "bin":
                return name
        return None

    def need_lid(slot, robot):
        if slot == "bin" and not lid["open"]:
            step("open_lid", robot)
            lid["open"] = True

    def put(name, slot, robot):
        need_lid(slot, robot)
        step("place", robot, slot=slot)
        occ[slot] = name
        loc[name] = slot

    def move(name, slot):
        robot = who(slot)
        need_lid(loc[name], robot)
        step("pick", robot, object=name)
        if loc[name] in occ and occ[loc[name]] == name:
            del occ[loc[name]]
        put(name, slot, robot)

    for r in world.get("robots", []):
        held = r.get("holding")
        if held:
            t = target.get(held)
            put(held, t if t and t not in occ else free_slot(), r["name"])
    if not world.get("robots") and world.get("holding"):
        held = world["holding"]
        t = target.get(held)
        put(held, t if t and t not in occ else free_slot(), "A")

    def wrong_shelf(b):
        where, t = loc[b["name"]], target.get(b["name"])
        return bool(t) and "shelf" in where and where.rsplit("_", 1)[0] != t.rsplit("_", 1)[0]

    order = [b["name"] for b in sorted(boxes, key=lambda b: not wrong_shelf(b))]
    for _ in range(40):
        pending = [n for n in order if n in target and loc[n] != target[n]]
        if not pending:
            break
        name = pending[0]
        slot = target[name]
        if slot in occ:
            blocker = occ[slot]
            bt = target.get(blocker)
            dest = bt if bt and bt not in occ else free_slot()
            if dest is None:
                break
            move(blocker, dest)
        if slot in occ:
            continue
        move(name, slot)
    if lid["open"] and "bin" in slot_names:
        step("close_lid", "A")

    moves = sum(1 for s in plan if s["action"] == "pick")
    reasoning = ("Rule-based plan: heavy boxes to the low shelf, light boxes to the high "
                 "shelf, ordered red, green, blue; %d move%s." % (moves, "" if moves == 1 else "s"))
    return {"reasoning": reasoning, "plan": plan, "question": "", "lesson": ""}


# ---------------------------------------------------------------------------
# The brain
# ---------------------------------------------------------------------------
class Brain:
    def __init__(self, ini_path=None):
        self.cfg = load_config(ini_path)
        self.models = [self.cfg["model"]]
        if self.cfg["small_model"] and self.cfg["small_model"] != self.cfg["model"]:
            self.models.append(self.cfg["small_model"])
        self.model = self.models[0]

    @property
    def uses_nemotron(self):
        return bool(self.cfg["api_key"])

    @property
    def name(self):
        if self.uses_nemotron:
            return "NVIDIA Nemotron via Nebius Token Factory"
        return "BACKUP planner (no Nebius key set)"

    def next_model(self):
        """Switch between the large and the small Nemotron model."""
        i = self.models.index(self.model) if self.model in self.models else -1
        self.model = self.models[(i + 1) % len(self.models)]
        return self.model

    def set_model(self, model):
        if model in self.models:
            self.model = model
        return self.model

    def _post(self, payload, timeout=90):
        req = urllib.request.Request(
            self.cfg["base_url"] + "chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + self.cfg["api_key"],
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError("HTTP %d from Nebius: %s" % (e.code, detail))
        except urllib.error.URLError as e:
            raise RuntimeError("could not reach Nebius: %s" % e.reason)
        msg = data["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning_content") or ""
        return text, data.get("usage") or {}

    def _chat(self, messages, model=None):
        return self._post({
            "model": model or self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 4096,
        })

    def _ask_nemotron(self, system_prompt, user_text):
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}]
        usage = {}

        def add_usage(u):
            for k, v in u.items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v

        text, u = self._chat(messages)
        add_usage(u)
        try:
            return parse_reply(text), usage, False
        except ValueError as e:
            print("[BRAIN] Reply was not valid JSON (%s). Asking again." % e)
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": RETRY_MESSAGE}]
        text, u = self._chat(messages)
        add_usage(u)
        return parse_reply(text), usage, True

    def plan(self, task, world, last_error=None, memory_text="", system_prompt=None, goal=None):
        """Return dict: source, model, reasoning, plan (list or None), question,
        lesson, latency_s, usage, json_retry, api_error.

        `goal` (machine-readable, optional) is only used by the backup planner: Nemotron
        always works from the plain-English task."""
        t0 = time.time()
        out = {"source": "backup", "model": None, "usage": {}, "json_retry": False,
               "api_error": None, "question": "", "lesson": ""}
        if self.uses_nemotron:
            user_text = ("TASK:\n%s\n\nLAST ERROR:\n%s\n\nLESSONS FROM PAST RUNS:\n%s\n\n"
                         "CURRENT WORLD STATE (JSON):\n%s"
                         % (task, last_error or "none", memory_text or "none",
                            json.dumps(world, indent=1)))
            try:
                parsed, usage, retried = self._ask_nemotron(
                    system_prompt or SYSTEM_PROMPT, user_text)
                out.update(source="nemotron", model=self.model, usage=usage,
                           json_retry=retried, **parsed)
                out["latency_s"] = round(time.time() - t0, 2)
                return out
            except (RuntimeError, ValueError, KeyError, IndexError, OSError) as e:
                out["api_error"] = str(e)
                print("[BRAIN] Nemotron call failed: %s" % e)
                if not self.cfg["allow_backup"]:
                    out.update(source="nemotron", model=self.model, plan=None,
                               reasoning="Nemotron call failed: %s" % e)
                    out["latency_s"] = round(time.time() - t0, 2)
                    return out
        if goal:
            plan = planning.solve_goal(world, goal)
            moves = sum(1 for st in plan if st["action"] == "pick")
            out.update(plan=plan, reasoning="Rule-based plan from the job's checklist (no Nemotron): "
                       "%d move%s." % (moves, "" if moves == 1 else "s"))
        else:
            out.update(backup_plan(world))
        if out["api_error"]:
            out["reasoning"] = "Nemotron unavailable, so: " + out["reasoning"]
        out["latency_s"] = round(time.time() - t0, 2)
        return out

    def see(self, image_bytes, prompt, mime="image/jpeg"):
        """Ask the vision model about a camera image. Returns (dict, usage, latency_s)."""
        if not self.uses_nemotron:
            raise RuntimeError("vision needs a Nebius key (NEBIUS_API_KEY in runtime.ini)")
        t0 = time.time()
        url = "data:%s;base64,%s" % (mime, base64.b64encode(image_bytes).decode("ascii"))
        text, usage = self._post({
            "model": self.cfg["vision_model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }, timeout=120)
        return extract_json(text), usage, round(time.time() - t0, 2)
