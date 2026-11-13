#!/usr/bin/env python3
"""Measure the upload ceiling with one fresh capped process per candidate size.

Methodology matters here: a single long-lived service process accumulates
retained arenas, so a later upload in the same process sees a lower ceiling
than a first upload in a fresh process.  Each real trial gets a fresh process,
so each candidate size must be measured in its own fresh process too.

The parent writes the candidate WAV to disk, then starts a child with
RLIMIT_AS = 192 MiB, which starts the real service and performs exactly one
streamed upload.  The child reports the status and its own peak RSS.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parents[2]


def child_one_upload(payload_path: Path) -> None:
    payload = json.loads(payload_path.read_text())
    limit_bytes = payload["limit_mib"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    sys.path.insert(0, str(TOOLS_DIR))
    sys.path.insert(0, str(PROJECT_ROOT))
    import harness  # noqa: E402
    from avprovenance.http_api import build_http_server  # noqa: E402

    server = build_http_server("127.0.0.1", 0, payload["data_dir"])
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    host, port = server.server_address[:2]

    candidate = Path(payload["candidate"])
    result = {"size_bytes": candidate.stat().st_size}
    try:
        up = harness.upload_streamed(str(host), int(port), candidate.name, candidate)
        result.update(
            {
                "status": up["status"],
                "accepted": up["status"] == 201,
                "error_code": (up["body"].get("error") or {}).get("code")
                if isinstance(up["body"], dict)
                else None,
                "server_peak_mib": harness.hwm_mib(),
            }
        )
    except MemoryError:
        result.update(
            {
                "status": None,
                "accepted": False,
                "error_code": "CLIENT_MEMORY_ERROR",
                "server_peak_mib": harness.hwm_mib(),
            }
        )
    except (BrokenPipeError, ConnectionResetError) as exc:
        result.update(
            {
                "status": None,
                "accepted": False,
                "error_code": f"CLIENT_{type(exc).__name__.upper()}",
                "server_peak_mib": harness.hwm_mib(),
            }
        )
    Path(payload["out"]).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit-mib", type=int, default=192)
    parser.add_argument(
        "--sizes",
        default="16000000,17000000,18000000,19000000,20000000,21000000,22000000,23000000,24000000,25000000",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(TOOLS_DIR))
    import materials  # noqa: E402

    out_path = Path(args.out).resolve()
    sizes = [int(item) for item in args.sizes.split(",")]
    results = []
    with tempfile.TemporaryDirectory(prefix="upload-ceiling-") as tmpdir:
        tmp = Path(tmpdir)
        for size_bytes in sizes:
            candidate = tmp / f"candidate_{size_bytes}.wav"
            materials.write_tiled_material(candidate, "dense", size_bytes)
            payload_path = tmp / f"payload_{size_bytes}.json"
            child_out = tmp / f"result_{size_bytes}.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "limit_mib": args.limit_mib,
                        "data_dir": str(tmp / f"data_{size_bytes}"),
                        "candidate": str(candidate),
                        "out": str(child_out),
                    }
                )
            )
            started = time.monotonic()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--child",
                    str(payload_path),
                ],
                capture_output=True,
                timeout=900,
            )
            if child_out.is_file():
                result = json.loads(child_out.read_text())
            else:
                result = {
                    "size_bytes": size_bytes,
                    "status": None,
                    "accepted": False,
                    "error_code": "CHILD_NO_RESULT",
                    "child_exit_code": completed.returncode,
                    "stderr_tail": completed.stderr.decode("utf-8", "replace")[-300:],
                }
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            results.append(result)
            print(json.dumps(result), flush=True)

    accepted = [r["size_bytes"] for r in results if r["accepted"]]
    rejected = [r["size_bytes"] for r in results if not r["accepted"]]
    summary = {
        "method": "one fresh RLIMIT_AS-capped process per candidate size, one upload each",
        "limit_mib": args.limit_mib,
        "largest_accepted_bytes": max(accepted) if accepted else None,
        "smallest_rejected_bytes": min(rejected) if rejected else None,
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "largest_accepted_bytes": summary["largest_accepted_bytes"],
                "smallest_rejected_bytes": summary["smallest_rejected_bytes"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child_one_upload(Path(sys.argv[2]))
        raise SystemExit(0)
    raise SystemExit(main())
