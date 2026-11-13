#!/usr/bin/env python3
"""Run exactly one trial inside a fresh process with a hard 192 MiB limit.

Each trial gets an independent process so that no earlier allocation, cache or
fragmentation is charged to the next trial's budget.  The child applies
RLIMIT_AS before importing the service, starts the real HTTP server on an
ephemeral port, runs one trial, writes the record, and exits.
"""
from __future__ import annotations

import argparse
import gc
import json
import resource
import sys
import threading
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parents[2]
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import harness  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit-mib", type=int, default=192)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--inputs-dir", required=True)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--record", required=True)
    args = parser.parse_args()

    limit_bytes = args.limit_mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    trial = json.loads(Path(args.trial).read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    from avprovenance.http_api import build_http_server

    # Observation-only: record which exception type escapes the render call.
    # The service maps MemoryError to a generic RENDER_FAILED response, which
    # would otherwise hide the budget cause of an incomplete trial.  The
    # wrapper re-raises the original exception unchanged.
    import avprovenance.renditions.executor as executor_module
    import avprovenance.media.pipeline as pipeline_module

    original_execute = executor_module.execute_plan

    def observed_execute(*call_args, **call_kwargs):
        try:
            return original_execute(*call_args, **call_kwargs)
        except BaseException as exc:
            harness.record_render_exception("execute_plan", exc)
            raise

    executor_module.execute_plan = observed_execute
    # application.py imported the symbol by value, so patch there too.
    import avprovenance.application as application_module

    application_module.execute_plan = observed_execute

    original_apply = pipeline_module.apply_media_operations

    def observed_apply(*call_args, **call_kwargs):
        try:
            return original_apply(*call_args, **call_kwargs)
        except BaseException as exc:
            harness.record_render_exception("apply_media_operations", exc)
            raise

    pipeline_module.apply_media_operations = observed_apply
    import avprovenance.renditions.verifier as verifier_module

    # Observation-only: attribute ingest-path failures (base64 decode, media
    # inspection) that the HTTP boundary reports as a generic 500.
    import avprovenance.http_api as http_api_module
    import avprovenance.transport.request_reader as reader_module

    original_decode_media_payload = http_api_module.decode_media_payload

    def observed_decode_media_payload(*call_args, **call_kwargs):
        try:
            return original_decode_media_payload(*call_args, **call_kwargs)
        except BaseException as exc:
            harness.record_render_exception("decode_media_payload", exc)
            raise

    http_api_module.decode_media_payload = observed_decode_media_payload

    original_read_json_body = reader_module.read_json_body

    def observed_read_json_body(*call_args, **call_kwargs):
        try:
            return original_read_json_body(*call_args, **call_kwargs)
        except BaseException as exc:
            harness.record_render_exception("read_json_body", exc)
            raise

    reader_module.read_json_body = observed_read_json_body
    http_api_module.read_json_body = observed_read_json_body

    import avprovenance.application as app_module

    original_ingest = app_module.MediaProvenanceApplication.ingest_asset

    def observed_ingest(self, *call_args, **call_kwargs):
        try:
            return original_ingest(self, *call_args, **call_kwargs)
        except BaseException as exc:
            harness.record_render_exception("ingest_asset", exc)
            raise

    app_module.MediaProvenanceApplication.ingest_asset = observed_ingest

    verifier_module.apply_media_operations = observed_apply

    try:
        server = build_http_server("127.0.0.1", 0, data_dir)
    except MemoryError:
        Path(args.record).write_text(
            json.dumps(
                {
                    "trial_id": trial["trial_id"],
                    "completed": False,
                    "phase_reached": "server_start+memory_error",
                    "incomplete_reason": "memory_budget_exceeded",
                    "error": {"kind": "MemoryError"},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 3

    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]

    record = harness.execute_trial(
        host=str(host),
        port=int(port),
        data_dir=data_dir,
        media_dir=Path(args.media_dir),
        inputs_dir=Path(args.inputs_dir),
        trial=trial,
    )
    record["process_pid"] = __import__("os").getpid()
    record["rss_vm_peak_mib"] = harness.hwm_mib()
    record["render_exceptions_observed"] = list(harness.RENDER_EXCEPTIONS)
    Path(args.record).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    gc.collect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
