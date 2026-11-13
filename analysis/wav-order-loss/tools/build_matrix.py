#!/usr/bin/env python3
"""Build the trial matrix: representative legal orderings with declared references.

Every plan is accepted unchanged by the service's plan compiler (verified
separately).  Each lossy trial names an explicit reference trial that ends at
the same target peak, so peak equality is measured rather than assumed.

Family map
----------
F1 gain_order   : gain/clip/normalize order on dense and sparse stereo material
F2 quantization : gain round-trips that squeeze a quiet channel toward 0 LSB
F3 mix_order    : two-source mix before/after level and gain staging
F4 trim_order   : trim vs normalize order (level-order contrast)
F5 budget       : identical plan across input sizes 1..20 MB (budget ladder)
F6 near_limit   : inputs at ~99% of the decoded upload cap
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DENSE = "dense_stereo_2s.wav"
SPARSE = "sparse_transient_stereo_2s.wav"
DYNAMICS = "dynamics_stereo_2s.wav"
STRONG = "mix_strong_primary.wav"
WEAK = "mix_weak_secondary.wav"
QUIET = "mix_quiet_secondary.wav"


def gain(db: float) -> dict:
    return {"type": "audio_gain", "db": db}


def norm(target: float = 0.5) -> dict:
    return {"type": "audio_normalize", "target_peak": target}


def trim(start: float, duration: float) -> dict:
    return {"type": "media_trim", "start_seconds": start, "duration_seconds": duration}


def mix(secondary_gain: float = 0.0, pw: float = 1.0, sw: float = 1.0) -> dict:
    return {
        "type": "audio_mix",
        "secondary_gain": secondary_gain,
        "primary_weight": pw,
        "secondary_weight": sw,
    }


def trial(
    trial_id: str,
    *,
    family: str,
    group: str,
    role: str,
    intent: str,
    expected_class: str,
    primary: str,
    operations: list[dict],
    secondary: str | None = None,
    compare_to: str | None = None,
) -> dict:
    return {
        "trial_id": trial_id,
        "family": family,
        "group": group,
        "role": role,
        "intent": intent,
        "expected_class": expected_class,
        "primary_asset_file": primary,
        "secondary_asset_file": secondary,
        "compare_to": compare_to,
        "output_name": f"{trial_id}.wav",
        "operations": operations,
    }


def build() -> list[dict]:
    trials: list[dict] = []

    # ------------------------------------------------------------------
    # F1: gain / clipping / normalize order.
    # For a source whose peak is 32000, normalize to 0.5 multiplies by
    # 16383.5/peak.  All five plans below end at peak 16384.
    # ------------------------------------------------------------------
    for material, label in ((DENSE, "dense"), (SPARSE, "sparse")):
        reference = f"f1_{label}_norm_only"
        group = f"gain_order_{label}"
        trials.append(trial(
            reference, family="gain_order", group=group, role="A_reference",
            intent="Single normalize to 0.5: the clean reference at the final peak.",
            expected_class="reference_clean", primary=material,
            operations=[norm(0.5)],
        ))
        trials.append(trial(
            f"f1_{label}_atten_then_norm", family="gain_order", group=group,
            role="A_reference", intent="Attenuate 6 dB, then normalize: never clips.",
            expected_class="reference_no_clip", primary=material,
            operations=[gain(-6), norm(0.5)], compare_to=reference,
        ))
        trials.append(trial(
            f"f1_{label}_clip_early", family="gain_order", group=group,
            role="B_lossy", intent="Boost 6 dB first: 32000 peaks become ~63840 and clip hard, then normalize.",
            expected_class="clipping_early", primary=material,
            operations=[gain(6), norm(0.5)], compare_to=reference,
        ))
        trials.append(trial(
            f"f1_{label}_clip_late", family="gain_order", group=group,
            role="B_lossy", intent="Normalize, boost 12 dB so the chain clips, then normalize again; flat tops persist.",
            expected_class="clipping_late", primary=material,
            operations=[norm(0.5), gain(12), norm(0.5)], compare_to=reference,
        ))
        trials.append(trial(
            f"f1_{label}_net_zero", family="gain_order", group=group,
            role="B_lossy", intent="-6 then +6 dB: level restored but every sample rounded twice.",
            expected_class="quantization_roundtrip", primary=material,
            operations=[gain(-6), gain(6), norm(0.5)], compare_to=reference,
        ))

    # ------------------------------------------------------------------
    # F2: quantisation depth on the dense stereo material.  The right
    # channel is a 1500 Hz pilot at peak 30 while the left is at 32000.
    # ------------------------------------------------------------------
    reference = "f2_dense_norm_only"
    group = "quant_dense"
    trials.append(trial(
        reference, family="quantization", group=group, role="A_reference",
        intent="Single normalize to 0.5: both channels keep their detail.",
        expected_class="reference_clean", primary=DENSE, operations=[norm(0.5)],
    ))
    trials.append(trial(
        "f2_dense_quiet_2lsb", family="quantization", group=group, role="B_lossy",
        intent="-24 then +24 dB: the quiet right pilot is reduced to ~2 LSB before being restored.",
        expected_class="quiet_detail_coarse", primary=DENSE,
        operations=[gain(-24), gain(24), norm(0.5)], compare_to=reference,
    ))
    trials.append(trial(
        "f2_dense_quiet_annihilated", family="quantization", group=group, role="B_lossy",
        intent="-48, -24, +24, +24, +24 dB (net 0): the quiet right pilot rounds to zero and stays zero.",
        expected_class="quiet_detail_annihilated", primary=DENSE,
        operations=[gain(-48), gain(-24), gain(24), gain(24), gain(24), norm(0.5)],
        compare_to=reference,
    ))

    # ------------------------------------------------------------------
    # F3: two-source mix order with unequal source levels.
    # strong primary peak 30000, secondary 6000 (weak) or 600 (quiet).
    # ------------------------------------------------------------------
    group = "mix_weak"
    trials.append(trial(
        "f3_weak_norm_first", family="mix_order", group=group, role="A_reference",
        intent="Normalize the primary, then mix the secondary, then normalize again.",
        expected_class="reference_no_clip", primary=STRONG, secondary=WEAK,
        operations=[norm(0.5), mix(0.0, 1.0, 1.0), norm(0.5)],
    ))
    trials.append(trial(
        "f3_weak_mix_first", family="mix_order", group=group, role="B_lossy",
        intent="Mix first: 30000 + 6000 exceeds full scale and clips before normalizing.",
        expected_class="mix_clipping", primary=STRONG, secondary=WEAK,
        operations=[mix(0.0, 1.0, 1.0), norm(0.5)], compare_to="f3_weak_norm_first",
    ))
    trials.append(trial(
        "f3_weak_balanced", family="mix_order", group=group, role="A_reference",
        intent="Attenuate primary and boost secondary before mixing: balanced, no clipping.",
        expected_class="reference_balanced", primary=STRONG, secondary=WEAK,
        operations=[gain(-6), mix(6.0, 1.0, 1.0), norm(0.5)],
    ))
    trials.append(trial(
        "f3_weak_gain_up", family="mix_order", group=group, role="B_lossy",
        intent="Boost the primary before mixing: primary peaks clip before the sum is normalized.",
        expected_class="mix_clipping", primary=STRONG, secondary=WEAK,
        operations=[gain(6), mix(0.0, 1.0, 1.0), norm(0.5)], compare_to="f3_weak_balanced",
    ))

    group = "mix_quiet"
    trials.append(trial(
        "f3_quiet_norm_first", family="mix_order", group=group, role="A_reference",
        intent="Normalize the primary, then mix the quiet 600-peak secondary.",
        expected_class="reference_no_clip", primary=STRONG, secondary=QUIET,
        operations=[norm(0.5), mix(0.0, 1.0, 1.0), norm(0.5)],
    ))
    trials.append(trial(
        "f3_quiet_mix_first", family="mix_order", group=group, role="B_lossy",
        intent="Mix first with the quiet secondary; the sum stays below full scale, so no clipping here.",
        expected_class="reference_no_clip", primary=STRONG, secondary=QUIET,
        operations=[mix(0.0, 1.0, 1.0), norm(0.5)], compare_to="f3_quiet_norm_first",
    ))
    trials.append(trial(
        "f3_quiet_boost_secondary", family="mix_order", group=group, role="B_lossy",
        intent="Boost the primary 6 dB before mixing, so 30000 peaks clip before the quiet sum is normalized.",
        expected_class="mix_clipping", primary=STRONG, secondary=QUIET,
        operations=[gain(6), mix(0.0, 1.0, 1.0), norm(0.5)], compare_to="f3_quiet_norm_first",
    ))

    # ------------------------------------------------------------------
    # F4: trim vs normalize order, an explicit level-order contrast.
    # The dynamics material is a quiet 440 Hz tone at ~1910 amplitude with
    # one loud transient per second at file peak 32000.  The 0.45-0.55 s
    # window contains quiet program content, not silence.
    # ------------------------------------------------------------------
    group = "trim_order"
    trials.append(trial(
        "f4_trim_then_norm", family="trim_order", group=group, role="A_reference",
        intent="Trim to the quiet 0.45-0.55 s window first, then normalize that window to 0.5.",
        expected_class="level_order_effect", primary=DYNAMICS,
        operations=[trim(0.45, 0.10), norm(0.5)],
    ))
    trials.append(trial(
        "f4_norm_then_trim", family="trim_order", group=group, role="B_contrast",
        intent="Normalize against the full-file transient peak, then trim to the same quiet window.",
        expected_class="level_order_effect", primary=DYNAMICS,
        operations=[norm(0.5), trim(0.45, 0.10)], compare_to="f4_trim_then_norm",
    ))

    # ------------------------------------------------------------------
    # F5: budget ladder, identical legal plan across input sizes.
    # ------------------------------------------------------------------
    for megabytes in (1, 2, 3, 4, 8, 16, 20):
        trials.append(trial(
            f"f5_budget_{megabytes}mb", family="budget", group="budget_ladder",
            role="probe", intent=f"Reference order on a {megabytes} MB two-channel input.",
            expected_class="budget_probe", primary=f"boundary_{megabytes}mb_dense.wav",
            operations=[gain(-6), norm(0.5)],
        ))

    # ------------------------------------------------------------------
    # F6: inputs at ~99% of the decoded upload cap (25 000 000 bytes).
    # ------------------------------------------------------------------
    for material, label in (("near_limit_dense.wav", "dense"),
                            ("near_limit_sparse.wav", "sparse")):
        for variant, operations in (
            ("reference", [gain(-6), norm(0.5)]),
            ("clip", [gain(6), norm(0.5)]),
        ):
            trials.append(trial(
                f"f6_near_{label}_{variant}", family="near_limit",
                group=f"near_limit_{label}", role="probe",
                intent=f"{variant} order on a near-upload-limit {label} input.",
                expected_class="near_limit_budget", primary=material,
                operations=operations,
            ))

    return trials


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    trials = build()
    ids = {item["trial_id"] for item in trials}
    if len(ids) != len(trials):
        raise SystemExit("duplicate trial ids")
    for item in trials:
        target = item.get("compare_to")
        if target and target not in ids:
            raise SystemExit(f"{item['trial_id']} compares to missing {target}")
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trials, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    families: dict[str, int] = {}
    for item in trials:
        families[item["family"]] = families.get(item["family"], 0) + 1
    print(json.dumps({"trials": len(trials), "families": families}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
