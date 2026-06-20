"""Alignment and distribution-comparison metrics (RQ1 + cheap part of RQ2).

Pure-numpy, no torch / no GPU. These are the load-bearing measurements, so they
are kept dependency-light and unit-tested in test_pipeline.py.

RQ2 (mechanistic change-of-basis):
    - linear_cka, kernel_cka      : representation similarity, T-free
    - procrustes_residual         : best orthogonal T, residual after aligning
    - low_rank_linear_residual    : rank-distortion curve (is a low-rank linear
                                    map enough? -> "linear basis change")

RQ1 (functional / distribution):
    - sym_kl, js_divergence       : predictive-distribution distance
    - topk_agreement, argmax_agree: cheap agreement signals
"""
from __future__ import annotations

import numpy as np


# --------------------------------------------------------------------------- #
# RQ2 — representation alignment
# --------------------------------------------------------------------------- #
def _center(x: np.ndarray) -> np.ndarray:
    return x - x.mean(axis=0, keepdims=True)


def linear_cka(X: np.ndarray, Y: np.ndarray) -> float:
    """Linear CKA in [0, 1]. Rotation/isotropic-scale invariant.

    X, Y: (n_samples, d_x), (n_samples, d_y). d_x and d_y may differ.
    """
    Xc, Yc = _center(X), _center(Y)
    hsic = np.linalg.norm(Xc.T @ Yc, ord="fro") ** 2
    norm_x = np.linalg.norm(Xc.T @ Xc, ord="fro")
    norm_y = np.linalg.norm(Yc.T @ Yc, ord="fro")
    denom = norm_x * norm_y
    return float(hsic / denom) if denom > 0 else 0.0


def _rbf_gram(X: np.ndarray, sigma: float | None = None) -> np.ndarray:
    sq = np.sum(X**2, axis=1)
    d2 = sq[:, None] + sq[None, :] - 2 * X @ X.T
    d2 = np.maximum(d2, 0.0)
    if sigma is None:  # median heuristic over off-diagonal distances
        med = np.median(d2[d2 > 0]) if np.any(d2 > 0) else 1.0
        sigma = np.sqrt(med / 2) if med > 0 else 1.0
    return np.exp(-d2 / (2 * sigma**2))


def kernel_cka(X: np.ndarray, Y: np.ndarray) -> float:
    """RBF-kernel CKA in [0, 1]. Catches nonlinear similarity linear_cka misses."""
    n = X.shape[0]
    H = np.eye(n) - np.ones((n, n)) / n
    Kx = H @ _rbf_gram(X) @ H
    Ky = H @ _rbf_gram(Y) @ H
    hsic = np.sum(Kx * Ky)
    denom = np.sqrt(np.sum(Kx * Kx) * np.sum(Ky * Ky))
    return float(hsic / denom) if denom > 0 else 0.0


def procrustes_residual(X: np.ndarray, Y: np.ndarray) -> float:
    """Best *orthogonal* map T (X T ~ Y), return normalized residual in [0, 1].

    0 => Y is an orthogonal rotation of X (the "strong version": linear/orthogonal
    change of basis). ~1 => no orthogonal map aligns them.
    Requires d_x == d_y; if not, the smaller is zero-padded.
    """
    Xc, Yc = _center(X), _center(Y)
    d = max(Xc.shape[1], Yc.shape[1])
    Xc = np.pad(Xc, ((0, 0), (0, d - Xc.shape[1])))
    Yc = np.pad(Yc, ((0, 0), (0, d - Yc.shape[1])))
    # orthogonal Procrustes: T = U V^T from SVD of Xc^T Yc
    U, _, Vt = np.linalg.svd(Xc.T @ Yc)
    T = U @ Vt
    resid = np.linalg.norm(Xc @ T - Yc, ord="fro") ** 2
    denom = np.linalg.norm(Yc, ord="fro") ** 2
    return float(resid / denom) if denom > 0 else 0.0


def low_rank_linear_residual(X: np.ndarray, Y: np.ndarray, rank: int) -> float:
    """Best rank-`rank` linear map (ridge-free) X W ~ Y, normalized residual.

    Sweeping `rank` gives a rank-distortion curve: if a *low* rank already drives
    the residual near 0, a low-rank linear basis change suffices (RQ2 strong).
    """
    Xc, Yc = _center(X), _center(Y)
    # full least-squares map, then truncate its SVD to `rank`
    W, *_ = np.linalg.lstsq(Xc, Yc, rcond=None)
    U, s, Vt = np.linalg.svd(W, full_matrices=False)
    r = min(rank, len(s))
    W_r = (U[:, :r] * s[:r]) @ Vt[:r]
    resid = np.linalg.norm(Xc @ W_r - Yc, ord="fro") ** 2
    denom = np.linalg.norm(Yc, ord="fro") ** 2
    return float(resid / denom) if denom > 0 else 0.0


def rank_distortion_curve(X: np.ndarray, Y: np.ndarray, ranks=None) -> dict[int, float]:
    if ranks is None:
        dmin = min(X.shape[1], Y.shape[1])
        ranks = sorted({1, 2, 4, 8, 16, 32, 64, dmin})
        ranks = [r for r in ranks if r <= dmin]
    return {r: low_rank_linear_residual(X, Y, r) for r in ranks}


# --------------------------------------------------------------------------- #
# RQ1 — predictive distribution comparison
# --------------------------------------------------------------------------- #
def _safe(p: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    p = np.clip(p, eps, None)
    return p / p.sum(axis=-1, keepdims=True)


def sym_kl(P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Symmetric KL per row. P, Q: (n, vocab). Returns (n,)."""
    P, Q = _safe(P), _safe(Q)
    return np.sum(P * np.log(P / Q) + Q * np.log(Q / P), axis=-1)


def js_divergence(P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Jensen-Shannon divergence per row (base-2, in [0, 1])."""
    P, Q = _safe(P), _safe(Q)
    M = 0.5 * (P + Q)
    kl = lambda a, b: np.sum(a * (np.log(a) - np.log(b)), axis=-1)
    return 0.5 * (kl(P, M) + kl(Q, M)) / np.log(2)


def argmax_agreement(P: np.ndarray, Q: np.ndarray) -> float:
    return float(np.mean(np.argmax(P, axis=-1) == np.argmax(Q, axis=-1)))


def topk_agreement(P: np.ndarray, Q: np.ndarray, k: int = 5) -> float:
    """Mean Jaccard overlap of the top-k token sets per row."""
    tk = lambda M: np.argsort(-M, axis=-1)[:, :k]
    A, B = tk(P), tk(Q)
    overlaps = [len(set(a) & set(b)) / len(set(a) | set(b)) for a, b in zip(A, B)]
    return float(np.mean(overlaps))
