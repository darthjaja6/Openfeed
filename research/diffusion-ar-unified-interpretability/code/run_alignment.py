"""First-step experiment: RQ1 (distribution match) + cheap RQ2 (CKA/Procrustes).

Always reports a *within-paradigm baseline*: the same metrics between two models of
the SAME paradigm. Cross-paradigm numbers are only interpretable relative to it.

Run on toy models (CPU, no deps beyond numpy):
    python run_alignment.py --toy

On RunPod with real models, pass adapters built in models_hf.py instead (see README).
"""
from __future__ import annotations

import argparse
import json

import numpy as np

import metrics
from alignment import build_random_mask_probes, make_toy_pair


def rq1_distribution(dist_a: np.ndarray, dist_b: np.ndarray) -> dict:
    return {
        "sym_kl_mean": float(np.mean(metrics.sym_kl(dist_a, dist_b))),
        "js_mean": float(np.mean(metrics.js_divergence(dist_a, dist_b))),
        "argmax_agreement": metrics.argmax_agreement(dist_a, dist_b),
        "top5_agreement": metrics.topk_agreement(dist_a, dist_b, k=5),
    }


def rq2_per_layer(reps_a: np.ndarray, reps_b: np.ndarray) -> dict:
    """reps_*: (N, n_layers, d). Compare layer-by-layer."""
    n_layers = reps_a.shape[1]
    out = {}
    for l in range(n_layers):
        A, B = reps_a[:, l, :], reps_b[:, l, :]
        out[f"layer_{l}"] = {
            "linear_cka": metrics.linear_cka(A, B),
            "kernel_cka": metrics.kernel_cka(A, B),
            "procrustes_residual": metrics.procrustes_residual(A, B),
            "rank_distortion": metrics.rank_distortion_curve(A, B),
        }
    return out


def evaluate(ar, dlm, ar2, probes) -> dict:
    """ar2 is a second AR-paradigm model for the within-paradigm baseline."""
    d_ar, r_ar = ar.collect(probes)
    d_dlm, r_dlm = dlm.collect(probes)
    d_ar2, r_ar2 = ar2.collect(probes)
    return {
        "n_probes": len(probes),
        "rq1": {
            "cross_ar_vs_dlm": rq1_distribution(d_ar, d_dlm),
            "baseline_ar_vs_ar2": rq1_distribution(d_ar, d_ar2),
        },
        "rq2": {
            "cross_ar_vs_dlm": rq2_per_layer(r_ar, r_dlm),
            "baseline_ar_vs_ar2": rq2_per_layer(r_ar, r_ar2),
        },
    }


def _toy_main(out_path: str | None):
    # toy world satisfies the strong hypothesis by construction (orthogonal basis),
    # so this is a sanity oracle: cross CKA should be high, Procrustes residual low.
    ar, dlm = make_toy_pair(seed=0)
    ar2, _ = make_toy_pair(seed=1)  # a different AR for the baseline
    rng = np.random.default_rng(0)
    seqs = [tuple(rng.integers(0, ar.vocab_size, size=12)) for _ in range(40)]
    probes = build_random_mask_probes(seqs, reveal_frac=0.5, n_per_seq=3, seed=0)
    res = evaluate(ar, dlm, ar2, probes)
    print(json.dumps(res, indent=2))
    if out_path:
        with open(out_path, "w") as f:
            json.dump(res, f, indent=2)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--toy", action="store_true", help="run on toy models (CPU)")
    ap.add_argument("--out", default=None, help="write results JSON here")
    args = ap.parse_args()
    if args.toy:
        _toy_main(args.out)
    else:
        raise SystemExit("Real-model run goes through models_hf.py on RunPod; see README.")
