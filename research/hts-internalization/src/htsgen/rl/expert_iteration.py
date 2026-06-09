"""Expert iteration ("filter & distill" / RAFT / STaR-for-crystals):

    round k:  sample N from model_k  ->  oracle scores  ->  select top-q by
              reward (with dedup + diversity guard)  ->  fine-tune model_k on
              the selected set  ->  model_{k+1}; measure raw-sample compliance
              of model_{k+1} (the paper's per-round curve).

This is internalization step 1 — the verifier appears only at training time;
all reported metrics are on raw unguided samples. DDPO/GRPO is the upgrade
path once this loop is stable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pymatgen.core import Structure

from htsgen.oracle.oracle import Oracle, compliance_table, save_reports, table_markdown
from htsgen.rl.reward import RewardConfig, reward
from htsgen.sampling import mattergen as mg


@dataclass
class RoundConfig:
    n_samples: int = 4096
    batch_size: int = 64
    top_quantile: float = 0.10        # keep best 10% by reward
    min_keep: int = 128
    max_per_formula: int = 5          # diversity guard vs mode collapse
    workdir: Path = Path("runs/expert_iter")


def dedup_and_cap(scored: list[tuple[Structure, float]], max_per_formula: int
                  ) -> list[tuple[Structure, float]]:
    from collections import defaultdict
    by_formula: dict[str, list] = defaultdict(list)
    for s, r in sorted(scored, key=lambda x: -x[1]):
        f = s.composition.reduced_formula
        if len(by_formula[f]) < max_per_formula:
            by_formula[f].append((s, r))
    out = [item for items in by_formula.values() for item in items]
    return sorted(out, key=lambda x: -x[1])


def run_round(round_idx: int, oracle: Oracle, reward_cfg: RewardConfig,
              cfg: RoundConfig, checkpoint: Path | None = None) -> dict:
    rdir = Path(cfg.workdir) / f"round-{round_idx:02d}"
    sample_dir = rdir / "samples"
    rdir.mkdir(parents=True, exist_ok=True)

    # 1. raw, unguided sampling from current model
    mg.sample(sample_dir, n_samples=cfg.n_samples, batch_size=cfg.batch_size,
              checkpoint_path=checkpoint)
    structures = mg.load_cifs(sample_dir, limit=cfg.n_samples)
    print(f"[round {round_idx}] loaded {len(structures)} structures")

    # 2. oracle scoring — this IS the per-round compliance measurement
    reports = oracle.evaluate_many(structures)
    save_reports(reports, rdir / "reports.json")
    table = compliance_table(reports)
    (rdir / "compliance.md").write_text(table_markdown(
        table, f"Raw-sample compliance — round {round_idx}"))
    (rdir / "compliance.json").write_text(json.dumps(table, indent=1))
    print(table_markdown(table))

    # 3. reward + selection
    scored = [(s, reward(rep, reward_cfg)) for s, rep in zip(structures, reports)]
    scored = [x for x in scored if x[1] > 0]
    scored = dedup_and_cap(scored, cfg.max_per_formula)
    k = max(cfg.min_keep, int(len(structures) * cfg.top_quantile))
    selected = [s for s, _ in scored[:k]]
    print(f"[round {round_idx}] selected {len(selected)} / {len(structures)}")

    # 4. export fine-tune dataset; the actual fine-tune is invoked by
    #    runpod/run_phase1.sh (needs the mattergen checkout + GPU).
    mg.export_finetune_dataset(selected, rdir / "finetune_data")
    return {"round": round_idx, "table": table, "n_selected": len(selected),
            "finetune_data": str(rdir / "finetune_data")}
