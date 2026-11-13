#!/usr/bin/env python3
"""Analyse captured WAV outputs: per-channel distortion, never L/R-averaged.

References are explicit rather than implied: a group's baseline trial is named
in the report, and every lossy variant is compared against it after aligning
gain by least squares, so a level difference is not mistaken for distortion.

Metrics are computed per channel and per trial; the report never averages the
two channels, so a one-sided loss cannot be hidden by the other channel.
"""
from __future__ import annotations

import argparse
import array
import json
import math
import sys
import wave
from pathlib import Path

TAU = 2.0 * math.pi
PCM_MAX = 32767
PCM_MIN = -32768


def read_channels(path: Path) -> tuple[int, list[list[int]]]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        rate = reader.getframerate()
        width = reader.getsampwidth()
        raw = reader.readframes(reader.getnframes())
    if width != 2:
        raise ValueError(f"{path}: expected 16-bit PCM")
    values = array.array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return rate, [values[index::channels].tolist() for index in range(channels)]


def clip_metrics(values: list[int]) -> dict:
    if not values:
        return {"frames": 0}
    peak = max(abs(v) for v in values)
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    railed = sum(1 for v in values if v >= PCM_MAX or v <= PCM_MIN)
    silent = sum(1 for v in values if v == 0)
    return {
        "frames": len(values),
        "peak": peak,
        "peak_fraction_of_full_scale": round(peak / PCM_MAX, 6),
        "rms": round(rms, 3),
        "rms_dbfs": round(20 * math.log10(rms / PCM_MAX), 3) if rms > 0 else None,
        "railed_samples": railed,
        "railed_fraction": round(railed / len(values), 6),
        "zero_samples": silent,
        "crest_factor": round(peak / rms, 4) if rms > 0 else None,
    }


def best_fit_scale(reference: list[int], candidate: list[int]) -> float:
    """Least-squares scale a such that a*reference ~= candidate."""
    denominator = sum(r * r for r in reference)
    if denominator == 0:
        return 1.0
    return sum(r * c for r, c in zip(reference, candidate)) / denominator


def compare_channels(reference: list[int], candidate: list[int]) -> dict:
    length = min(len(reference), len(candidate))
    if length == 0:
        return {"compared_frames": 0}
    scale = best_fit_scale(reference[:length], candidate[:length])
    aligned = [scale * value for value in reference[:length]]
    residuals = [candidate[i] - aligned[i] for i in range(length)]
    signal_energy = sum(value * value for value in aligned)
    error_energy = sum(value * value for value in residuals)
    snr = (
        round(10 * math.log10(signal_energy / error_energy), 3)
        if error_energy > 0 and signal_energy > 0
        else None
    )
    differing = sum(
        1
        for i in range(length)
        if candidate[i] != int(round(aligned[i]))
    )
    return {
        "compared_frames": length,
        "best_fit_scale": round(scale, 6),
        "differing_samples": differing,
        "differing_fraction": round(differing / length, 6),
        "max_abs_residual": round(max(abs(v) for v in residuals), 3),
        "rms_residual": round(math.sqrt(error_energy / length), 3),
        "snr_db": snr,
    }


def downmix(values: list[list[int]]) -> list[float]:
    """(L+R)/2 mono fold-down, the exact operation we must NOT let hide loss."""
    length = min(len(channel) for channel in values)
    return [
        (values[0][index] + values[1][index]) / 2.0
        for index in range(length)
    ]


def mono_trap(reference_channels: list[list[int]], candidate_channels: list[list[int]]) -> dict:
    """Show which per-channel loss a mono fold-down would conceal."""
    if len(reference_channels) < 2 or len(candidate_channels) < 2:
        return {"available": False}
    reference_mono = downmix(reference_channels)
    candidate_mono = downmix(candidate_channels)
    length = min(len(reference_mono), len(candidate_mono))
    scale = best_fit_scale(
        [int(round(v)) for v in reference_mono[:length]],
        [int(round(v)) for v in candidate_mono[:length]],
    )
    residuals = [
        candidate_mono[i] - scale * reference_mono[i] for i in range(length)
    ]
    signal_energy = sum((scale * reference_mono[i]) ** 2 for i in range(length))
    error_energy = sum(value ** 2 for value in residuals)
    mono_snr = (
        round(10 * math.log10(signal_energy / error_energy), 3)
        if error_energy > 0 and signal_energy > 0
        else None
    )
    return {
        "available": True,
        "mono_snr_db": mono_snr,
        "mono_max_abs_residual": round(max(abs(v) for v in residuals), 3),
        "note": "A mono fold-down averages the channels and can dilute a one-channel loss.",
    }


def goertzel_amplitude(values: list[int], rate: int, frequency: float) -> float:
    """Amplitude of one frequency via the Goertzel algorithm (no FFT)."""
    if not values:
        return 0.0
    n = len(values)
    omega = TAU * frequency / rate
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0
    for value in values:
        s = value + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    return math.sqrt(max(power, 0.0)) * 2.0 / n


