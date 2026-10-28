"""Summarise ShelfMind logs: python3 results.py

Reads every logs/*.jsonl file and prints:
  - a table per run, plus totals (tasks, success rate, attempts, recoveries,
    Nemotron latency, tokens, recovery on vs off)
  - a comparison per model (large vs small Nemotron, backup planner)
  - benchmark results per model, and vision accuracy
Saves results.png if matplotlib is installed.
"""

import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def tasks_of(events):
    """Group events into tasks: {task_id: {...}}."""
    tasks = {}
    for e in events:
        if e["event"] == "task_start":
            tasks[e["task_id"]] = {"trigger": e.get("trigger"), "recovery": e.get("recovery"),
                                   "attempts": 0, "outcome": "unfinished", "models": set()}
        t = tasks.get(e.get("task_id"))
        if t is None:
            continue
        if e["event"] == "plan":
            t["attempts"] = max(t["attempts"], e.get("attempt", 0))
            t["models"].add("backup" if e.get("source") == "backup" else e.get("model"))
        elif e["event"] == "task_done":
            t["outcome"] = "done"
        elif e["event"] == "gave_up":
            t["outcome"] = "gave_up"
        elif e["event"] == "question":
            t["outcome"] = "question"
    return tasks


def summarise(events):
    tasks = tasks_of(events)
    done = [t for t in tasks.values() if t["outcome"] == "done"]
    finished = [t for t in tasks.values() if t["outcome"] in ("done", "gave_up")]
    plans = [e for e in events if e["event"] == "plan"]
    nemo = [e for e in plans if e.get("source") == "nemotron" and not e.get("api_error")]
    s = {
        "tasks": len(tasks),
        "done": len(done),
        "gave_up": sum(t["outcome"] == "gave_up" for t in tasks.values()),
        "questions": sum(t["outcome"] == "question" for t in tasks.values()),
        "finished": len(finished),
        "attempts": sum(t["attempts"] for t in finished),
        # A recovery = a finished task that needed a replan or repaired a change.
        "recoveries": sum(t["attempts"] > 1 or t["trigger"] in ("watch", "inspector", "duck") for t in done),
        "drops": sum(e["event"] == "drop" for e in events),
        "knocks": sum(e["event"] == "knock" for e in events),
        "nemo_calls": len(nemo),
        "nemo_latency": sum(e.get("latency_s", 0) for e in nemo),
        "backup_calls": sum(e.get("source") == "backup" for e in plans),
        "tokens": sum((e.get("usage") or {}).get("total_tokens", 0) for e in plans),
        "by_mode": {},
    }
    for mode in (True, False):
        f = [t for t in finished if t["recovery"] == mode]
        s["by_mode"][mode] = (sum(t["outcome"] == "done" for t in f), len(f))
    return s


def add(total, s):
    for k, v in s.items():
        if k == "by_mode":
            for mode, (ok, n) in v.items():
                a, b = total["by_mode"].get(mode, (0, 0))
                total["by_mode"][mode] = (a + ok, b + n)
        else:
            total[k] = total.get(k, 0) + v


def pct(a, b):
    return "%5.1f%%" % (100.0 * a / b) if b else "   n/a"


def avg(a, b, fmt="%.2f"):
    return fmt % (a / b) if b else "n/a"


def short(model):
    return (model or "backup").split("/")[-1]


def per_model(all_events):
    """Plan calls and task outcomes per model."""
    rows = {}
    for events in all_events:
        for e in events:
            if e["event"] != "plan":
                continue
            m = short(e.get("model") if e.get("source") == "nemotron" else "backup")
            r = rows.setdefault(m, {"calls": 0, "latency": 0.0, "tokens": 0, "errors": 0,
                                    "tasks_done": 0, "tasks_finished": 0})
            r["calls"] += 1
            r["latency"] += e.get("latency_s", 0)
            r["tokens"] += (e.get("usage") or {}).get("total_tokens", 0)
            r["errors"] += bool(e.get("api_error"))
        for t in tasks_of(events).values():
            if t["outcome"] not in ("done", "gave_up") or len(t["models"]) != 1:
                continue
            m = short(next(iter(t["models"])))
            if m in rows:
                rows[m]["tasks_finished"] += 1
                rows[m]["tasks_done"] += t["outcome"] == "done"
    return rows


