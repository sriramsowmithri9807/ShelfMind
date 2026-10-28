"""Helpers for the vision model: the prompt, and scoring the answer against the
simulator's truth."""

import math

# The overview camera pose: behind the table, looking straight at the shelves.
CAMERA_POS = (-1.6, 0.0, 1.9)
CAMERA_TARGET = (0.25, 0.0, 0.4)
CAMERA_FOV = 1.1


def lookat(pos, target):
    """Axis-angle rotation pointing a Webots viewpoint (x forward, z up) at target."""
    dx, dy, dz = (t - p for t, p in zip(target, pos))
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(-dz, math.hypot(dx, dy))
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    r = [[cy * cp, -sy, cy * sp], [sy * cp, cy, sy * sp], [-sp, 0, cp]]
    ang = math.acos(max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1) / 2)))
    if ang < 1e-9:
        return [0.0, 0.0, 1.0, 0.0]
    ax = [r[2][1] - r[1][2], r[0][2] - r[2][0], r[1][0] - r[0][1]]
    n = math.sqrt(sum(a * a for a in ax))
    return [a / n for a in ax] + [ang]


def look(box):
    """How a box looks, e.g. "dark red", "pale blue", "big yellow", "clear glass"."""
    name = box["name"]
    if name.startswith("glass"):
        return "clear glass"
    if "parts" in name:
        return "small orange"
    if "big" in name:
        return "big %s" % box["colour"]
    return "%s %s" % ("dark" if box["class"] == "heavy" else "pale", box["colour"])


def vision_prompt(slot_info, looks):
    slots = "\n".join("- %s: %s" % (s, d) for s, d in slot_info.items())
    return (
        "This image is from the overview camera of a stockroom robot. The camera looks from behind "
        "the pickup table (brown, in front) towards the low shelf (blue, middle) and the high shelf "
        "(orange, at the back). On the table, table_1..3 is the row nearest the camera and "
        "table_4..6 the row nearest the shelves. Slot 1 is on the LEFT of the image, slot 2 in the "
        "middle, slot 3 on the RIGHT.\n\nSlots:\n%s\n\n"
        "The boxes can look like: %s.\n\n"
        "Say which box is in each slot. Reply with ONLY this JSON object:\n"
        '{"slots": {"<slot>": "<look or empty>", ...}, "floor": ["<look>", ...]}'
        % (slots, ", ".join(sorted(set(looks)))))


def score(answer, truth_slots):
    """answer: model JSON; truth_slots: {slot: look or "empty"}.
    Returns (correct, total, mistakes)."""
    seen = {str(k): str(v).strip().lower() for k, v in (answer.get("slots") or {}).items()}
    correct, mistakes = 0, []
    for slot, want in truth_slots.items():
        got = seen.get(slot, "empty") or "empty"
        if got in ("none", "nothing", ""):
            got = "empty"
        if got == want:
            correct += 1
        else:
            mistakes.append("%s: saw %s, really %s" % (slot, got, want))
    return correct, len(truth_slots), mistakes


def locations_from_vision(answer, boxes):
    """Map the vision answer back to box names. Returns {box: location}."""
    by_look = {}
    for b in boxes:
        by_look.setdefault(look(b), []).append(b["name"])
    out = {b["name"]: "unknown" for b in boxes}
    for slot, seen in (answer.get("slots") or {}).items():
        names = by_look.get(str(seen).strip().lower())
        if names:
            out[names.pop(0)] = slot
    for seen in answer.get("floor") or []:
        names = by_look.get(str(seen).strip().lower())
        if names:
            out[names.pop(0)] = "floor"
    return out
