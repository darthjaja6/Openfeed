#!/usr/bin/env python3
"""Train the v0 Tc surrogate on JARVIS-SC with a grouped, honest split and
report held-out MAE + calibration. Saves the ensemble for oracle use.

  python scripts/02_train_tc_surrogate.py
"""
from __future__ import annotations

import numpy as np

from htsgen.config import PATHS
from htsgen.data.datasets import load_jarvis_supercon
from htsgen.models.tc_ensemble import TcEnsemble, featurize


def main() -> None:
    PATHS.ensure()
    X, y, groups = [], [], []
    for structure, tc in load_jarvis_supercon():
        try:
            X.append(featurize(structure))
            y.append(tc)
            # group by chemical system to prevent same-system leakage
            groups.append("-".join(sorted({str(e) for e in
                                           structure.composition.elements})))
        except Exception:
            continue
    X, y = np.array(X), np.array(y)
    print(f"featurized {len(X)} structures")

    from sklearn.model_selection import GroupShuffleSplit
    gss = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=0)
    tr, te = next(gss.split(X, y, groups))

    model = TcEnsemble().fit(X[tr], y[tr])
    preds = model.predict(X[te])
    mu = np.array([p.tc_mean for p in preds])
    sd = np.array([p.tc_std for p in preds])
    mae = float(np.abs(mu - y[te]).mean())
    # crude calibration: fraction of truths inside +-2 sigma
    cover = float(np.mean(np.abs(mu - y[te]) <= 2 * sd + 1e-9))
    print(f"held-out (grouped by chemsys): MAE = {mae:.2f} K | "
          f"2-sigma coverage = {cover:.2%} | OOD threshold = {model.ood_threshold:.3f}")

    model.save(PATHS.tc_model_dir)
    print(f"saved -> {PATHS.tc_model_dir}")


if __name__ == "__main__":
    main()
