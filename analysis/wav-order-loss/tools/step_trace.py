#!/usr/bin/env python3
"""Stepwise trace of the same legal plans, using the service's own operations.

This does not replace the HTTP trials.  It answers a question the final bytes
cannot: at which step clipped samples were created and whether a later
normalize merely rescaled already-flattened peaks.  It calls the exact
functions the service uses (trim_audio, gain_audio, normalize_audio,
mix_audio) and never re-implements clipping.

Run in its own process, outside the 192 MiB per-trial budget, because this
trace intentionally holds intermediate clips for small inputs only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path("/workspace")
sys.path.insert(0, str(PROJECT_ROOT))

from avprovenance.media.audio_ops import (  # noqa: E402
    gain_audio,
    mix_audio,
    normalize_audio,
    trim_audio,
)
from avprovenance.media.wav import decode_wav  # noqa: E402

PCM_MAX = 32767
PCM_MIN = -32768


def channel_stats(clip) -> list[dict]:
    stats = []
    for channel in clip.channels:
        peak = max((abs(v) for v in channel), default=0)
        railed = sum(1 for v in channel if v >= PCM_MAX or v <= PCM_MIN)
        zeroes = sum(1 for v in channel if v == 0)
        stats.append(
            {
                "peak": peak,
                "railed_samples": railed,
                "zero_samples": zeroes,
                "frames": len(channel),
            }
        )
    return stats


def apply_one(clip, operation, secondary):
    kind = operation["type"]
    if kind == "media_trim":
        return trim_audio(clip, operation)
    if kind == "audio_gain":
        return gain_audio(clip, operation)
    if kind == "audio_normalize":
        return normalize_audio(clip, operation)
    if kind == "audio_mix":
        if secondary is None:
            raise RuntimeError("audio_mix has no secondary")
        return mix_audio(clip, secondary, operation)
    raise RuntimeError(f"unsupported operation: {kind}")


def trace_trial(trial, inputs_dir: Path) -> dict:
    primary_path = inputs_dir / trial["primary_asset_file"]
    clip = decode_wav(primary_path.read_bytes())
    secondary = None
    if trial.get("secondary_asset_file"):
        secondary = decode_wav((inputs_dir / trial["secondary_asset_file"]).read_bytes())

    steps = [
        {
            "step": 0,
            "operation": "source",
            "channels": channel_stats(clip),
        }
    ]
    cumulative_railed = [0, 0]
    for index, operation in enumerate(trial["operations"], start=1):
        clip = apply_one(clip, operation, secondary)
        stats = channel_stats(clip)
        for channel_index, stat in enumerate(stats):
            cumulative_railed[channel_index] = cumulative_railed[channel_index]
        steps.append(
            {
                "step": index,
                "operation": operation,
                "channels": stats,
            }
        )

    # Determine whether any step created new full-scale samples.  A normalize
    # rescales to the target, so a later step can hide earlier clipping in the
    # final peak; the per-step railed counts show it instead.
    clipping_steps = [
        step
        for step in steps
        if step["step"] > 0
        and any(channel["railed_samples"] > 0 for channel in step["channels"])
    ]
    return {
        "trial_id": trial["trial_id"],
        "family": trial["family"],
        "group": trial.get("group"),
        "operations": trial["operations"],
        "steps": steps,
        "clipping_steps": [step["step"] for step in clipping_steps],
        "clipped_at_any_step": bool(clipping_steps),
        "final_channels": steps[-1]["channels"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", required=True)
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--families", default=None)
    args = parser.parse_args()

    trials = json.loads(Path(args.trials).read_text(encoding="utf-8"))
    if args.families:
        keep = {item.strip() for item in args.families.split(",")}
        trials = [item for item in trials if item["family"] in keep]

    traces = []
    for trial in trials:
        if trial["family"] in {"budget", "near_limit"}:
            continue  # intermediates do not fit in the trace budget either
        traces.append(trace_trial(trial, Path(args.inputs_dir)))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(traces, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"traced": len(traces)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
