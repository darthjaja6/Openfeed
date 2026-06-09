#!/usr/bin/env bash
# Phase 1 driver: expert-iteration rounds on the GPU pod.
#   round k: sample raw -> oracle -> compliance table -> select top-q ->
#            fine-tune MatterGen on selection -> next round
# Env: HTSGEN_ROUNDS (default 3), HTSGEN_N_SAMPLES (default 4096)
set -euxo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROUNDS="${HTSGEN_ROUNDS:-3}"
NSAMP="${HTSGEN_N_SAMPLES:-4096}"
RUNDIR=/workspace/runs/expert_iter
mkdir -p "$RUNDIR"

CKPT=""   # empty = pretrained mattergen_base
for ((k=0; k<ROUNDS; k++)); do
  python - "$k" "$NSAMP" "$RUNDIR" "$CKPT" <<'PY'
import sys
from pathlib import Path
from htsgen.config import PATHS
from htsgen.oracle.oracle import Oracle
from htsgen.oracle.stability import HullChecker, MLIPRelaxer
from htsgen.rl.expert_iteration import RoundConfig, run_round
from htsgen.rl.reward import RewardConfig

k, nsamp, rundir, ckpt = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
relaxer = MLIPRelaxer(device="cuda")
oracle = Oracle(relaxer=relaxer, run_phonons=False)
if PATHS.mp_entries.exists():
    oracle.hull = HullChecker(PATHS.mp_entries, relaxer)
if (PATHS.tc_model_dir / "meta.json").exists():
    from htsgen.models.tc_ensemble import TcEnsemble
    oracle.tc_model = TcEnsemble.load(PATHS.tc_model_dir)

# curriculum: stability-only in round 0-1, Tc joins (gated) from round 2
rcfg = RewardConfig(w_tc=0.0 if k < 2 else 1.0)
out = run_round(k, oracle, rcfg,
                RoundConfig(n_samples=nsamp, workdir=rundir),
                checkpoint=Path(ckpt) if ckpt else None)
print("ROUND_RESULT", out["finetune_data"])
PY

  DATA="$RUNDIR/round-0$k/finetune_data"
  FT_OUT="$RUNDIR/round-0$k/ft_ckpt"
  # ---- MatterGen fine-tune on the selected set ----
  # The dataset prep + trainer entrypoints below are pinned to the bootstrap
  # checkout of microsoft/mattergen. If upstream renamed flags, fix HERE only:
  # 1) convert CIF manifest to mattergen dataset format
  python /workspace/mattergen/scripts/csv_to_dataset.py \
      --csv "$DATA/manifest.csv" --cif-dir "$DATA" \
      --output-dir "$DATA/dataset" || {
        echo "csv_to_dataset entrypoint mismatch — check mattergen checkout"; exit 1; }
  # 2) short fine-tune from the previous checkpoint (or pretrained base)
  mattergen-finetune \
      data_module.root="$DATA/dataset" \
      trainer.max_epochs=20 \
      ${CKPT:+finetune_from="$CKPT"} \
      output_dir="$FT_OUT" || {
        echo "mattergen-finetune flags mismatch — check mattergen checkout"; exit 1; }
  CKPT="$FT_OUT"
done

echo "ALL ROUNDS DONE. Per-round compliance tables: $RUNDIR/round-*/compliance.md"
