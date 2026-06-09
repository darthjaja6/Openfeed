# htsgen — Internalizing physical constraints into crystal diffusion models

Research code for the plan in [`docs/hts-internalization-research-plan.md`](docs/hts-internalization-research-plan.md).
Goal: measure and raise the **raw-sample compliance rate** (unguided, unfiltered
samples passing physical constraints) of crystal generative models, with BCS
superconductor discovery as the demonstration.

## Layout

```
src/htsgen/
  config.py            all thresholds (single source of truth for the paper)
  oracle/              the verifier oracle — shared by training & evaluation
    composition.py       tier 1: SMACT charge neutrality        [tested]
    geometry.py          tier 1: min distance / volume sanity   [tested]
    symmetry.py          tier 1: space-group detection          [tested]
    stability.py         tier 2: MACE relax + E_hull vs MP hull
    phonons.py           tier 2: Gamma-point gate / phonopy slow mode
    oracle.py            composition + compliance tables        [tested]
  models/tc_ensemble.py  tier 3: v0 Tc surrogate (ensemble + OOD)[tested]
  data/datasets.py       JARVIS-SC download, MP hull snapshot
  sampling/mattergen.py  sample/load/export wrappers around MatterGen CLI
  rl/reward.py           composite reward, curriculum + OOD gate [tested]
  rl/expert_iteration.py round driver: sample→score→select→finetune dataset
scripts/                 00 setup check · 01 data · 02 train surrogate ·
                         03 baseline compliance table
runpod/                  launch.py (REST API) · bootstrap.sh · run_phase1.sh
tests/                   pytest suite (8 tests, all passing on CPU-only deps)
```

## Quickstart (local Mac/CPU — Phase 0)

```bash
cd research/hts-internalization
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[data]"        # core oracle + datasets
uv pip install -e ".[mlip]"        # + MACE for tier-2 (CPU works, slower)

python scripts/00_setup_check.py                       # smoke test
python scripts/01_download_data.py --mp-api-key $MP_API_KEY
python scripts/02_train_tc_surrogate.py                # held-out MAE + calibration

# Baseline compliance table from any folder of generated CIFs:
python scripts/03_baseline_compliance.py --samples /path/to/cifs --mlip --tc
```

To produce baseline samples without a GPU box, run `mattergen-generate` on a
short-lived pod, or start with published sample dumps; the oracle side is fully
local.

## RunPod (Phase 1 — the part that needs the API key)

```bash
export RUNPOD_API_KEY=...          # the one thing needed to start
export MP_API_KEY=...              # optional but recommended (E_hull)
python runpod/launch.py --dry-run  # inspect request
python runpod/launch.py            # create pod; bootstrap installs everything
```

The pod then runs `runpod/run_phase1.sh`: **expert-iteration internalization**
— per round: raw sampling → oracle scoring (this is the per-round compliance
measurement) → top-decile selection with per-formula diversity caps →
MatterGen fine-tune on the selection. Round-by-round compliance tables land in
`/workspace/runs/expert_iter/round-*/compliance.md`; that sequence is the
paper's first headline figure.

Reward curriculum (in `run_phase1.sh`): rounds 0–1 stability-only; round 2+
adds the OOD-gated Tc surrogate term (`RewardConfig.w_tc`). The OOD gate
on/off is the tier-3 ceiling ablation.

## Verification status (honest)

- **Tested here (CPU CI)**: tier-1 oracle, compliance tables, reward logic +
  OOD gating, Tc ensemble train/save/load, package install. `pytest -q` → 8 pass.
- **Tested with real physics (CPU)**: `stability.py` MACE-MP-0 relax of MgB₂
  → a=3.077 Å, c=3.525 Å (exp. 3.086/3.524); `phonons.py` Γ-gate correctly
  reports MgB₂ dynamically stable (min optical mode +30 meV).
- **Written but needs a network/GPU box to exercise**: hull snapshot +
  E_hull (needs MP API key), dataset downloads, MatterGen wrappers.
- **Pinned-by-hand (check on first pod run)**: the two MatterGen CLI calls in
  `run_phase1.sh` (`csv_to_dataset.py`, `mattergen-finetune` flags) and the
  RunPod REST field names in `launch.py` (`--dry-run` first). Both are
  isolated so a flag rename is a one-line fix.

## Upgrade path

1. v0 Tc surrogate → equivariant GNN (BETE-NET-style) on the same grouped splits.
2. Expert iteration → DDPO/GRPO on the diffusion trajectory (true RLVR).
3. Γ-point phonon gate → phonopy supercell on survivors.
4. DFT shortlist: QE relax (local) → DFPT/EPW burst (cloud CPU) per the plan.
