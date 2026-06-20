"""RunPod orchestration — provision a GPU pod and run pod_experiment.py on it.

DRY-RUN BY DEFAULT. Launching a GPU pod costs money, so actually creating one
requires the explicit --launch flag. Reads RUNPOD_API_KEY from the repo-root .env.

This calls RunPod's GraphQL API; it is written from the documented schema but has
NOT been executed from this sandbox. Verify the pod boots and `nvidia-smi` works
before trusting a run. Docs: https://docs.runpod.io/ (GraphQL API).
"""
from __future__ import annotations

import argparse
import os
import sys

API = "https://api.runpod.io/graphql"

BOOTSTRAP = r"""
set -e
cd /workspace
pip install -q numpy torch transformers accelerate datasets
python pod_experiment.py --dlm "{dlm}" --dlm-mask "{mask}" \
    --ar "{ar}" --ar-baseline "{ar_baseline}" \
    --n-seq {n_seq} --reveal {reveal} --out results.json
"""


def load_key() -> str:
    # tiny .env reader so we don't add a dependency
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    env = os.path.join(os.path.abspath(root), ".env")
    if os.path.exists(env):
        for line in open(env):
            if line.startswith("RUNPOD_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("RUNPOD_API_KEY", "")


def create_pod_mutation(name, gpu, image) -> str:
    return f"""
    mutation {{
      podFindAndDeployOnDemand(input: {{
        name: "{name}", imageName: "{image}",
        gpuTypeId: "{gpu}", gpuCount: 1,
        containerDiskInGb: 50, volumeInGb: 50, volumeMountPath: "/workspace",
        ports: "8888/http"
      }}) {{ id imageName machineId }}
    }}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true", help="actually create a pod (costs money)")
    ap.add_argument("--gpu", default="NVIDIA A100 80GB PCIe")
    ap.add_argument("--image", default="runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04")
    ap.add_argument("--dlm", default="Dream-org/Dream-v0-Instruct-7B")
    ap.add_argument("--dlm-mask", default="<mask>")
    ap.add_argument("--ar", default="Dream-org/Dream-v0-Base-7B")
    ap.add_argument("--ar-baseline", default="meta-llama/Llama-3.1-8B")
    ap.add_argument("--n-seq", type=int, default=200)
    ap.add_argument("--reveal", type=float, default=0.5)
    args = ap.parse_args()

    key = load_key()
    if not key:
        sys.exit("no RUNPOD_API_KEY in .env or env")
    boot = BOOTSTRAP.format(dlm=args.dlm, mask=args.dlm_mask, ar=args.ar,
                            ar_baseline=args.ar_baseline, n_seq=args.n_seq,
                            reveal=args.reveal)
    print(f"GPU: {args.gpu}\nImage: {args.image}\nBootstrap:\n{boot}")
    if not args.launch:
        print("\n[dry-run] pass --launch to create the pod and run. "
              "Then copy code/ to /workspace and execute the bootstrap.")
        return

    import json as _json
    import urllib.request as _u
    body = _json.dumps({"query": create_pod_mutation("dlm-ar-align", args.gpu, args.image)})
    req = _u.Request(f"{API}?api_key={key}", data=body.encode(),
                     headers={"Content-Type": "application/json"})
    print(_u.urlopen(req).read().decode())
    print("\nPod requested. SSH in, put code/ at /workspace, run the bootstrap above.")


if __name__ == "__main__":
    main()
