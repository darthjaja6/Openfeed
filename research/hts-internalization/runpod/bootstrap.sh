#!/usr/bin/env bash
# Pod-side bootstrap: install everything, then start Phase 1 in the background.
# Runs inside the RunPod container; repo already cloned to /workspace/repo.
set -euxo pipefail

PROJ=/workspace/repo/research/hts-internalization
cd /workspace

# ---- tooling ----
pip install -q uv
export UV_SYSTEM_PYTHON=1

# ---- MatterGen (pinned checkout; fix flags in src/htsgen/sampling/mattergen.py
#      if you bump this) ----
if [ ! -d /workspace/mattergen ]; then
  git clone --depth 1 https://github.com/microsoft/mattergen.git /workspace/mattergen
fi
uv pip install -e /workspace/mattergen

# ---- htsgen + MLIP ----
uv pip install -e "$PROJ[mlip,data]" joblib

# ---- data (hull snapshot needs MP_API_KEY in pod env; skip gracefully) ----
cd "$PROJ"
python scripts/01_download_data.py ${MP_API_KEY:+--mp-api-key "$MP_API_KEY"} || true
python scripts/02_train_tc_surrogate.py || true
python scripts/00_setup_check.py

# ---- go ----
mkdir -p /workspace/runs
nohup bash "$PROJ/runpod/run_phase1.sh" > /workspace/runs/phase1.log 2>&1 &
echo "Phase 1 started; tail -f /workspace/runs/phase1.log"
# Keep container alive for inspection.
sleep infinity
