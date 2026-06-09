#!/usr/bin/env python3
"""Launch a RunPod GPU pod that bootstraps this repo and runs Phase 1.

Usage:
  export RUNPOD_API_KEY=...           # the only thing needed from the user
  python runpod/launch.py --dry-run   # inspect the request first
  python runpod/launch.py             # create the pod
  python runpod/launch.py --list      # list pods
  python runpod/launch.py --terminate POD_ID

The pod pulls GIT_REPO/GIT_BRANCH, runs runpod/bootstrap.sh, which installs
mattergen + htsgen and starts runpod/run_phase1.sh under nohup. Results land in
/workspace/runs (persistent volume) — fetch with runpodctl or scp.

NOTE: field names follow the RunPod REST API (https://rest.runpod.io/v1/pods).
Verified against docs as of 2026-06; if the API moved, --dry-run shows the
payload to fix.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = "https://rest.runpod.io/v1"

DEFAULTS = dict(
    name="htsgen-phase1",
    image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
    gpus=["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000"],  # cheap-first
    gpu_count=1,
    disk_gb=40,
    volume_gb=60,
    repo="https://github.com/darthjaja6/Openfeed.git",
    branch="claude/hts-discovery-diffusion-transformers-l0wrn6",
)


def _req(method: str, path: str, key: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or "{}")


def pod_payload(args) -> dict:
    env = {
        "GIT_REPO": args.repo,
        "GIT_BRANCH": args.branch,
        "HTSGEN_ROUNDS": str(args.rounds),
        "HTSGEN_N_SAMPLES": str(args.n_samples),
    }
    if os.environ.get("MP_API_KEY"):
        env["MP_API_KEY"] = os.environ["MP_API_KEY"]
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]

    bootstrap = ("bash -c 'cd /workspace && "
                 "git clone --depth 1 -b \"$GIT_BRANCH\" \"$GIT_REPO\" repo && "
                 "bash repo/research/hts-internalization/runpod/bootstrap.sh'")
    return {
        "name": args.name,
        "imageName": args.image,
        "gpuTypeIds": args.gpus,
        "gpuCount": DEFAULTS["gpu_count"],
        "containerDiskInGb": DEFAULTS["disk_gb"],
        "volumeInGb": DEFAULTS["volume_gb"],
        "volumeMountPath": "/workspace",
        "env": env,
        "dockerStartCmd": ["bash", "-c", bootstrap],
        "cloudType": "COMMUNITY",   # cheapest; switch to SECURE if needed
        "interruptible": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=DEFAULTS["name"])
    ap.add_argument("--image", default=DEFAULTS["image"])
    ap.add_argument("--gpus", nargs="+", default=DEFAULTS["gpus"])
    ap.add_argument("--repo", default=DEFAULTS["repo"])
    ap.add_argument("--branch", default=DEFAULTS["branch"])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--n-samples", type=int, default=4096)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--terminate", metavar="POD_ID")
    args = ap.parse_args()

    key = os.environ.get("RUNPOD_API_KEY")
    if not key and not args.dry_run:
        sys.exit("Set RUNPOD_API_KEY (https://www.runpod.io/console/user/settings)")

    if args.list:
        print(json.dumps(_req("GET", "/pods", key), indent=1))
        return
    if args.terminate:
        print(_req("DELETE", f"/pods/{args.terminate}", key))
        return

    payload = pod_payload(args)
    print(json.dumps(payload, indent=1))
    if args.dry_run:
        print("\n--dry-run: not creating pod")
        return
    resp = _req("POST", "/pods", key, payload)
    print("\ncreated:", json.dumps(resp, indent=1))
    print("\nMonitor:  python runpod/launch.py --list\n"
          "Results land on the pod volume at /workspace/runs/")


if __name__ == "__main__":
    main()
