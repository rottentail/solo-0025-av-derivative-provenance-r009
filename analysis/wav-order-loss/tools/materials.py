#!/usr/bin/env python3
"""Deterministic PCM WAV materials for the processing-order study.

Design goals
------------
* "Same peak" is a property of the emitted bytes: every channel is rescaled
  so its largest sample equals the requested integer peak.
* Dense and sparse materials share duration, geometry, file peak and per
  channel peaks, but differ in energy distribution (RMS / crest / silence).
* Mix sources are strongly unequal: primary peak 30000, natural secondary
  6000, quiet secondary 600 (the quiet detail whose survival we measure).
* Near-upload-limit inputs are tiled from a one-second block so preparing
  them never allocates the whole file.

Materials
---------
dense_stereo_2s.wav           L: 440 Hz continuous at 32000, R: 1500 Hz pilot at 30
sparse_transient_stereo_2s    L: four 25 ms bursts/s at 32000, R: same pilot at 30
mix_strong_primary.wav        L 440 Hz / R 587 Hz, peak 30000
mix_weak_secondary.wav        L 880 Hz / R 1046 Hz, peak 6000
mix_quiet_secondary.wav       L 880 Hz / R 1046 Hz, peak 600
near_limit_dense.wav          tiled dense stereo, 25 000 000 bytes
near_limit_sparse.wav         tiled sparse stereo, 25 000 000 bytes
boundary_*_dense.wav          tiled dense stereo at 4/8/16/20 MB
"""
from __future__ import annotations

import argparse
import array
import json
import math
import wave
from pathlib import Path

TAU = 2.0 * math.pi
RATE = 8000
FILE_PEAK = 32000
PILOT_HZ = 1500.0
PILOT_PEAK = 30


def _round_sample(value: float) -> int:
    rounded = int(round(value))
    if rounded > 32767:
        return 32767
    if rounded < -32768:
        return -32768
    return rounded


def _rescale(values: list[float], target_peak: int) -> list[int]:
    largest = max((abs(v) for v in values), default=0.0)
    if largest == 0.0:
        return [0 for _ in values]
    scale = float(target_peak) / largest
    return [_round_sample(v * scale) for v in values]


def _program(kind: str, rate: int) -> list[int]:
    frames = rate
    if kind == "dense":
        raw = [math.sin(TAU * 440.0 * i / rate) for i in range(frames)]
    elif kind == "sparse":
        burst_starts = (
            int(rate * 0.10),
            int(rate * 0.35),
            int(rate * 0.60),
            int(rate * 0.85),
        )
        burst_frames = max(8, int(rate * 0.025))
        raw = [0.0] * frames
        for start in burst_starts:
            for offset in range(burst_frames):
                index = start + offset
                if index >= frames:
                    break
                window = math.sin(math.pi * (offset + 1) / (burst_frames + 1))
                raw[index] = window * math.sin(TAU * 440.0 * offset / rate)
    elif kind == "dynamics":
        # A quiet continuous tone with one short loud transient per second.
        # The 0.30-0.40 s window therefore contains quiet program content,
        # not silence, on the left channel.
        raw = [2000.0 / 32000.0 * math.sin(TAU * 440.0 * i / rate) for i in range(frames)]
        start = int(rate * 0.20)
        burst_frames = max(8, int(rate * 0.025))
        for offset in range(burst_frames):
            index = start + offset
            if index >= frames:
                break
            window = math.sin(math.pi * (offset + 1) / (burst_frames + 1))
            raw[index] += window * math.sin(TAU * 440.0 * offset / rate)
    else:
        raise ValueError(f"unknown program kind: {kind}")
    return _rescale(raw, FILE_PEAK)


def _pilot(rate: int) -> list[int]:
    raw = [math.sin(TAU * PILOT_HZ * i / rate) for i in range(rate)]
    return _rescale(raw, PILOT_PEAK)


def one_second_stereo(kind: str, rate: int = RATE) -> array.array:
    program = _program(kind, rate)
    pilot = _pilot(rate)
    block = array.array("h")
    for index in range(rate):
        block.append(program[index])
        block.append(pilot[index])
    return block


