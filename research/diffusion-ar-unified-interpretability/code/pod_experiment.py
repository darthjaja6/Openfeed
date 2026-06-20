"""Real-model experiment driver — runs ON a GPU pod. Produces the verdict.

Swaps the toy adapters for HF ARAdapter / DLMAdapter, runs RQ1 + cheap RQ2 with a
within-paradigm baseline, and prints analyze.verdict(). This is the script that
turns the goal's "判定" into an actual STRONG_LINEAR / PARTIAL_NONLINEAR / FALSIFIED
call on real DLM vs AR.

Example (on the pod):
    python pod_experiment.py \
        --dlm  Dream-org/Dream-v0-Instruct-7B  --dlm-mask "<mask>" \
        --ar   Dream-org/Dream-v0-Base-7B \
        --ar-baseline meta-llama/Llama-3.1-8B \
        --n-seq 200 --reveal 0.5 --out results.json
"""
from __future__ import annotations

import argparse
import json

import numpy as np

import analyze
from alignment import build_random_mask_probes
from run_alignment import evaluate


def load_text_sequences(tokenizer, n_seq, max_len=64, seed=0):
    """Small generic corpus -> token-id sequences. Replace with your eval set."""
    try:
        from datasets import load_dataset
        ds = load_dataset("wikitext", "wikitext-103-raw-v1", split="test")
        texts = [t for t in ds["text"] if len(t.strip()) > 80][:n_seq]
    except Exception:
        texts = ["The quick brown fox jumps over the lazy dog near the river bank."] * n_seq
    seqs = []
    for t in texts:
        ids = tokenizer(t, add_special_tokens=False)["input_ids"][:max_len]
        if len(ids) >= 8:
            seqs.append(tuple(ids))
    return seqs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dlm", required=True)
    ap.add_argument("--dlm-mask", default=None)
    ap.add_argument("--ar", required=True)
    ap.add_argument("--ar-baseline", default=None,
                    help="second AR (same tokenizer) for the within-paradigm baseline")
    ap.add_argument("--n-seq", type=int, default=200)
    ap.add_argument("--reveal", type=float, default=0.5)
    ap.add_argument("--out", default="results.json")
    args = ap.parse_args()

    from models_hf import ARAdapter, DLMAdapter

    ar = ARAdapter(args.ar)
    dlm = DLMAdapter(args.dlm, mask_token=args.dlm_mask)
    assert ar.tok.get_vocab() == dlm.tok.get_vocab(), "tokenizer mismatch — see README"

    ar2 = ARAdapter(args.ar_baseline) if args.ar_baseline else ar  # degrade: self-baseline
    if args.ar_baseline is None:
        print("WARNING: no --ar-baseline; baseline is trivially self -> RQ1 gate weak.")

    seqs = load_text_sequences(ar.tok, args.n_seq)
    probes = build_random_mask_probes(seqs, reveal_frac=args.reveal, n_per_seq=3)
    print(f"{len(seqs)} sequences -> {len(probes)} probes")

    res = evaluate(ar, dlm, ar2, probes)
    with open(args.out, "w") as f:
        json.dump(res, f, indent=2)

    v = analyze.verdict(res)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    print("\n=== VERDICT:", v["verdict"], "===")


if __name__ == "__main__":
    main()
