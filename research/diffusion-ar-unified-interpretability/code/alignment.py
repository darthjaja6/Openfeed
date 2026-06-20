"""The matched-trajectory alignment protocol — the load-bearing design.

The central difficulty (see 03-methodology.md): to compare a DLM and an AR model
fairly we must put their internal states in correspondence under the *same*
conditioning. We do that at the level of a probe:

    ProbeContext = (tokens, revealed positions S, target position t)

Both models are asked the same question: "given the tokens at positions S, what is
the distribution over the token at position t, and what is your hidden state there?"

    - AR-native mode:  S = {0..t-1}  (a prefix). This is the only conditioning a
      causal AR model can represent without modification.
    - DLM-native mode: S = arbitrary subset not containing t. A DLM can condition
      on this directly; a vanilla AR cannot, so cross-paradigm comparison in this
      mode requires an any-order / permuted-AR or is restricted to RQ1-on-DLM.

Adapters expose two methods at the target position t:
    predict_dist(ctx)  -> (vocab,)          for RQ1
    hidden_states(ctx) -> (n_layers, d)     for RQ2

Real adapters (HF LLaDA / Dream / LLaMA) live in models_hf.py and only run on a
GPU box. The Toy* adapters here let the whole pipeline run on CPU for validation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ProbeContext:
    tokens: tuple[int, ...]          # full token sequence (target token is the truth)
    revealed: tuple[int, ...]        # positions whose tokens the model may see
    target: int                      # position to predict / read representation at

    def __post_init__(self):
        assert self.target not in self.revealed, "target must be masked"
        assert all(0 <= p < len(self.tokens) for p in self.revealed)
        assert 0 <= self.target < len(self.tokens)


def build_prefix_probes(sequences, min_prefix: int = 1):
    """AR-native probes: for each position t>=min_prefix, condition on prefix {0..t-1}."""
    probes = []
    for seq in sequences:
        seq = tuple(seq)
        for t in range(min_prefix, len(seq)):
            probes.append(ProbeContext(seq, tuple(range(t)), t))
    return probes


def build_random_mask_probes(sequences, reveal_frac=0.5, n_per_seq=4, seed=0):
    """DLM-native probes: reveal a random subset, predict one held-out position."""
    rng = np.random.default_rng(seed)
    probes = []
    for seq in sequences:
        seq = tuple(seq)
        L = len(seq)
        for _ in range(n_per_seq):
            k = max(1, int(round(reveal_frac * L)))
            revealed = rng.choice(L, size=min(k, L - 1), replace=False)
            remaining = [p for p in range(L) if p not in set(revealed)]
            target = int(rng.choice(remaining))
            probes.append(ProbeContext(seq, tuple(int(x) for x in revealed), target))
    return probes


class ModelAdapter:
    """Interface both Toy and HF adapters implement."""
    n_layers: int
    hidden_dim: int
    vocab_size: int

    def predict_dist(self, ctx: ProbeContext) -> np.ndarray:  # (vocab,)
        raise NotImplementedError

    def hidden_states(self, ctx: ProbeContext) -> np.ndarray:  # (n_layers, hidden_dim)
        raise NotImplementedError

    # convenience: stack over many probes
    def collect(self, probes):
        dists = np.stack([self.predict_dist(p) for p in probes])      # (N, vocab)
        reps = np.stack([self.hidden_states(p) for p in probes])      # (N, n_layers, d)
        return dists, reps


# --------------------------------------------------------------------------- #
# Toy adapters — deterministic, CPU-only, for pipeline validation.
# Both toy models share a hidden "ground-truth" embedding table E (the shared
# "kernel"). ToyDLM reads it through a random *orthogonal* basis change Q; ToyAR
# reads it directly. So the pipeline SHOULD report: low Procrustes residual,
# high CKA -> i.e. the toy world satisfies the strong hypothesis by construction.
# This is a sanity oracle, not a result.
# --------------------------------------------------------------------------- #
class _ToyWorld:
    def __init__(self, vocab=64, dim=32, n_layers=4, seed=0):
        rng = np.random.default_rng(seed)
        self.vocab, self.dim, self.n_layers = vocab, dim, n_layers
        self.E = rng.standard_normal((vocab, dim))          # shared kernel
        self.unembed = rng.standard_normal((dim, vocab))
        # a fixed random orthogonal basis change for the DLM "view"
        Q, _ = np.linalg.qr(rng.standard_normal((dim, dim)))
        self.Q = Q

    def _context_vector(self, ctx: ProbeContext) -> np.ndarray:
        if not ctx.revealed:
            return np.zeros(self.dim)
        return self.E[[ctx.tokens[p] for p in ctx.revealed]].mean(axis=0)


class _ToyAdapter(ModelAdapter):
    def __init__(self, world: _ToyWorld, basis: np.ndarray):
        self._w = world
        self._basis = basis  # identity for AR, Q for DLM
        self.n_layers, self.hidden_dim, self.vocab_size = (
            world.n_layers, world.dim, world.vocab,
        )

    def hidden_states(self, ctx):
        h = self._w._context_vector(ctx) @ self._basis           # change of basis
        # cheap per-layer transform so layers differ but stay tied to the kernel
        return np.stack([np.tanh(h * (1 + 0.1 * l)) for l in range(self.n_layers)])

    def predict_dist(self, ctx):
        h = self._w._context_vector(ctx)                         # predict in kernel space
        logits = h @ self._w.unembed
        e = np.exp(logits - logits.max())
        return e / e.sum()


def make_toy_pair(seed=0):
    """Returns (ar_adapter, dlm_adapter) sharing one kernel; DLM uses an orthogonal basis."""
    w = _ToyWorld(seed=seed)
    ar = _ToyAdapter(w, np.eye(w.dim))
    dlm = _ToyAdapter(w, w.Q)
    return ar, dlm
