#!/usr/bin/env python3
"""Phase 0 deliverable: the BASELINE raw-sample compliance table.

Evaluates raw, unguided samples from a pretrained generator (MatterGen CIF dir,
or any folder of CIFs, e.g. DiffCSP output) through the oracle. This produces
the [X]% in the paper's abstract.

  python scripts/03_baseline_compliance.py --samples DIR [--mlip] [--phonons] [--tc]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from htsgen.config import PATHS
from htsgen.oracle.oracle import Oracle, compliance_table, save_reports, table_markdown
from htsgen.sampling.mattergen import load_cifs


def build_oracle(args) -> Oracle:
    oracle = Oracle(run_phonons=args.phonons)
    if args.mlip:
        from htsgen.oracle.stability import HullChecker, MLIPRelaxer
        relaxer = MLIPRelaxer(device=args.device)
        oracle.relaxer = relaxer
        if PATHS.mp_entries.exists():
            oracle.hull = HullChecker(PATHS.mp_entries, relaxer)
        else:
            print("WARN: no MP entries snapshot — E_hull disabled "
                  "(run 01_download_data.py with --mp-api-key)")
    if args.tc:
        from htsgen.models.tc_ensemble import TcEnsemble
        oracle.tc_model = TcEnsemble.load(PATHS.tc_model_dir)
    return oracle


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", required=True, help="dir of CIF/extxyz samples")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--mlip", action="store_true", help="enable tier-2 stability")
    ap.add_argument("--phonons", action="store_true", help="enable Gamma phonon gate")
    ap.add_argument("--tc", action="store_true", help="enable Tc surrogate")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--tag", default="baseline")
    args = ap.parse_args()

    PATHS.ensure()
    structures = load_cifs(Path(args.samples), limit=args.limit)
    if not structures:
        raise SystemExit(f"no structures found under {args.samples}")
    print(f"loaded {len(structures)} structures")

    oracle = build_oracle(args)
    reports = oracle.evaluate_many(structures)

    out = PATHS.reports_dir / args.tag
    save_reports(reports, out / "reports.json")
    table = compliance_table(reports)
    (out / "compliance.json").write_text(json.dumps(table, indent=1))
    md = table_markdown(table, f"Raw-sample compliance — {args.tag}")
    (out / "compliance.md").write_text(md)
    print()
    print(md)
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()
