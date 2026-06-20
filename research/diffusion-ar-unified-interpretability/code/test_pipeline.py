"""CPU sanity tests for the metrics and the toy alignment pipeline.

Run: python test_pipeline.py   (needs only numpy)

Verifies (a) metric math on known inputs, and (b) that on a toy world built to
satisfy the strong hypothesis, the pipeline actually reports high CKA / low
Procrustes residual cross-paradigm. If this oracle ever flips, the harness is
broken before we touch real models.
"""
import numpy as np

import metrics
from alignment import build_random_mask_probes, make_toy_pair
from run_alignment import evaluate


def test_metric_identity():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((100, 16))
    assert abs(metrics.linear_cka(X, X) - 1.0) < 1e-6
    assert abs(metrics.kernel_cka(X, X) - 1.0) < 1e-6
    assert metrics.procrustes_residual(X, X) < 1e-6
    P = metrics._safe(rng.random((50, 10)))
    assert np.allclose(metrics.sym_kl(P, P), 0, atol=1e-9)
    assert np.allclose(metrics.js_divergence(P, P), 0, atol=1e-9)
    assert metrics.argmax_agreement(P, P) == 1.0


def test_orthogonal_invariance():
    # CKA invariant under orthogonal map; Procrustes residual ~0 for a rotation.
    rng = np.random.default_rng(1)
    X = rng.standard_normal((200, 24))
    Q, _ = np.linalg.qr(rng.standard_normal((24, 24)))
    Y = X @ Q
    assert abs(metrics.linear_cka(X, Y) - 1.0) < 1e-6
    assert metrics.procrustes_residual(X, Y) < 1e-6


def test_rank_distortion_monotone():
    rng = np.random.default_rng(2)
    X = rng.standard_normal((300, 32))
    Y = X @ rng.standard_normal((32, 32))  # full-rank linear -> low rank insufficient
    curve = metrics.rank_distortion_curve(X, Y)
    ranks = sorted(curve)
    vals = [curve[r] for r in ranks]
    assert all(vals[i] >= vals[i + 1] - 1e-9 for i in range(len(vals) - 1)), "non-monotone"
    assert curve[ranks[-1]] < 1e-6, "full rank should reconstruct a linear map"


def test_toy_pipeline_oracle():
    ar, dlm = make_toy_pair(seed=0)
    ar2, _ = make_toy_pair(seed=1)
    rng = np.random.default_rng(0)
    seqs = [tuple(rng.integers(0, ar.vocab_size, size=12)) for _ in range(30)]
    probes = build_random_mask_probes(seqs, reveal_frac=0.5, n_per_seq=3, seed=0)
    res = evaluate(ar, dlm, ar2, probes)
    # toy DLM = AR kernel under an orthogonal basis change -> strong-hypothesis world
    l0 = res["rq2"]["cross_ar_vs_dlm"]["layer_0"]
    assert l0["linear_cka"] > 0.95, l0["linear_cka"]
    assert l0["procrustes_residual"] < 0.05, l0["procrustes_residual"]
    # RQ1: AR and DLM read out from the same kernel -> identical predictive dists
    assert res["rq1"]["cross_ar_vs_dlm"]["argmax_agreement"] == 1.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
