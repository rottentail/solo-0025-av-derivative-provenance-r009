#!/usr/bin/env python3
"""Re-run selected budget-ladder rungs several times to expose stability.

The 192 MiB ceiling is not a perfectly sharp cliff: a rung that sits exactly at
the boundary can complete on one attempt and fail on another because Python
heap layout and peak ordering vary slightly.  This tool repeats each requested
rung and reports the completion rate, so the report can state a boundary as a
band rather than a false-precision threshold.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", required=True)
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--rungs", default="1,2,3,4,8,16")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    report = {"repeat": args.repeat, "runs": {}}
    for rung in [item.strip() for item in args.rungs.split(",")]:
        trial_id = f"f5_budget_{rung}mb"
        outcomes = []
        for attempt in range(args.repeat):
            results_path = work / f"{trial_id}_{attempt}.json"
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(TOOLS_DIR / "run_experiment.py"),
                    "--trials",
                    args.trials,
                    "--inputs-dir",
                    args.inputs_dir,
                    "--media-dir",
                    str(work / "media"),
                    "--results",
                    str(results_path),
                    "--only",
                    trial_id,
                    "--trial-timeout",
                    "600",
                ],
                capture_output=True,
            )
            record = json.loads(results_path.read_text())[0]
            outcomes.append(
                {
                    "completed": bool(record.get("completed")),
                    "phase_reached": record.get("phase_reached"),
                    "rss_hwm_mib": record.get("rss_hwm_mib"),
                }
            )
        counts = Counter("completed" if item["completed"] else "failed" for item in outcomes)
        report["runs"][trial_id] = {
            "attempts": args.repeat,
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "outcomes": outcomes,
        }

    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                trial: {"completed": data["completed"], "of": data["attempts"]}
                for trial, data in report["runs"].items()
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
