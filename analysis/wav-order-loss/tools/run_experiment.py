#!/usr/bin/env python3
"""Orchestrator: run every trial in a fresh 192 MiB process and record results.

Each child gets its own data directory and media directory, so outputs cannot
collide and state files stay small.  A child that dies from the memory cap is
recorded as incomplete with the phase it reached; it is never dropped.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = TOOLS_DIR.parent


def run_one(trial: dict, args: argparse.Namespace, base_tmp: Path) -> dict:
    trial_dir = base_tmp / trial["trial_id"]
    data_dir = trial_dir / "data"
    media_dir = args.media_dir
    inputs_dir = args.inputs_dir
    record_path = trial_dir / "record.json"
    trial_json = trial_dir / "trial.json"
    trial_dir.mkdir(parents=True, exist_ok=True)
    trial_json.write_text(json.dumps(trial, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    command = [
        sys.executable,
        "-I",
        str(TOOLS_DIR / "run_one_trial.py"),
        "--limit-mib",
        str(args.limit_mib),
        "--data-dir",
        str(data_dir),
        "--media-dir",
        str(media_dir),
        "--inputs-dir",
        str(inputs_dir),
        "--trial",
        str(trial_json),
        "--record",
        str(record_path),
    ]
    started = time.monotonic()
    timeout = args.trial_timeout
    try:
        completed = subprocess.run(
            command,
            timeout=timeout,
            capture_output=True,
        )
        stderr_tail = completed.stderr.decode("utf-8", "replace")[-900:]
        exit_code = completed.returncode
        stdout_tail = completed.stdout.decode("utf-8", "replace")[-400:]
    except subprocess.TimeoutExpired as exc:
        exit_code = -9
        stderr_tail = (exc.stderr or b"").decode("utf-8", "replace")[-900:]
        stdout_tail = ""
    elapsed = round(time.monotonic() - started, 3)

    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
    else:
        record = {
            "trial_id": trial["trial_id"],
            "family": trial["family"],
            "group": trial.get("group"),
            "pair": trial.get("pair"),
            "role": trial.get("role"),
            "label": trial.get("label", trial["trial_id"]),
            "intent": trial["intent"],
            "expected_class": trial["expected_class"],
            "material": trial["primary_asset_file"],
            "requested_operations": trial["operations"],
            "study_profile": trial.get("study_profile"),
            "completed": False,
            "phase_reached": "child_produced_no_record",
            "error": {
                "kind": "ChildExit" if exit_code != -9 else "Timeout",
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
                "stdout_tail": stdout_tail,
            },
            "incomplete_reason": "child_failure",
        }
    record.setdefault("trial_id", trial["trial_id"])
    record["child_exit_code"] = exit_code
    record["orchestrator_elapsed_seconds"] = elapsed
    if exit_code == -9:
        record.setdefault("incomplete_reason", "timeout")
    if not record.get("completed"):
        record.setdefault("incomplete_reason", "not_recorded")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", required=True)
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--results", required=True)
    parser.add_argument("--limit-mib", type=int, default=192)
    parser.add_argument("--trial-timeout", type=float, default=900.0)
    parser.add_argument("--only", default=None, help="comma separated trial ids or family names")
    args = parser.parse_args()

    trials = json.loads(Path(args.trials).read_text(encoding="utf-8"))
    if args.only:
        selections = {item.strip() for item in args.only.split(",") if item.strip()}
        trials = [
            item
            for item in trials
            if item["trial_id"] in selections or item["family"] in selections
        ]
        if not trials:
            raise SystemExit("selection matched no trials")

    media_dir = Path(args.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    results_path = Path(args.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    temp_root = Path(tempfile.mkdtemp(prefix="wav-order-runs-"))
    try:
        for index, trial in enumerate(trials, start=1):
            print(
                json.dumps(
                    {
                        "progress": f"{index}/{len(trials)}",
                        "trial_id": trial["trial_id"],
                        "family": trial["family"],
                    }
                ),
                flush=True,
            )
            record = run_one(trial, args, temp_root)
            results.append(record)
            results_path.write_text(
                json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            status = "completed" if record.get("completed") else f"incomplete:{record.get('incomplete_reason')}"
            print(
                json.dumps(
                    {
                        "trial_id": trial["trial_id"],
                        "status": status,
                        "rss_hwm_mib": record.get("rss_hwm_mib"),
                        "elapsed": record.get("orchestrator_elapsed_seconds"),
                    }
                ),
                flush=True,
            )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