def benchmarks(all_events):
    """Latest benchmark result per (model, task id), plus pass counts per model."""
    results = {}
    for events in all_events:
        for e in events:
            if e["event"] == "benchmark_result":
                results.setdefault(short(e.get("model")), {})[e["id"]] = e["passed"]
    return results


def main():
    files = sorted(glob.glob(os.path.join(HERE, "logs", "*.jsonl")))
    if not files:
        print("No logs found in logs/. Run the simulation first.")
        return
    all_events = [load(p) for p in files]
    header = "%-26s %-22s %5s %5s %6s %7s %8s %6s %5s %6s %8s %8s" % (
        "run", "world", "tasks", "done", "gaveup", "success", "avg_att", "recov", "drops",
        "knocks", "lat_s", "tokens")
    print(header)
    print("-" * len(header))
    total = {"by_mode": {}}
    for path, events in zip(files, all_events):
        s = summarise(events)
        if not s["tasks"]:
            continue
        add(total, s)
        world = next((e.get("world") for e in events if e.get("world")), "-")
        print("%-26s %-22s %5d %5d %6d %7s %8s %6d %5d %6d %8s %8d" % (
            os.path.basename(path)[:26], (world or "-")[:22], s["tasks"], s["done"], s["gave_up"],
            pct(s["done"], s["finished"]), avg(s["attempts"], s["finished"]),
            s["recoveries"], s["drops"], s["knocks"],
            avg(s["nemo_latency"], s["nemo_calls"]), s["tokens"]))
    if "tasks" not in total:
        print("No tasks in the logs yet.")
        return

    print()
    print("TOTALS")
    print("  Tasks run:               %d (%d finished, %d asked a question, %d unfinished)" % (
        total["tasks"], total["finished"], total["questions"],
        total["tasks"] - total["finished"] - total["questions"]))
    print("  Success rate:            %s (%d done, %d gave up)" % (
        pct(total["done"], total["finished"]).strip(), total["done"], total["gave_up"]))
    print("  Average attempts:        %s" % avg(total["attempts"], total["finished"]))
    print("  Recoveries:              %d (drops: %d, knocks: %d)" % (
        total["recoveries"], total["drops"], total["knocks"]))
    print("  Avg Nemotron latency:    %s over %d calls (backup planner calls: %d)" % (
        avg(total["nemo_latency"], total["nemo_calls"], "%.2f s"), total["nemo_calls"],
        total["backup_calls"]))
    print("  Total tokens:            %d" % total["tokens"])
    for mode, name in ((True, "ON "), (False, "OFF")):
        ok, n = total["by_mode"].get(mode, (0, 0))
        print("  Self-recovery %s:       %s (%d of %d tasks)" % (name, pct(ok, n).strip(), ok, n))

    models = per_model(all_events)
    print()
    print("PER MODEL")
    print("  %-30s %6s %10s %10s %7s %14s" % ("model", "calls", "avg_lat_s", "tokens", "errors",
                                               "task success"))
    for m, r in sorted(models.items()):
        print("  %-30s %6d %10s %10d %7d %14s" % (
            m, r["calls"], avg(r["latency"], r["calls"]), r["tokens"], r["errors"],
            "%s (%d/%d)" % (pct(r["tasks_done"], r["tasks_finished"]).strip(), r["tasks_done"],
                            r["tasks_finished"])))

    bench = benchmarks(all_events)
    if bench:
        print()
        print("BENCHMARK (latest result per task)")
        ids = sorted({i for r in bench.values() for i in r})
        names = sorted(bench)
        print("  %-22s %s" % ("task", " ".join("%-18s" % n[:18] for n in names)))
        for i in ids:
            print("  %-22s %s" % (i[:22], " ".join(
                "%-18s" % {True: "PASS", False: "FAIL", None: "-"}[bench[n].get(i)] for n in names)))
        print("  %-22s %s" % ("passed", " ".join(
            "%-18s" % ("%d/%d" % (sum(bench[n].values()), len(bench[n]))) for n in names)))

    vision = [e for events in all_events for e in events if e["event"] == "vision" and e.get("ok")]
    if vision:
        right = sum(e["correct"] for e in vision)
        seen = sum(e["total"] for e in vision)
        print()
        print("VISION: %d checks, %s of slots read correctly (%d/%d), avg %.1f s" % (
            len(vision), pct(right, seen).strip(), right, seen,
            sum(e.get("latency_s", 0) for e in vision) / len(vision)))

    reports = [e for events in all_events for e in events if e["event"] in ("inspector_report", "duck_report")]
    looks = [e for events in all_events for e in events if e["event"] in ("inspector_vision", "duck_vision")]
    if reports or looks:
        delays = [e["detect_delay_s"] for e in reports if e.get("detect_delay_s") is not None]
        print()
        print("HUMANOID INSPECTOR: %d reports (%d by sensors, %d by the vision model)" % (
            len(reports), sum(e.get("by") == "sensors" for e in reports),
            sum(e.get("by") == "vision" for e in reports)))
        if delays:
            print("  Time from knock to alert: avg %.1f s, fastest %.1f s, slowest %.1f s" % (
                sum(delays) / len(delays), min(delays), max(delays)))
        if looks:
            right = sum(e["said_problem"] == e["truth_problem"] for e in looks)
            print("  Inspector camera + vision model: %d of %d inspections judged correctly" % (
                right, len(looks)))

    shifts = [e for events in all_events for e in events if e["event"] == "shift_report"]
    if shifts:
        print()
        print("FULL SHIFT (latest of %d)" % len(shifts))
        last = shifts[-1]
        print("  %d of %d steps passed in %.1f simulated minutes (%s)" % (
            last["passed"], last["total"], last.get("sim_minutes", 0), last.get("model", "?")))
        for i, r in enumerate(last["results"], 1):
            print("  %2d. %-4s %-42s %s" % (i, "PASS" if r["passed"] else "FAIL", r["say"][:42], r["note"]))

    plot(total, bench)


