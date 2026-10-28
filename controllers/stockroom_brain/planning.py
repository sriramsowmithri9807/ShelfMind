"""Pure logic used by the controller (no Webots imports, easy to test):
plan dependencies for parallel robots, goal checks, stock orders, layouts."""

import json
import os
import random

DISPATCH = ("table_4", "table_5", "table_6")
GROUPS = {
    "dispatch": DISPATCH,
    "table": tuple("table_%d" % i for i in range(1, 7)),
    "low_shelf": ("low_shelf_1", "low_shelf_2", "low_shelf_3"),
    "high_shelf": ("high_shelf_1", "high_shelf_2", "high_shelf_3"),
}

# The finished default job ("restock"), used by benchmarks and the scatter test.
RESTOCKED = {
    "red_box_1": "low_shelf_1", "green_box_1": "low_shelf_2", "blue_box_2": "low_shelf_3",
    "red_box_2": "high_shelf_1", "green_box_2": "high_shelf_2", "blue_box_1": "high_shelf_3",
}


# ---------------------------------------------------------------------------
# Plan dependencies
# ---------------------------------------------------------------------------
def analyse_plan(plan, locations, holding, robots):
    """Annotate each step with its robot and the steps it must wait for.

    locations: {box: location}, holding: {robot: box or None}, robots: ["A"] or ["A", "B"].
    Two steps conflict if they use the same robot, box, slot or the bin lid.
    Returns (steps, error). Each step: {"index", "robot", "step", "deps", "uses"}."""
    loc = dict(locations)
    held = dict(holding)
    last_user = {}  # resource -> index of the last step that used it
    out = []
    for i, raw in enumerate(plan):
        robot = str(raw.get("robot") or robots[0]).upper()
        if robot not in robots:
            return out, ("Step %d uses robot '%s', but the robots are: %s."
                         % (i + 1, raw.get("robot"), ", ".join(robots)))
        action = str(raw.get("action", "")).lower()
        uses = {"robot:" + robot}
        if action == "pick":
            box = raw.get("object") or raw.get("box")
            uses |= {"box:%s" % box, "spot:%s" % loc.get(box)}
            if str(loc.get(box)) == "bin":
                uses.add("lid")
            held[robot] = box
            loc[box] = "gripper"
        elif action in ("place", "stack"):
            box = held.get(robot)
            where = raw.get("slot") if action == "place" else "on:%s" % raw.get("on")
            uses |= {"box:%s" % box, "spot:%s" % where}
            if action == "stack":
                uses.add("box:%s" % raw.get("on"))
            if where == "bin":
                uses.add("lid")
            if box:
                loc[box] = where
            held[robot] = None
        elif action in ("open_lid", "close_lid"):
            uses.add("lid")
        deps = sorted({last_user[r] for r in uses if r in last_user})
        for r in uses:
            last_user[r] = i
        out.append({"index": i, "robot": robot, "step": raw, "deps": deps, "uses": sorted(uses)})
    return out, None


# ---------------------------------------------------------------------------
# Goals (benchmark, orders)
# ---------------------------------------------------------------------------
def location_matches(where, want):
    """want may be a slot name, a group name (table, dispatch, low_shelf,
    high_shelf), "on:<box>", "floor" or a list of these."""
    if isinstance(want, (list, tuple)):
        return any(location_matches(where, w) for w in want)
    if want in GROUPS:
        return where in GROUPS[want]
    return where == want


def check_goal(goal, locations, before=None):
    """Return (passed, reasons). goal keys: where {box: want}, empty [slots],
    same true/[boxes] (unchanged since `before`)."""
    reasons = []
    for box, want in (goal.get("where") or {}).items():
        if not location_matches(locations.get(box), want):
            reasons.append("%s is at %s, wanted %s" % (box, locations.get(box), want))
    for slot in goal.get("empty") or []:
        taken = [b for b, w in locations.items() if w == slot]
        if taken:
            reasons.append("%s should be empty but holds %s" % (slot, ", ".join(taken)))
    same = goal.get("same")
    if same and before:
        names = [b for b in before if b not in (goal.get("where") or {})] if same is True else same
        for box in names:
            if locations.get(box) != before.get(box):
                reasons.append("%s moved from %s to %s but should stay"
                               % (box, before.get(box), locations.get(box)))
    return not reasons, reasons


# ---------------------------------------------------------------------------
# Stock orders
# ---------------------------------------------------------------------------
def load_orders(folder):
    orders = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        order = json.load(f)
                    order.setdefault("order_id", name[:-5])
                    orders.append(order)
                except (OSError, ValueError) as e:
                    print("[ROBOT] Could not read order %s: %s" % (name, e))
    return orders


def order_to_task(order):
    """Turn a stock order into (task text, goal)."""
    lines = order.get("lines", [])
    ship = [ln["item"] for ln in lines if ln.get("to") == "dispatch"]
    store = [ln["item"] for ln in lines if ln.get("to") == "store"]
    parts = ["Fulfil stock order %s (%s)." % (order.get("order_id"), order.get("type", "order"))]
    if order.get("note"):
        parts.append("Note from the customer: %s." % order["note"].rstrip("."))
    goal = {"where": {}, "same": True}
    if ship:
        parts.append("Move these items to the dispatch bay (table_4, table_5 or table_6): %s."
                     % ", ".join(ship))
        for item in ship:
            goal["where"][item] = "dispatch"
    if store:
        parts.append("Put these items away on the shelves: heavy boxes on the low shelf, light "
                     "boxes on the high shelf, ordered left to right red, green, blue: %s."
                     % ", ".join(store))
        for item in store:
            goal["where"][item] = RESTOCKED.get(item, ["low_shelf", "high_shelf"])
    parts.append("Leave every other box where it is.")
    return " ".join(parts), goal


