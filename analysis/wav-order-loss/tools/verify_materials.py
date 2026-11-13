#!/usr/bin/env python3
"""Verify the generated materials from their actual bytes.

Answers three report claims with measurements rather than generator intent:
  * dense and sparse stereo materials share the same file peak;
  * they differ in energy distribution (RMS, crest factor, silence fraction);
  * the two near-limit inputs are each close to the service upload cap.
"""
from __future__ import annotations

import argparse
import array
import json
import math
import sys
import wave
from pathlib import Path

MAX_MEDIA_BYTES = 24 * 1024 * 1024  # matches avprovenance.validation

PCM_MAX = 32767


def describe(path: Path, sample_limit: int = 400000) -> dict:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        rate = reader.getframerate()
        frames = reader.getnframes()
        raw = reader.readframes(min(frames, sample_limit))
    values = array.array("h")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    per_channel = [values[index::channels].tolist() for index in range(channels)]
    stats = []
    for channel in per_channel:
        if not channel:
            stats.append({})
            continue
        peak = max(abs(v) for v in channel)
        rms = math.sqrt(sum(v * v for v in channel) / len(channel))
        silence = sum(1 for v in channel if v == 0)
        stats.append(
            {
                "peak": peak,
                "peak_fraction_of_full_scale": round(peak / PCM_MAX, 5),
                "rms": round(rms, 2),
                "crest_factor": round(peak / rms, 3) if rms > 0 else None,
                "silent_fraction": round(silence / len(channel), 4),
            }
        )
    return {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "channels": channels,
        "rate": rate,
        "frames": frames,
        "seconds": round(frames / rate, 3),
        "analysed_frames": len(per_channel[0]) if per_channel else 0,
        "file_peak": max(abs(v) for v in values) if values else 0,
        "channels_detail": stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--near-limit-bytes", type=int, default=25_000_000)
    args = parser.parse_args()

    inputs = Path(args.inputs_dir)
    payload = {
        "upload_cap_bytes": MAX_MEDIA_BYTES,
        "near_limit_bytes": args.near_limit_bytes,
        "near_limit_fraction_of_cap": round(args.near_limit_bytes / MAX_MEDIA_BYTES, 4),
        "materials": {},
    }
    for key, name in (
        ("dense", "dense_stereo_2s.wav"),
        ("sparse", "sparse_transient_stereo_2s.wav"),
        ("dynamics", "dynamics_stereo_2s.wav"),
        ("strong", "mix_strong_primary.wav"),
        ("weak", "mix_weak_secondary.wav"),
        ("quiet", "mix_quiet_secondary.wav"),
        ("near_dense", "near_limit_dense.wav"),
        ("near_sparse", "near_limit_sparse.wav"),
    ):
        path = inputs / name
        if path.is_file():
            payload["materials"][key] = describe(path)

    dense = payload["materials"].get("dense")
    sparse = payload["materials"].get("sparse")
    if dense and sparse:
        payload["equal_peak_check"] = {
            "dense_file_peak": dense["file_peak"],
            "sparse_file_peak": sparse["file_peak"],
            "equal": dense["file_peak"] == sparse["file_peak"],
            "dense_crest_factor_left": dense["channels_detail"][0]["crest_factor"],
            "sparse_crest_factor_left": sparse["channels_detail"][0]["crest_factor"],
            "dense_rms_left": dense["channels_detail"][0]["rms"],
            "sparse_rms_left": sparse["channels_detail"][0]["rms"],
            "dense_silent_fraction_left": dense["channels_detail"][0]["silent_fraction"],
            "sparse_silent_fraction_left": sparse["channels_detail"][0]["silent_fraction"],
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    check = payload.get("equal_peak_check", {})
    print(json.dumps({
        "equal_peak": check.get("equal"),
        "dense_crest": check.get("dense_crest_factor_left"),
        "sparse_crest": check.get("sparse_crest_factor_left"),
        "near_limit_fraction": payload["near_limit_fraction_of_cap"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