def tone_metrics(values: list[int], rate: int, frequencies: list[float]) -> dict:
    return {
        f"{frequency:g}hz_amplitude": round(goertzel_amplitude(values, rate, frequency), 3)
        for frequency in frequencies
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--trace", default=None)
    args = parser.parse_args()

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    completed = [r for r in results if r.get("completed") and Path(r["media_path"]).is_file()]

    per_trial: dict[str, dict] = {}
    channel_data: dict[str, list[list[int]]] = {}
    rates: dict[str, int] = {}
    for record in completed:
        path = Path(record["media_path"])
        rate, channels = read_channels(path)
        rates[record["trial_id"]] = rate
        channel_data[record["trial_id"]] = channels
        per_trial[record["trial_id"]] = {
            "trial_id": record["trial_id"],
            "family": record["family"],
            "group": record.get("group"),
            "role": record.get("role"),
            "intent": record.get("intent"),
            "expected_class": record.get("expected_class"),
            "material": record.get("material"),
            "primary_asset_file": record.get("primary_asset_file"),
            "secondary_asset_file": record.get("secondary_asset_file"),
            "requested_operations": record.get("requested_operations"),
            "normalized_operations": record.get("normalized_operations"),
            "plan_digest": record.get("plan_digest"),
            "output_sha256": record.get("output_sha256"),
            "integrity_result": record.get("integrity_result"),
            "rss_hwm_mib": record.get("rss_hwm_mib"),
            "channels": [clip_metrics(values) for values in channels],
        }

    # Damage estimates are computed against the reference each trial declares.
    comparisons: list[dict] = []
    for record in completed:
        trial_id = record["trial_id"]
        reference = record.get("compare_to")
        if not reference or reference not in channel_data:
            continue
        ref_channels = channel_data[reference]
        candidate_channels = channel_data[trial_id]
        channel_comparisons = []
        for index in range(min(len(ref_channels), len(candidate_channels))):
            channel_comparisons.append(
                {
                    "channel": "left" if index == 0 else "right",
                    **compare_channels(ref_channels[index], candidate_channels[index]),
                    "reference_metrics": clip_metrics(ref_channels[index]),
                    "candidate_metrics": clip_metrics(candidate_channels[index]),
                }
            )
        comparisons.append(
            {
                "trial_id": trial_id,
                "group": record.get("group"),
                "reference_trial_id": reference,
                "family": record.get("family"),
                "role": record.get("role"),
                "intent": record.get("intent"),
                "expected_class": record.get("expected_class"),
                "channels": channel_comparisons,
                "mono_fold_down": mono_trap(ref_channels, candidate_channels),
            }
        )

    # Channel distinctness: prove the two channels carry different content,
    # so a single-channel reading cannot stand in for the other.
    distinctness: dict[str, dict] = {}
    for trial_id, channels in channel_data.items():
        if len(channels) < 2:
            continue
        length = min(len(channels[0]), len(channels[1]))
        left = channels[0][:length]
        right = channels[1][:length]
        differing = sum(1 for a, b in zip(left, right) if a != b)
        left_rms = math.sqrt(sum(v * v for v in left) / length) if length else 0.0
        right_rms = math.sqrt(sum(v * v for v in right) / length) if length else 0.0
        distinctness[trial_id] = {
            "compared_frames": length,
            "differing_samples": differing,
            "differing_fraction": round(differing / length, 6) if length else 0.0,
            "left_rms": round(left_rms, 3),
            "right_rms": round(right_rms, 3),
            "rms_ratio_left_over_right": (
                round(left_rms / right_rms, 4) if right_rms > 0 else None
            ),
        }

    # Attach step trace where available so the report can name the step that
    # created distortion rather than only the final outcome.
    step_trace = None
    if args.trace and Path(args.trace).is_file():
        traced = json.loads(Path(args.trace).read_text(encoding="utf-8"))
        step_trace = {item["trial_id"]: item for item in traced}

    payload = {
        "trials": per_trial,
        "comparisons": comparisons,
        "channel_distinctness": distinctness,
        "step_trace": step_trace,
        "rate": 8000,
        "incomplete": [
            {
                "trial_id": record["trial_id"],
                "family": record.get("family"),
                "phase_reached": record.get("phase_reached"),
                "incomplete_reason": record.get("incomplete_reason"),
                "rss_hwm_mib": record.get("rss_hwm_mib"),
                "primary_input_bytes": record.get("primary_input_bytes"),
                "filename": record.get("primary_asset_file"),
                "render_exceptions_observed": record.get("render_exceptions_observed"),
                "error": record.get("error"),
            }
            for record in results
            if not record.get("completed")
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "analysed_trials": len(per_trial),
                "comparisons": len(comparisons),
                "incomplete": len(payload["incomplete"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
