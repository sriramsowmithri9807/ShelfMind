"""Memory between runs: failures and lessons, saved to memory/memory.json and
shown to Nemotron before every plan."""

import json
import os
from datetime import datetime

MAX_FAILURES = 50
MAX_LESSONS = 8


class Memory:
    def __init__(self, path):
        self.path = path
        self.data = {"runs": 0, "tasks_done": 0, "failures": [], "lessons": [], "counts": {}}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
        except OSError as e:
            print("[ROBOT] Could not save memory: %s" % e)

    def clear(self):
        self.data = {"runs": 0, "tasks_done": 0, "failures": [], "lessons": [], "counts": {}}
        self.save()

    def start_session(self):
        self.data["runs"] += 1
        self.save()

    def task_done(self):
        self.data["tasks_done"] += 1
        self.save()

    def failure(self, kind, error, box=None, slot=None):
        self.data["failures"].append({"time": datetime.now().isoformat(timespec="seconds"),
                                      "kind": kind, "box": box, "slot": slot,
                                      "error": error[:200]})
        self.data["failures"] = self.data["failures"][-MAX_FAILURES:]
        key = "%s:%s" % (kind, box or slot or "-")
        self.data["counts"][key] = self.data["counts"].get(key, 0) + 1
        self.save()

    def lesson(self, text):
        text = (text or "").strip()
        if not text:
            return
        lessons = [ln for ln in self.data["lessons"] if ln != text] + [text]
        self.data["lessons"] = lessons[-MAX_LESSONS:]
        self.save()

    @property
    def lessons(self):
        return list(self.data["lessons"])

    def summary(self):
        """Short text for the prompt."""
        parts = []
        counts = sorted(self.data["counts"].items(), key=lambda kv: -kv[1])[:6]
        if counts:
            parts.append("Problems seen in earlier runs: " + "; ".join(
                "%s x%d" % (k.replace(":", " with "), v) for k, v in counts) + ".")
        for ln in self.data["lessons"]:
            parts.append("Lesson: " + ln)
        return "\n".join(parts)
