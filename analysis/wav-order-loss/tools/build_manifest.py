#!/usr/bin/env python3
"""Emit a self-contained manifest that pairs every artefact with its recipe.

The study expects that, after a partial failure and a re-organisation of the
files, captured media can still be matched to the recipe that really produced
it.  This tool rebuilds that pairing from two independent sources and verifies
they agree:

  1. the per-media recipe sidecar written by the trial process at capture time;
  2. the raw trial record (rendition id, plan digest, output digest).

For every captured WAV it checks:
  * the file's SHA-256 equals the record's output digest;
  * the sidecar's plan digest equals the record's plan digest;
  * the sidecar's normalised operations equal the record's.
Incomplete trials are listed with the phase they reached and the memory-cause
evidence, so they remain visible instead of disappearing from the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    media_dir = Path(args.media_dir)

    entries = []
    mismatches = []
    for record in results:
        trial_id = record["trial_id"]
        entry = {
            "trial_id": trial_id,
            "family": record.get("family"),
            "group": record.get("group"),
            "role": record.get("role"),
            "material": record.get("material"),
            "primary_asset_file": record.get("primary_asset_file"),
            "secondary_asset_file": record.get("secondary_asset_file"),
            "requested_operations": record.get("requested_operations"),
            "normalized_operations": record.get("normalized_operations"),
            "plan_digest": record.get("plan_digest"),
            "rendition_id": record.get("rendition_id"),
            "output_asset_id": record.get("output_asset_id"),
            "output_sha256": record.get("output_sha256"),
            "integrity_result": record.get("integrity_result"),
            "rss_hwm_mib": record.get("rss_hwm_mib"),
            "completed": bool(record.get("completed")),
        }
        if not record.get("completed"):
            entry["incomplete"] = {
                "phase_reached": record.get("phase_reached"),
                "incomplete_reason": record.get("incomplete_reason"),
                "primary_input_bytes": record.get("primary_input_bytes"),
                "render_exceptions_observed": record.get("render_exceptions_observed"),
                "child_exit_code": record.get("child_exit_code"),
            }
            entries.append(entry)
            continue

        media_path = media_dir / f"{trial_id}.wav"
        sidecar_path = media_dir / f"{trial_id}.recipe.json"
        entry["media_file"] = media_path.name
        entry["sidecar_file"] = sidecar_path.name
        entry["media_exists"] = media_path.is_file()
        entry["sidecar_exists"] = sidecar_path.is_file()

        if media_path.is_file():
            actual = sha256_file(media_path)
            entry["media_sha256_actual"] = actual
            if actual != record.get("output_sha256"):
                mismatches.append({"trial_id": trial_id, "reason": "media digest differs from record"})
                entry["media_matches_record"] = False
            else:
                entry["media_matches_record"] = True
        else:
            mismatches.append({"trial_id": trial_id, "reason": "media file missing"})
            entry["media_matches_record"] = False

        if sidecar_path.is_file():
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            entry["sidecar"] = sidecar
            same_plan = sidecar.get("plan_digest") == record.get("plan_digest")
            same_ops = sidecar.get("normalized_operations") == record.get("normalized_operations")
            same_sha = sidecar.get("media_sha256") == record.get("output_sha256")
            same_rendition = sidecar.get("rendition_id") == record.get("rendition_id")
            entry["sidecar_matches_record"] = {
                "plan_digest": same_plan,
                "normalized_operations": same_ops,
                "media_sha256": same_sha,
                "rendition_id": same_rendition,
            }
            if not (same_plan and same_ops and same_sha and same_rendition):
                mismatches.append({"trial_id": trial_id, "reason": "sidecar disagrees with record"})
        else:
            mismatches.append({"trial_id": trial_id, "reason": "sidecar missing"})
        entries.append(entry)

    manifest = {
        "schema": "wav-order-loss/manifest-v1",
        "note": (
            "Each completed entry pairs a captured media file with the recipe it "
            "actually used, reconstructed from the capture-time sidecar and the "
            "raw trial record. Incomplete entries are retained with their phase."
        ),
        "media_dir": str(media_dir),
        "completed_count": sum(1 for e in entries if e["completed"]),
        "incomplete_count": sum(1 for e in entries if not e["completed"]),
        "mismatches": mismatches,
        "entries": entries,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "completed": manifest["completed_count"],
                "incomplete": manifest["incomplete_count"],
                "mismatches": len(mismatches),
            }
        )
    )
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
