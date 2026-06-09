"""Composite physics reward with uncertainty gating.

This is the training-time face of the oracle. Tier weights implement the
curriculum from the plan (stability first, Tc later) and the OOD gate is the
mechanism that turns "tier-3 has an information ceiling" from a claim into a
measurable design choice (gate on/off = ablation).
"""
from __future__ import annotations

from dataclasses import dataclass

from htsgen.oracle.oracle import StructureReport


@dataclass
class RewardConfig:
    w_tier1: float = 1.0          # charge neutrality + geometry + small cell
    w_symmetry: float = 0.5       # beyond-P1 bonus
    w_stability: float = 2.0      # smooth in e_hull
    w_phonon: float = 1.0
    w_tc: float = 0.0             # OFF in round 1 (curriculum); raised later
    e_hull_scale: float = 0.1     # eV/atom softness of the stability term
    tc_scale_k: float = 20.0      # K
    ood_gate: bool = True         # tier-3 reward zeroed when surrogate is OOD
    novelty_bonus: float = 0.0    # filled in by dedup stage if used


def reward(report: StructureReport, cfg: RewardConfig) -> float:
    r = 0.0
    # tier 1 — exact constraints. Hard zero if violated: invalid structures
    # must never be reinforced, whatever their (meaningless) surrogate scores.
    if not report.tier1_pass:
        return 0.0
    r += cfg.w_tier1
    if report.beyond_p1:
        r += cfg.w_symmetry

    # tier 2 — smooth stability shaping: exp(-e_hull / scale) in [0, 1].
    if report.e_above_hull is not None:
        import math
        r += cfg.w_stability * math.exp(-max(report.e_above_hull, 0.0) / cfg.e_hull_scale)
    if report.phonon_pass:
        r += cfg.w_phonon

    # tier 3 — Tc surrogate, uncertainty-gated.
    if cfg.w_tc > 0 and report.tc_mean is not None:
        gated_out = cfg.ood_gate and bool(report.tc_ood)
        if not gated_out:
            r += cfg.w_tc * min(report.tc_mean / cfg.tc_scale_k, 2.0)
    return r