def load_benchmark(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("[ROBOT] Could not read benchmark %s: %s" % (path, e))
        return []


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------
FLOOR_SPOTS = [(0.02, -0.3), (0.02, 0.3), (0.05, 0.0)]


def scatter_layout(names, slot_names, seed=None, floor_chance=0.25):
    """Random, non-overlapping layout {box: slot or ("floor", x, y)}."""
    rng = random.Random(seed)
    spots = [s for s in slot_names if s != "bin"]
    rng.shuffle(spots)
    floor = list(FLOOR_SPOTS)
    rng.shuffle(floor)
    layout = {}
    for name in names:
        if floor and rng.random() < floor_chance:
            x, y = floor.pop()
            layout[name] = ("floor", x, y)
        else:
            layout[name] = spots.pop()
    return layout


# ---------------------------------------------------------------------------
# Goal solver (backup planner for jobs that come with a machine-readable goal)
# ---------------------------------------------------------------------------
def solve_goal(world, goal):
    """Plan moves that reach `goal` (the same format as check_goal) from the world state.
    Used by the backup planner for shift steps and stock orders when Nemotron is not
    available. Returns a plan list (may be empty if the goal is already met)."""
    slot_names = [s for s in (world.get("slots") or {}) if s != "bin"]
    loc = {b["name"]: b["location"] for b in world["boxes"]}
    broken = {b["name"] for b in world["boxes"] if b.get("broken")}
    robots = [r["name"] for r in world.get("robots", [])] or ["A"]
    duo = len(robots) > 1
    want = {b: w[0] if isinstance(w, (list, tuple)) else w
            for b, w in (goal.get("where") or {}).items() if b in loc and b not in broken}
    empty = set(goal.get("empty") or [])
    plan = []

    def occupant(slot):
        return next((b for b, w in loc.items() if w == slot), None)

    def top_of(box):
        return next((b for b, w in loc.items() if w == "on:" + box), None)

    def is_done(box):
        return location_matches(loc[box], want[box])

    def wanted_slots():
        out = set()
        for b, w in want.items():
            if w in slot_names:
                out.add(w)
        return out

    def free_slot(exclude=()):
        reserved = wanted_slots() | empty | set(exclude)
        for prefer in (GROUPS["table"], slot_names):
            for s in prefer:
                if s in slot_names and s not in reserved and occupant(s) is None:
                    return s
        for s in slot_names:
            if occupant(s) is None and s not in empty and s not in exclude:
                return s
        return None

    def who(dest):
        return "B" if duo and (str(dest).startswith("high_shelf") or str(dest).startswith("dock")) else "A"

    def step(action, robot, **kw):
        s = {"action": action}
        s.update(kw)
        if duo:
            s["robot"] = robot
        plan.append(s)

    def move(box, dest):
        """Move box to a slot or onto another box ("on:<box>"), clearing what is on top first."""
        top = top_of(box)
        if top:
            move(top, free_slot())
        robot = who(dest)
        step("pick", robot, object=box)
        if str(dest).startswith("on:"):
            step("stack", robot, on=dest[3:])
        else:
            step("place", robot, slot=dest)
        loc[box] = dest

    # A box still in a gripper goes to its target or anywhere free first.
    for r in world.get("robots", []):
        held = r.get("holding")
        if held:
            dest = want.get(held) if want.get(held) in slot_names and occupant(want.get(held)) is None \
                else free_slot()
            step("place", r["name"], slot=dest)
            loc[held] = dest

    for _ in range(60):
        todo = [b for b in want if not is_done(b)]
        evict = [b for b, w in loc.items() if w in empty and b not in want]
        if evict:
            move(evict[0], free_slot())
            continue
        if not todo:
            break
        box = todo[0]
        w = want[box]
        if str(w).startswith("on:"):
            base = w[3:]
            top = top_of(base)
            if top and top != box:
                move(top, free_slot())
            move(box, w)
            continue
        if w in GROUPS:
            choices = [s for s in GROUPS[w] if s in slot_names]
            free = [s for s in choices if occupant(s) is None and s not in empty]
            if free:
                move(box, free[0])
                continue
            # every slot of the group is taken: clear one that holds a box not wanted there
            blocker = next((occupant(s) for s in choices
                            if occupant(s) not in want or not location_matches(s, want[occupant(s)])), None)
            if blocker is None:
                break
            move(blocker, free_slot(exclude=choices))
            continue
        other = occupant(w)
        if other and other != box:
            dest = want.get(other)
            if dest in slot_names and occupant(dest) is None and dest != w:
                move(other, dest)
            else:
                move(other, free_slot(exclude=[w]))
        move(box, w)
    return plan


def load_scenario(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("[ROBOT] Could not read scenario %s: %s" % (path, e))
        return None
