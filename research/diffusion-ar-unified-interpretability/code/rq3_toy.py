"""RQ3 toy: does a learnable basis + better coupling straighten the flow-ODE field?

Numpy-only proof-of-concept of the mechanism behind the second half of the goal
("找一个让 ODE 路径变直的好基"). NOT a real-model result.

Straightness proxy (rectified-flow style): for a linear-interpolant coupling, the
marginal velocity field is v(x,t)=E[u | x_t=x, t] with u = x1 - x0. The flow ODE
is straight iff u is *determined* by (x_t, t), i.e. Var[u | x_t, t] is small. We
estimate

    S = E_{t,sample} Var[u | x_t, t]    (kNN estimate in (x_t, t) space)

Lower S => straighter marginal field => fewer Euler steps to integrate. We compare:
  (a) identity basis + independent coupling   (baseline)
  (b) identity basis + entropic-OT coupling   (known straightener)
  (c) learned volume-preserving basis + OT    (the extra knob this project proposes)

Basis is constrained to SL(2) (det=1) so it can reshape geometry but not cheat by
shrinking everything.
"""
from __future__ import annotations

import numpy as np


# --------------------------------------------------------------------------- #
# toy data: Gaussian source, bimodal anisotropic target (independent coupling
# then gives a genuinely curved marginal field -> room to straighten)
# --------------------------------------------------------------------------- #
def sample_data(n, seed=0):
    rng = np.random.default_rng(seed)
    x0 = rng.standard_normal((n, 2)) * 0.3
    comp = rng.integers(0, 2, n)
    centers = np.array([[2.5, 1.2], [-2.5, -1.2]])
    x1 = centers[comp] + rng.standard_normal((n, 2)) @ np.diag([0.25, 1.0])
    return x0, x1


# --------------------------------------------------------------------------- #
# couplings
# --------------------------------------------------------------------------- #
def independent_coupling(x0, x1, seed=0):
    rng = np.random.default_rng(seed)
    return x0, x1[rng.permutation(len(x1))]


def sinkhorn_coupling(x0, x1, eps=0.1, iters=200, seed=0):
    """Entropic-OT plan; sample one target per source from each row."""
    C = np.sum((x0[:, None, :] - x1[None, :, :]) ** 2, axis=-1)
    C = C / C.max()
    K = np.exp(-C / eps)
    n = len(x0)
    u, v = np.ones(n), np.ones(n)
    for _ in range(iters):
        u = 1.0 / (K @ v + 1e-12)
        v = 1.0 / (K.T @ u + 1e-12)
    P = u[:, None] * K * v[None, :]
    P = P / P.sum(axis=1, keepdims=True)
    rng = np.random.default_rng(seed)
    idx = np.array([rng.choice(n, p=P[i]) for i in range(n)])
    return x0, x1[idx]


# --------------------------------------------------------------------------- #
# straightness proxy
# --------------------------------------------------------------------------- #
def straightness_S(x0, x1, n_t=8, k=12, seed=0):
    rng = np.random.default_rng(seed)
    u = x1 - x0
    feats, us = [], []
    for t in np.linspace(0.05, 0.95, n_t):
        xt = (1 - t) * x0 + t * x1
        feats.append(np.column_stack([xt, np.full(len(xt), t)]))
        us.append(u)
    F = np.vstack(feats)            # (N*n_t, 3)  features (x_t, t)
    U = np.vstack(us)              # (N*n_t, 2)  velocity targets
    # scale t so it is comparable to spatial coords in the kNN metric
    F = F.copy()
    F[:, 2] *= np.std(F[:, :2])
    M = len(F)
    sub = rng.choice(M, size=min(M, 600), replace=False)
    variances = []
    for i in sub:
        d = np.sum((F - F[i]) ** 2, axis=1)
        nn = np.argpartition(d, k)[:k]
        variances.append(np.mean(np.var(U[nn], axis=0)))
    return float(np.mean(variances))


# --------------------------------------------------------------------------- #
# learnable volume-preserving basis: B = R(a) diag(s, 1/s) R(b), det(B)=1
# --------------------------------------------------------------------------- #
def _rot(a):
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s], [s, c]])


def make_basis(a, b, log_s):
    s = np.exp(log_s)
    return _rot(a) @ np.diag([s, 1.0 / s]) @ _rot(b)


def fit_basis(x0, x1, n_restarts=40, seed=0):
    """Random search over SL(2) basis params to minimize straightness in-basis."""
    rng = np.random.default_rng(seed)
    best, best_S = None, np.inf
    for _ in range(n_restarts):
        a, b = rng.uniform(0, np.pi, 2)
        log_s = rng.uniform(-1.0, 1.0)
        B = make_basis(a, b, log_s)
        S = straightness_S(x0 @ B.T, x1 @ B.T, seed=seed)
        if S < best_S:
            best_S, best = S, (a, b, log_s)
    return make_basis(*best), best_S


def main():
    x0, x1 = sample_data(500, seed=0)

    s_indep = straightness_S(*independent_coupling(x0, x1))
    xo0, xo1 = sinkhorn_coupling(x0, x1)
    s_ot = straightness_S(xo0, xo1)
    B, s_basis = fit_basis(xo0, xo1)

    print("straightness S (lower = straighter marginal field, fewer ODE steps):")
    print(f"  (a) identity   + independent coupling : {s_indep:.4f}")
    print(f"  (b) identity   + OT coupling          : {s_ot:.4f}")
    print(f"  (c) learned SL(2) basis + OT coupling : {s_basis:.4f}")
    print(f"\n  OT vs independent      : {100*(1-s_ot/s_indep):+.1f}% straighter")
    print(f"  +learned basis vs OT   : {100*(1-s_basis/s_ot):+.1f}% straighter")
    return dict(indep=s_indep, ot=s_ot, basis=s_basis)


if __name__ == "__main__":
    main()
