#!/usr/bin/env python3
"""Shared trial execution logic: drives the real HTTP service for one trial.

This module holds no memory limit itself; the child process that imports it
applies RLIMIT_AS before starting the server.
"""
from __future__ import annotations

import base64
import gc
import http.client
import json
import shutil
import time
import traceback
from pathlib import Path
from typing import Any


def hwm_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024.0, 2)
    except OSError:
        pass
    return -1.0


def vm_peak_mib() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmPeak:"):
                return round(int(line.split()[1]) / 1024.0, 2)
    except OSError:
        pass
    return -1.0


# Observation-only ledger.  The service converts MemoryError into a generic
# RENDER_FAILED response, which would hide *why* a trial did not complete.
# Wrappers installed by the child record the exception type that escaped the
# render call, then re-raise it unchanged.  They do not alter behaviour.
RENDER_EXCEPTIONS: list[dict[str, Any]] = []


def record_render_exception(stage: str, exc: BaseException) -> None:
    # Must not itself raise while the process is out of memory.
    try:
        RENDER_EXCEPTIONS.append(
            {
                "stage": stage,
                "type": type(exc).__name__,
                "detail": str(exc)[:300],
            }
        )
    except BaseException:  # noqa: BLE001 - observation must never mask the cause
        pass


def upload_streamed(host: str, port: int, filename: str, path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    prefix = ('{"filename":' + json.dumps(filename) + ',"data_base64":"').encode()
    body_len = len(prefix) + 4 * ((size + 2) // 3) + 2
    conn = http.client.HTTPConnection(host, port, timeout=600.0)
    try:
        conn.putrequest("POST", "/v1/assets")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(body_len))
        conn.endheaders()
        conn.send(prefix)
        remaining = size
        with path.open("rb") as handle:
            while remaining:
                take = min(196608, remaining)
                take -= take % 3
                if take == 0:
                    take = remaining
                raw = handle.read(take)
                if not raw:
                    break
                conn.send(base64.b64encode(raw))
                remaining -= len(raw)
        conn.send(b'"}')
        response = conn.getresponse()
        raw = response.read()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"raw": raw[:200].decode("utf-8", "replace")}
        return {"status": response.status, "body": body}
    finally:
        conn.close()


def request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {"raw": raw[:200].decode("utf-8", "replace")}
        return {"status": response.status, "body": parsed}
    finally:
        conn.close()


