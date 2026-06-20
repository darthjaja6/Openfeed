# First-step harness — RQ1 + cheap RQ2

Implements Phase 0–1 and the cheapest slice of Phase 2 from `../04-experiments.md`:
the matched-trajectory alignment protocol, RQ1 distribution comparison, and
CKA / Procrustes / rank-distortion alignment — **with a within-paradigm baseline**,
without training anything (no crosscoder/SAE yet).

## Files

| File | Role |
|---|---|
| `metrics.py` | CKA, orthogonal Procrustes, rank-distortion (RQ2); sym-KL / JS / agreement (RQ1). Pure numpy. |
| `alignment.py` | `ProbeContext` + the matched-trajectory protocol; toy adapters (CPU). |
| `run_alignment.py` | Orchestrates RQ1 + per-layer RQ2 with the AR-vs-AR baseline. |
| `test_pipeline.py` | CPU sanity tests + a toy oracle (strong-hypothesis world). |
| `models_hf.py` | Real AR / masked-diffusion adapters. **GPU only.** |

## Run locally (CPU, numpy only)

```bash
pip install numpy
python test_pipeline.py        # 4 tests, incl. toy oracle
python run_alignment.py --toy  # end-to-end on toy models
```

The toy world is built so the DLM reads AR's kernel through a random *orthogonal*
basis change — i.e. it satisfies the strong hypothesis by construction. The
pipeline should report high CKA / near-zero Procrustes residual cross-paradigm.
That is a wiring check, **not** a result.

## Run on RunPod (real 8B models)

`RUNPOD_API_KEY` is in the repo-root `.env` (gitignored — rotate it; it was pasted
in chat). On a GPU pod:

```bash
pip install -r requirements.txt   # uncomment the torch/transformers block first
python -c "import models_hf; models_hf.smoke_test('<AR_NAME>', '<DLM_NAME>', dlm_mask_token='<MASK>')"
```

Then swap the toy adapters in `run_alignment.evaluate()` for `ARAdapter` /
`DLMAdapter`.

### Model pair to start with

Start with the **same-source** pair (identical tokenizer removes the biggest
confound, and it is the clean upper bound for the linear-basis hypothesis):

- AR  : Dream's autoregressive base checkpoint
- DLM : Dream-7B (masked diffusion)

`smoke_test()` asserts the tokenizers match. The decisive **independent** pair
(LLaDA-8B vs an independent AR) comes second — see `../04-experiments.md`.

## How to read the output

- **RQ1**: cross `sym_kl` / `js` near the AR-vs-AR baseline ⇒ same conditionals
  functionally. Far above ⇒ premise weak.
- **RQ2 per layer**: high `linear_cka` + low `procrustes_residual` ⇒ orthogonal
  (strong) basis change. If only `kernel_cka` is high, or `rank_distortion` needs
  high rank ⇒ the change of basis is nonlinear (partial result).

## Not yet here (deliberately)

Crosscoder / SAE shared-vs-specific decomposition, the learnable-basis + path-
straightness work (RQ3), and RunPod pod orchestration. Those come after this slice
returns a go/no-go.