def plot(total, bench):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib is not installed, so results.png was skipped "
              "(pip install matplotlib to get the chart).")
        return
    ink, muted, bar, grid = "#1f2328", "#6e7781", "#2f6fdf", "#e5e7eb"
    panels = 2 if bench else 1
    fig, axes = plt.subplots(1, panels, figsize=(5 * panels, 3.4), dpi=150, squeeze=False)

    def draw(ax, names, oks, ns, title, ylabel):
        rates = [100.0 * ok / n if n else 0 for ok, n in zip(oks, ns)]
        bars = ax.bar(names, rates, width=0.5, color=bar)
        for b, ok, n in zip(bars, oks, ns):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2,
                    "%d%%  (%d/%d)" % (round(100.0 * ok / n), ok, n) if n else "no runs",
                    ha="center", va="bottom", color=ink, fontsize=9)
        ax.set_xlim(-0.8, max(len(names), 2) - 0.2)
        ax.set_ylim(0, 115)
        ax.set_yticks([0, 25, 50, 75, 100])
        ax.set_ylabel(ylabel, color=muted)
        ax.set_title(title, color=ink, loc="left", fontsize=11)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(muted)
        ax.tick_params(colors=muted, labelsize=8)
        ax.yaxis.grid(True, color=grid, linewidth=0.8)
        ax.set_axisbelow(True)

    on, off = total["by_mode"].get(True, (0, 0)), total["by_mode"].get(False, (0, 0))
    draw(axes[0][0], ["Self-recovery ON", "Self-recovery OFF"], [on[0], off[0]], [on[1], off[1]],
         "ShelfMind task success rate", "Tasks completed (%)")
    if bench:
        names = sorted(bench)
        draw(axes[0][1], [n[:20] for n in names], [sum(bench[n].values()) for n in names],
             [len(bench[n]) for n in names], "Benchmark pass rate by planner", "Tasks passed (%)")
    fig.tight_layout()
    out = os.path.join(HERE, "results.png")
    fig.savefig(out)
    print("\nSaved %s" % out)


if __name__ == "__main__":
    main()