def execute_trial(
    *,
    host: str,
    port: int,
    data_dir: Path,
    media_dir: Path,
    trial: dict[str, Any],
    inputs_dir: Path,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "trial_id": trial["trial_id"],
        "family": trial["family"],
        "group": trial.get("group"),
        "pair": trial.get("pair"),
        "role": trial.get("role"),
        "label": trial.get("label", trial["trial_id"]),
        "intent": trial["intent"],
        "expected_class": trial["expected_class"],
        "material": trial.get("material", trial["primary_asset_file"]),
        "requested_operations": trial["operations"],
        "compare_to": trial.get("compare_to"),
        "secondary_asset_file": trial.get("secondary_asset_file"),
        "primary_asset_file": trial["primary_asset_file"],
        "study_profile": trial.get("study_profile"),
        "phase_reached": "started",
        "completed": False,
        "started_at": time.time(),
    }
    started = time.monotonic()
    try:
        primary_file = inputs_dir / trial["primary_asset_file"]
        record["primary_input_bytes"] = primary_file.stat().st_size
        up = upload_streamed(host, port, trial["primary_asset_file"], primary_file)
        record["primary_upload_status"] = up["status"]
        if up["status"] != 201:
            record["phase_reached"] = "upload_primary"
            record["error"] = {"kind": "http", "status": up["status"], "body": up["body"]}
            record["rss_hwm_mib"] = hwm_mib()
            return record
        record["primary_asset_id"] = up["body"]["id"]
        record["primary_asset_sha256"] = up["body"]["sha256"]

        secondary_id = None
        if trial.get("secondary_asset_file"):
            secondary_file = inputs_dir / trial["secondary_asset_file"]
            record["secondary_input_bytes"] = secondary_file.stat().st_size
            sec = upload_streamed(host, port, trial["secondary_asset_file"], secondary_file)
            record["secondary_upload_status"] = sec["status"]
            if sec["status"] != 201:
                record["phase_reached"] = "upload_secondary"
                record["error"] = {"kind": "http", "status": sec["status"], "body": sec["body"]}
                record["rss_hwm_mib"] = hwm_mib()
                return record
            secondary_id = sec["body"]["id"]
            record["secondary_asset_id"] = secondary_id
            record["secondary_asset_sha256"] = sec["body"]["sha256"]

        record["phase_reached"] = "rendition_requested"
        payload = {
            "primary_asset_id": record["primary_asset_id"],
            "secondary_asset_id": secondary_id,
            "output_name": trial["output_name"],
            "operations": trial["operations"],
        }
        resp = request_json(host, port, "POST", "/v1/renditions", payload)
        record["rendition_http_status"] = resp["status"]
        record["rendition_response"] = resp["body"]
        if resp["status"] != 201:
            record["phase_reached"] = "rendition_failed"
            record["error"] = {"kind": "http", "status": resp["status"], "body": resp["body"]}
            record["rss_hwm_mib"] = hwm_mib()
            return record

        rendition = resp["body"]
        record["rendition_id"] = rendition["id"]
        record["plan_digest"] = rendition["plan_digest"]
        record["normalized_operations"] = rendition["operations"]
        record["transitions"] = [item["to"] for item in rendition.get("transitions", [])]
        record["output_asset_id"] = rendition["output_asset"]["id"]
        record["output_sha256"] = rendition["output_asset"]["sha256"]
        record["output_descriptor"] = rendition["output_asset"]["descriptor"]
        record["phase_reached"] = "rendition_completed"

        check = request_json(
            host, port, "POST", f"/v1/renditions/{rendition['id']}/integrity-checks", {}
        )
        record["integrity_http_status"] = check["status"]
        body = check["body"] if isinstance(check["body"], dict) else {}
        if check["status"] == 201:
            record["integrity_result"] = body.get("status")
            record["integrity_reason"] = body.get("reason")
        else:
            # A replay that cannot run inside the budget is itself evidence.
            record["integrity_result"] = "error"
            error = body.get("error", {}) if isinstance(body, dict) else {}
            record["integrity_reason"] = error.get("code")
            record["integrity_error_body"] = body

        lineage = request_json(
            host, port, "GET", f"/v1/renditions/{rendition['id']}/lineage", None
        )
        record["lineage_http_status"] = lineage["status"]
        record["lineage"] = lineage["body"]
        if isinstance(lineage["body"], dict) and "edge" in lineage["body"]:
            record["recorded_plan_digest"] = lineage["body"]["edge"].get("plan_digest")

        digest = rendition["output_asset"]["sha256"]
        source = data_dir / "content" / digest[:2] / digest[2:4] / digest
        media_target = media_dir / f"{trial['trial_id']}.wav"
        media_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, media_target)
            record["media_path"] = str(media_target)
            record["media_sha256"] = digest
            record["media_size_bytes"] = media_target.stat().st_size
            record["phase_reached"] = "media_captured"
        except OSError as exc:
            record["media_copy_error"] = f"{type(exc).__name__}: {exc}"
            record["phase_reached"] = "media_copy_failed"
            record["rss_hwm_mib"] = hwm_mib()
            return record

        record["completed"] = True
        record["rss_hwm_mib"] = hwm_mib()
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)

        # Write a sidecar next to the media so that the recording stays paired
        # with the recipe it actually used, even if result files are later
        # re-organised or partially lost.
        sidecar = media_target.with_suffix(".recipe.json")
        sidecar.write_text(
            json.dumps(
                {
                    "trial_id": trial["trial_id"],
                    "family": trial["family"],
                    "rendition_id": record.get("rendition_id"),
                    "plan_digest": record.get("plan_digest"),
                    "recorded_plan_digest": record.get("recorded_plan_digest"),
                    "normalized_operations": record.get("normalized_operations"),
                    "requested_operations": trial["operations"],
                    "primary_asset_id": record.get("primary_asset_id"),
                    "primary_asset_sha256": record.get("primary_asset_sha256"),
                    "secondary_asset_id": record.get("secondary_asset_id"),
                    "secondary_asset_sha256": record.get("secondary_asset_sha256"),
                    "output_asset_id": record.get("output_asset_id"),
                    "media_sha256": record.get("output_sha256"),
                    "media_file": media_target.name,
                    "integrity_result": record.get("integrity_result"),
                    "rss_hwm_mib": record.get("rss_hwm_mib"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        record["recipe_sidecar"] = str(sidecar)
        return record
    except MemoryError:
        record["phase_reached"] = str(record.get("phase_reached")) + "+memory_error"
        record["error"] = {
            "kind": "MemoryError",
            "detail": "allocation refused by 192 MiB RLIMIT_AS",
        }
        record["incomplete_reason"] = "memory_budget_exceeded"
        record["rss_hwm_mib"] = hwm_mib()
        return record
    except Exception as exc:  # noqa: BLE001 - record every failure verbatim
        record["error"] = {
            "kind": type(exc).__name__,
            "detail": str(exc)[:400],
            "traceback_tail": traceback.format_exc()[-700:],
        }
        record["rss_hwm_mib"] = hwm_mib()
        return record
    finally:
        gc.collect()
