"""Check your Nebius Token Factory setup before the demo: python3 check_nebius.py

1. Reads controllers/stockroom_brain/runtime.ini.
2. Lists the models your key can use and points out Nemotron / vision models.
3. Asks each configured planning model (large and small) for a real plan for
   the default task, and checks the plan is valid JSON with known names.
4. Optionally (--vision) sends a test image to the vision model.

Standard library only. Exit code 0 = ready for the demo.
"""

import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "controllers", "stockroom_brain"))
from brain import SLOT_INFO, SLOTS, Brain, build_system_prompt  # noqa: E402

TASK = ("Restock the shelves. Heavy boxes go on the low shelf and light boxes go on the high "
        "shelf. On each shelf, order them left to right: red, green, blue.")
BOXES = [("red_box_1", 2.0, "table_1"), ("blue_box_1", 0.3, "table_2"),
         ("green_box_1", 2.0, "table_3"), ("red_box_2", 0.3, "table_4"),
         ("blue_box_2", 2.0, "table_5"), ("green_box_2", 0.3, "table_6")]


def world():
    boxes = [{"name": n, "colour": n.split("_")[0], "weight_kg": m,
              "weight_class": "heavy" if m >= 1 else "light", "location": s} for n, m, s in BOXES]
    taken = {b["location"] for b in boxes}
    return {"robots": [{"name": "A", "holding": None}], "holding": None, "boxes": boxes,
            "slots": SLOT_INFO, "empty_slots": [s for s in SLOTS if s not in taken]}


def simulate(plan):
    """Run the plan on paper. Returns (final locations, problems)."""
    loc = {n: s for n, _, s in BOXES}
    held, problems = None, []
    for i, step in enumerate(plan, 1):
        a = step.get("action")
        if a == "pick":
            box = step.get("object")
            if box not in loc:
                problems.append("step %d: unknown box %r" % (i, box))
            elif held:
                problems.append("step %d: already holding %s" % (i, held))
            else:
                held, loc[box] = box, "gripper"
        elif a == "place":
            slot = step.get("slot")
            if slot not in SLOTS:
                problems.append("step %d: unknown slot %r" % (i, slot))
            elif not held:
                problems.append("step %d: gripper empty" % i)
            elif slot in loc.values():
                problems.append("step %d: %s is taken" % (i, slot))
            else:
                loc[held], held = slot, None
        else:
            problems.append("step %d: unknown action %r" % (i, a))
    if held:
        problems.append("plan ends holding %s" % held)
    return loc, problems


def tiny_png():
    """A 64x64 test image: left half red, right half blue."""
    rows = b"".join(b"\x00" + (b"\xc0\x20\x20" * 32 + b"\x20\x40\xc0" * 32) for _ in range(64))

    def chunk(kind, body):
        c = struct.pack(">I", len(body)) + kind + body
        return c + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def main():
    ok = True
    brain = Brain(os.path.join(HERE, "controllers", "stockroom_brain", "runtime.ini"))
    cfg = brain.cfg
    print("Base URL:      %s" % cfg["base_url"])
    print("Models:        %s" % ", ".join(brain.models))
    print("Vision model:  %s" % cfg["vision_model"])
    if not brain.uses_nemotron:
        print("\nNo key yet. Paste your Nebius key into controllers/stockroom_brain/runtime.ini "
              "(NEBIUS_API_KEY = ...) and run this again.")
        return 1

    print("\n1) Models your key can use")
    try:
        req = urllib.request.Request(cfg["base_url"] + "models",
                                     headers={"Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ids = sorted(m.get("id", "") for m in json.loads(resp.read().decode()).get("data", []))
        nemo = [i for i in ids if "nemotron" in i.lower()]
        print("   %d models, %d Nemotron:" % (len(ids), len(nemo)))
        for i in nemo:
            tags = []
            if i in brain.models:
                tags.append("planning (configured)")
            if i == cfg["vision_model"]:
                tags.append("vision (configured)")
            if any(k in i.lower() for k in ("vl", "vision")):
                tags.append("vision-capable")
            print("     %s%s" % (i, ("   <- " + ", ".join(tags)) if tags else ""))
        for m in brain.models + [cfg["vision_model"]]:
            if ids and m not in ids:
                print("   WARNING: %s is not in the list. Fix the name in runtime.ini." % m)
                ok = False
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("   Could not list models (%s). Continuing with the plan test." % e)

    print("\n2) Planning test (the default restock job)")
    prompt = build_system_prompt()
    want = {"red_box_1": "low_shelf_1", "green_box_1": "low_shelf_2", "blue_box_2": "low_shelf_3",
            "red_box_2": "high_shelf_1", "green_box_2": "high_shelf_2", "blue_box_1": "high_shelf_3"}
    for model in brain.models:
        brain.set_model(model)
        cfg["allow_backup"] = False
        r = brain.plan(TASK, world(), None, "", prompt)
        if r.get("plan") is None:
            print("   %s: FAILED - %s" % (model, r.get("api_error")))
            ok = False
            continue
        final, problems = simulate(r["plan"])
        correct = sum(final.get(b) == s for b, s in want.items())
        print("   %s: %.1f s, %d tokens, %d steps, %d/6 boxes in the right slot%s%s" % (
            model, r["latency_s"], (r.get("usage") or {}).get("total_tokens", 0), len(r["plan"]),
            correct, ", needed a JSON retry" if r.get("json_retry") else "",
            "" if not problems else ", PROBLEMS: " + "; ".join(problems)))
        print("      reasoning: %s" % r.get("reasoning", "")[:160])
        if problems:
            ok = False

    if "--vision" in sys.argv:
        print("\n3) Vision test")
        try:
            answer, usage, latency = brain.see(
                tiny_png(), 'This image has two halves. Reply with ONLY {"left": "<colour>", '
                            '"right": "<colour>"}.', mime="image/png")
            print("   %s: %.1f s, answer %s" % (cfg["vision_model"], latency, json.dumps(answer)))
        except (RuntimeError, ValueError, OSError, KeyError) as e:
            print("   Vision FAILED: %s" % e)
            ok = False

    print("\n%s" % ("READY for the demo." if ok else "Some checks failed, see above."))
    return 0 if ok else 1


if __name__ == "__main__":
    t0 = time.time()
    code = main()
    print("(took %.1f s)" % (time.time() - t0))
    sys.exit(code)