def write_stereo_block_material(path: Path, kind: str, seconds: int, rate: int = RATE) -> dict:
    block = one_second_stereo(kind, rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        raw = block.tobytes()
        writer.writeframes(raw * seconds)
    frames = rate * seconds
    return {
        "path": str(path),
        "format": "wav_pcm_s16le",
        "channels": 2,
        "rate": rate,
        "frames": frames,
        "seconds": round(frames / rate, 3),
        "size_bytes": path.stat().st_size,
        "stereo_layout": {
            "left": kind,
            "right": f"pilot_{PILOT_HZ:g}hz",
        },
        "channel_peaks": {"left": FILE_PEAK, "right": PILOT_PEAK},
        "file_peak": FILE_PEAK,
    }


def write_tiled_material(path: Path, kind: str, total_bytes: int, rate: int = RATE) -> dict:
    block = one_second_stereo(kind, rate)
    payload_budget = total_bytes - 44
    payload_budget -= payload_budget % 4
    total_frames = payload_budget // 4
    full_blocks, tail_frames = divmod(total_frames, rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw_block = block.tobytes()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        done = 0
        while done < full_blocks:
            count = min(64, full_blocks - done)
            writer.writeframes(raw_block * count)
            done += count
        if tail_frames:
            writer.writeframes(block[: tail_frames * 2].tobytes())
    size = path.stat().st_size
    if size != total_bytes:
        raise RuntimeError(f"{path}: expected {total_bytes} bytes, wrote {size}")
    return {
        "path": str(path),
        "format": "wav_pcm_s16le",
        "channels": 2,
        "rate": rate,
        "frames": total_frames,
        "seconds": round(total_frames / rate, 3),
        "size_bytes": size,
        "stereo_layout": {"left": kind, "right": f"pilot_{PILOT_HZ:g}hz"},
        "channel_peaks": {"left": FILE_PEAK, "right": PILOT_PEAK},
        "file_peak": FILE_PEAK,
    }


def write_mix_source(
    path: Path,
    *,
    left_hz: float,
    right_hz: float,
    peak: int,
    seconds: float = 2.0,
    rate: int = RATE,
) -> dict:
    frames = int(round(seconds * rate))
    left_raw = [math.sin(TAU * left_hz * i / rate) for i in range(frames)]
    right_raw = [math.sin(TAU * right_hz * i / rate) for i in range(frames)]
    left = _rescale(left_raw, peak)
    right = _rescale(right_raw, peak)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = array.array("h")
    for index in range(frames):
        values.append(left[index])
        values.append(right[index])
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(values.tobytes())
    return {
        "path": str(path),
        "format": "wav_pcm_s16le",
        "channels": 2,
        "rate": rate,
        "frames": frames,
        "seconds": round(frames / rate, 3),
        "size_bytes": path.stat().st_size,
        "channel_tones_hz": {"left": left_hz, "right": right_hz},
        "channel_peaks": {"left": peak, "right": peak},
        "file_peak": peak,
    }


def build_all(out_dir: Path, near_limit_bytes: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    manifest["dense"] = write_stereo_block_material(out_dir / "dense_stereo_2s.wav", "dense", 2)
    manifest["sparse"] = write_stereo_block_material(
        out_dir / "sparse_transient_stereo_2s.wav", "sparse", 2
    )
    manifest["dynamics"] = write_stereo_block_material(
        out_dir / "dynamics_stereo_2s.wav", "dynamics", 2
    )
    manifest["strong"] = write_mix_source(
        out_dir / "mix_strong_primary.wav", left_hz=440.0, right_hz=587.0, peak=30000
    )
    manifest["weak"] = write_mix_source(
        out_dir / "mix_weak_secondary.wav", left_hz=880.0, right_hz=1046.0, peak=6000
    )
    manifest["quiet"] = write_mix_source(
        out_dir / "mix_quiet_secondary.wav", left_hz=880.0, right_hz=1046.0, peak=600
    )
    manifest["near_dense"] = write_tiled_material(
        out_dir / "near_limit_dense.wav", "dense", near_limit_bytes
    )
    manifest["near_sparse"] = write_tiled_material(
        out_dir / "near_limit_sparse.wav", "sparse", near_limit_bytes
    )
    for megabytes in (1, 2, 3, 4, 8, 16, 20):
        key = f"boundary_{megabytes}mb"
        manifest[key] = write_tiled_material(
            out_dir / f"boundary_{megabytes}mb_dense.wav",
            "dense",
            megabytes * 1_000_000,
        )
    (out_dir / "materials.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--near-limit-bytes", type=int, default=25_000_000)
    args = parser.parse_args()
    manifest = build_all(Path(args.out_dir), args.near_limit_bytes)
    summary = {
        key: {
            "path": value["path"],
            "size_bytes": value["size_bytes"],
            "seconds": value["seconds"],
            "file_peak": value["file_peak"],
        }
        for key, value in manifest.items()
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
