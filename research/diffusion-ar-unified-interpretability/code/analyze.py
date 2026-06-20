"""Mechanize the verdict: turn a results JSON from run_alignment into the
linear-vs-nonlinear judgment defined in GOAL.md / 01-goal.md.

This is the "判定" step made deterministic, so once real-model results land the
conclusion is not eyeballed. Thresholds are conservative defaults; tune in config.

verdict ∈ {STRONG_LINEAR, PARTIAL_NONLINEAR, FALSIFIED, INCONCLUSIVE}
"""
from __future__ import annotations

import argparse
import json


# decision thresholds (relative to the within-paradigm baseline where noted)
CFG = dict(
    cka_strong=0.90,          # linear_cka at/above this => orthogonal-ish alignment
    procrustes_strong=0.15,   # residual at/below this => linear basis suffices
    kernel_gap_nonlinear=0.20,  # kernel_cka - linear_cka above this => nonlinear-only
    rq1_baseline_ratio=2.0,   # cross JS must be within this x of AR-vs-AR baseline
    layer_frac=0.5,           # fraction of layers that must pass for a global call
)


def _per_layer_call(cross_layer: dict, cfg=CFG) -> str:
    cka = cross_layer["linear_cka"]
    proc = cross_layer["procrustes_residual"]
    kcka = cross_layer["kernel_cka"]
    if cka >= cfg["cka_strong"] and proc <= cfg["procrustes_strong"]:
        return "STRONG_LINEAR"
    if (kcka - cka) >= cfg["kernel_gap_nonlinear"] and kcka >= cfg["cka_strong"]:
        return "PARTIAL_NONLINEAR"
    # a low-rank linear map rescuing it also counts as linear
    rd = cross_layer.get("rank_distortion", {})
    if rd:
        lo_ranks = sorted(int(r) for r in rd)[: max(1, len(rd) // 3)]
        if any(rd[str(r)] if str(r) in rd else rd[r] <= cfg["procrustes_strong"]
               for r in lo_ranks):
            return "STRONG_LINEAR"
    if kcka < 0.5 and cka < 0.5:
        return "FALSIFIED"
    return "INCONCLUSIVE"


def verdict(results: dict, cfg=CFG) -> dict:
    rq2 = results["rq2"]["cross_ar_vs_dlm"]
    calls = {l: _per_layer_call(v, cfg) for l, v in rq2.items()}
    n = len(calls)
    frac = lambda label: sum(c == label for c in calls.values()) / n if n else 0.0

    # RQ1 gate: are the conditionals functionally close to the baseline?
    js_cross = results["rq1"]["cross_ar_vs_dlm"]["js_mean"]
    js_base = results["rq1"]["baseline_ar_vs_ar2"]["js_mean"]
    rq1_ok = js_cross <= cfg["rq1_baseline_ratio"] * max(js_base, 1e-9)

    if frac("STRONG_LINEAR") >= cfg["layer_frac"] and rq1_ok:
        v = "STRONG_LINEAR"
    elif frac("FALSIFIED") >= cfg["layer_frac"] or not rq1_ok:
        v = "FALSIFIED"
    elif (frac("STRONG_LINEAR") + frac("PARTIAL_NONLINEAR")) >= cfg["layer_frac"]:
        v = "PARTIAL_NONLINEAR"
    else:
        v = "INCONCLUSIVE"

    return {
        "verdict": v,
        "rq1_functional_match": rq1_ok,
        "rq1_js_cross": js_cross,
        "rq1_js_baseline": js_base,
        "layer_calls": calls,
        "fractions": {k: frac(k) for k in
                      ["STRONG_LINEAR", "PARTIAL_NONLINEAR", "FALSIFIED", "INCONCLUSIVE"]},
        "interpretation": {
            "STRONG_LINEAR": "同一内核、线性/正交换基 —— 强假设成立。进入完整 RQ2 + RQ3。",
            "PARTIAL_NONLINEAR": "同一内核但换基非线性 —— 部分成功;'基'框架需放宽。",
            "FALSIFIED": "无低维对齐、分布不匹配 —— DLM 与 AR 机制上是不同内核。",
            "INCONCLUSIVE": "信号混杂 —— 加大样本/换模型对/逐层细看。",
        }[v],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results_json")
    args = ap.parse_args()
    with open(args.results_json) as f:
        res = json.load(f)
    print(json.dumps(verdict(res), ensure_ascii=False, indent=2))
