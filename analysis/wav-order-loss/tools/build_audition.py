#!/usr/bin/env python3
"""Build audition comparisons from the real captured service outputs.

Each audition set pairs a reference output and a lossy output that share the
same final peak, plus a mono fold-down of the lossy file and a small
difference excerpt, so the loss can be heard and seen.  No audio is
regenerated: every WAV here is a copy or a mono fold-down of a file the
service actually produced.
"""
from __future__ import annotations

import argparse
import array
import json
import shutil
import wave
from pathlib import Path

PCM_MAX = 32767


def read_wav(path: Path) -> tuple[int, list[list[int]]]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    values = array.array("h")
    values.frombytes(raw)
    import sys

    if sys.byteorder != "little":
        values.byteswap()
    return rate, [values[index::channels].tolist() for index in range(channels)]


def write_wav(path: Path, rate: int, channels: list[list[int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = array.array("h")
    length = min(len(channel) for channel in channels)
    for index in range(length):
        for channel in channels:
            values.append(channel[index])
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(len(channels))
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(values.tobytes())


def mono_downmix(channels: list[list[int]]) -> list[int]:
    length = min(len(channel) for channel in channels)
    return [
        int(round((channels[0][index] + channels[1][index]) / 2.0))
        for index in range(length)
    ]


def difference(reference: list[int], candidate: list[int], gain: int = 24) -> list[int]:
    length = min(len(reference), len(candidate))
    out = []
    factor = 2 ** gain
    for index in range(length):
        delta = candidate[index] - reference[index]
        scaled = delta * factor
        out.append(max(-32768, min(32767, int(round(scaled)))))
    return out


AUDITION_SETS = [
    {
        "set_id": "F1_dense_clip_vs_reference",
        "title": "F1 dense: same final peak, clipped vs clean",
        "reference": "f1_dense_norm_only",
        "lossy": "f1_dense_clip_early",
        "channel": 0,
        "what_to_listen_for": "The clipped take flattens the tone into a square-like buzz while its peak is still 16384.",
    },
    {
        "set_id": "F1_sparse_early_vs_late_clip",
        "title": "F1 sparse: transient clipping before vs after normalize",
        "reference": "f1_sparse_atten_then_norm",
        "lossy": "f1_sparse_clip_late",
        "channel": 0,
        "what_to_listen_for": "Both are deviations from the clean atten-then-norm reference; the later-clipped take has smeared transients.",
    },
    {
        "set_id": "F2_quiet_channel_annihilated",
        "title": "F2 dense: quiet right-channel pilot deleted while left survives",
        "reference": "f2_dense_norm_only",
        "lossy": "f2_dense_quiet_annihilated",
        "channel": 1,
        "what_to_listen_for": "The right channel goes to digital silence; a mono fold-down of the pair does not reveal it.",
    },
    {
        "set_id": "F3_mix_clip_vs_norm_first",
        "title": "F3 weak mix: mix-first clip vs normalize-first reference",
        "reference": "f3_weak_norm_first",
        "lossy": "f3_weak_mix_first",
        "channel": 1,
        "what_to_listen_for": "Both end at peak 16384 on the right channel; the mix-first take is still 15.8 dB SNR, so equal peak does not mean equal sound.",
    },
    {
        "set_id": "F4_trim_order_level",
        "title": "F4 dynamics: normalize-then-trim captures 16.7x less level",
        "reference": "f4_trim_then_norm",
        "lossy": "f4_norm_then_trim",
        "channel": 0,
        "what_to_listen_for": "The same window ends at peak 16384 or 978 depending only on operation order.",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    results = {r["trial_id"]: r for r in json.loads(Path(args.results).read_text()) if r.get("completed")}
    analysis = json.loads(Path(args.analysis).read_text())
    comparison_by_trial = {c["trial_id"]: c for c in analysis["comparisons"]}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    index = []
    for spec in AUDITION_SETS:
        reference_id = spec["reference"]
        lossy_id = spec["lossy"]
        if reference_id not in results or lossy_id not in results:
            index.append({**spec, "available": False, "reason": "trial not completed"})
            continue

        ref_path = Path(results[reference_id]["media_path"])
        lossy_path = Path(results[lossy_id]["media_path"])
        ref_rate, ref_channels = read_wav(ref_path)
        lossy_rate, lossy_channels = read_wav(lossy_path)
        if ref_rate != lossy_rate:
            index.append({**spec, "available": False, "reason": "rate mismatch"})
            continue

        channel_index = spec["channel"]
        set_dir = out_dir / spec["set_id"]
        set_dir.mkdir(parents=True, exist_ok=True)

        # Copy the real service outputs unchanged for reference/lossy listening.
        shutil.copyfile(ref_path, set_dir / "reference.wav")
        shutil.copyfile(lossy_path, set_dir / "lossy.wav")

        # Isolate the channel of interest as a mono file so it can be heard
        # without the other channel masking it.
        write_wav(set_dir / f"reference_channel{channel_index}.wav", ref_rate, [ref_channels[channel_index]])
        write_wav(set_dir / f"lossy_channel{channel_index}.wav", lossy_rate, [lossy_channels[channel_index]])

        # Mono fold-down of the lossy output, to demonstrate the averaging trap.
        write_wav(set_dir / "lossy_mono_folddown.wav", lossy_rate, [mono_downmix(lossy_channels)])

        # Difference on the channel of interest, amplified 24 dB, self-describing.
        ref_channel = ref_channels[channel_index]
        lossy_channel = lossy_channels[channel_index]
        write_wav(set_dir / "difference_plus24dB.wav", ref_rate, [difference(ref_channel, lossy_channel)])

        comparison = comparison_by_trial.get(lossy_id, {})
        channel_stats = None
        for item in comparison.get("channels", []):
            if item["channel"] == ("left" if channel_index == 0 else "right"):
                channel_stats = item
        index.append(
            {
                "set_id": spec["set_id"],
                "title": spec["title"],
                "available": True,
                "reference_trial": reference_id,
                "lossy_trial": lossy_id,
                "channel": "left" if channel_index == 0 else "right",
                "reference_operations": results[reference_id]["normalized_operations"],
                "lossy_operations": results[lossy_id]["normalized_operations"],
                "reference_sha256": results[reference_id]["output_sha256"],
                "lossy_sha256": results[lossy_id]["output_sha256"],
                "what_to_listen_for": spec["what_to_listen_for"],
                "measured": {
                    "snr_db": channel_stats["snr_db"] if channel_stats else None,
                    "reference_peak": channel_stats["reference_metrics"]["peak"] if channel_stats else None,
                    "lossy_peak": channel_stats["candidate_metrics"]["peak"] if channel_stats else None,
                },
                "files": {
                    "reference": "reference.wav",
                    "lossy": "lossy.wav",
                    f"reference_channel{channel_index}": f"reference_channel{channel_index}.wav",
                    f"lossy_channel{channel_index}": f"lossy_channel{channel_index}.wav",
                    "lossy_mono_folddown": "lossy_mono_folddown.wav",
                    "difference_plus24dB": "difference_plus24dB.wav",
                },
            }
        )

    (out_dir / "audition_index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"sets": len(index), "available": sum(1 for s in index if s.get("available"))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
