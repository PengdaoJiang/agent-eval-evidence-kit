import argparse
import json
from pathlib import Path

from .core import grade, sample_trace, schedule, strict_loads, summarize


def main():
    parser = argparse.ArgumentParser(description="Offline trace evaluation; no model calls")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("grade")
    one.add_argument("trace", type=Path)
    plan = sub.add_parser("schedule")
    plan.add_argument("--seed", type=int, default=17)
    plan.add_argument("--repeats", type=int, default=2)
    demo = sub.add_parser("demo")
    demo.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "grade":
            result = grade(strict_loads(args.trace.read_text(encoding="utf-8")))
            print(json.dumps(result, indent=2))
            return 0 if result["strict_success"] else 1
        if args.command == "schedule":
            print(json.dumps(schedule(args.seed, args.repeats), indent=2))
            return 0
        planned = schedule()
        traces = [sample_trace(r["fixture_id"], r["attempt_id"]) for r in planned]
        traces[1]["tools"].append(dict(traces[1]["tools"][0]))
        traces[4]["answer"] = {"incorrect": True}
        traces.pop()  # Intentionally missing attempt, not silently discarded.
        report = summarize(planned, traces)
        # Exclusive file creation prevents overwriting a previous experiment.
        args.out.mkdir(parents=True, exist_ok=False)
        for name, data in [("schedule.json", planned), ("synthetic-traces.json", traces), ("trace.json", traces[0]), ("report.json", report)]:
            (args.out / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        lines = ["# Synthetic evaluator demonstration", "", "These are not model benchmark results.", "",
                 f"Planned attempts: {len(planned)}; present: {len(traces)}.",
                 f"Missing: {', '.join(report['missing_attempt_ids'])}.", "", "## Paired outcomes", ""]
        lines.extend(f"- {key}: {value}" for key, value in report["paired_outcomes"].items())
        (args.out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.out), "provenance": "synthetic", "planned": len(planned), "present": len(traces)}, indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
