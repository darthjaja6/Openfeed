# Project: constraint internalization for superconductor discovery (htsgen)

Onboarding notes for Claude Code working in this directory.

## What this is
Research project: internalize physical constraints (charge neutrality, symmetry,
stability, Tc) into crystal diffusion models (MatterGen), so raw unguided samples
are physically valid. Headline metric: **raw-sample compliance rate**. Discovery
demo: DFPT-validated ambient-pressure BCS superconductor candidates.

Read first:
- `docs/hts-internalization-research-plan.md` — the finalized plan
  (internalization ladder, phases, budget, paper title/abstract drafts)
- `docs/hts-generative-discovery-survey-and-plan.md` — literature survey
- `README.md` here — code layout + verification status

## Current state (2026-06)
- Phase 0 code complete & tested: tiered verifier oracle (tier1 exact checks,
  tier2 MACE relax + E_hull + Γ-phonon gate, tier3 OOD-gated Tc surrogate),
  expert-iteration loop, RunPod launcher. 8/8 pytest pass; MACE physics
  verified on MgB₂ (a=3.077/c=3.525 Å vs exp 3.086/3.524).
- NOT yet run: dataset downloads (01), Tc surrogate training (02), baseline
  compliance table (03), RunPod Phase 1. These run on this machine / pod.

## Immediate next steps
1. `python scripts/01_download_data.py --mp-api-key $MP_API_KEY` (key: materialsproject.org dashboard)
2. `python scripts/02_train_tc_surrogate.py` → record held-out MAE + 2σ coverage
3. RunPod: `export RUNPOD_API_KEY=...; python runpod/launch.py --dry-run` then launch
4. First-pod-run checkpoints (pinned-by-hand, likely need 1-line fixes):
   - MatterGen finetune CLI flags in `runpod/run_phase1.sh` (two call sites, marked)
   - RunPod REST field names in `runpod/launch.py`
5. After round tables exist: compare round-0 vs round-N compliance — that curve
   is the paper's first figure.

## Environment (Apple Silicon)
```bash
uv venv .venv --python 3.11 && source .venv/bin/activate
uv pip install -e ".[data,mlip]"   # mlip = torch+mace, CPU/MPS fine for oracle
python scripts/00_setup_check.py
pytest -q                           # should be 8 passed
```
GPU training/sampling happens on RunPod (CUDA), never locally.

## Conventions
- All thresholds live in `src/htsgen/config.py` — never hardcode cutoffs elsewhere.
- The oracle is the single source of truth for both training rewards and
  evaluation; don't fork its logic.
- Tier-1 violations are hard reward zero (never reinforce invalid structures).
- Never commit API keys. Plan docs are the protocol — update them when the
  protocol changes.
