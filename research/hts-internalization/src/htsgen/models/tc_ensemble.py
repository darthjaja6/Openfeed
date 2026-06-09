"""v0 Tc surrogate: Magpie-style composition + cheap structure features,
HistGradientBoosting deep-ish ensemble, kNN feature-space OOD detector.

Design requirements from the plan:
 - predicts log(Tc+1) trained on first-principles (Allen-Dynes) labels
   (JARVIS-SC `Tc_supercon`, optionally Cerqueira/Alexandria EPC sets);
 - MUST expose uncertainty (ensemble std) and OOD distance — these gate the
   reward during RL (tier-3 ceiling) and allocate the DFPT budget;
 - v0 is deliberately simple/fast; the upgrade path is an equivariant GNN
   (BETE-NET-style) trained on the same splits.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pymatgen.core import Composition, Structure

# ---------------------------------------------------------------- features

_EL_PROPS = ("Z", "X", "atomic_mass", "atomic_radius_calculated", "row", "group")


def _element_vector(el) -> np.ndarray:
    vals = []
    for p in _EL_PROPS:
        v = getattr(el, p, None)
        vals.append(float(v) if v is not None else np.nan)
    return np.array(vals, dtype=float)


def composition_features(comp: Composition) -> np.ndarray:
    """Weighted mean/std/min/max element-property stats + light-element flags."""
    els = list(comp.get_el_amt_dict().items())
    fracs = np.array([a for _, a in els], dtype=float)
    fracs = fracs / fracs.sum()
    mat = np.stack([_element_vector(Composition(sym).elements[0]) for sym, _ in els])
    mat = np.nan_to_num(mat, nan=0.0)

    mean = (fracs[:, None] * mat).sum(0)
    std = np.sqrt((fracs[:, None] * (mat - mean) ** 2).sum(0))
    mn, mx = mat.min(0), mat.max(0)

    symbols = {sym for sym, _ in els}
    light = float(sum(a for s, a in els if s in {"H", "B", "C", "N", "O"}) / fracs.size)
    extras = np.array([
        len(els),
        comp.num_atoms,
        light,
        float("H" in symbols),
        float(bool(symbols & {"B", "C", "N"})),
    ])
    return np.concatenate([mean, std, mn, mx, extras])


def structure_features(structure: Structure, symprec: float = 0.1) -> np.ndarray:
    from htsgen.oracle.geometry import min_interatomic_distance, volume_per_atom
    from htsgen.oracle.symmetry import detect_spacegroup

    spg = detect_spacegroup(structure, symprec)["spacegroup_number"]
    return np.array([
        volume_per_atom(structure),
        structure.density,
        min_interatomic_distance(structure),
        len(structure),
        spg,
    ])


def featurize(structure: Structure) -> np.ndarray:
    return np.concatenate([
        composition_features(structure.composition),
        structure_features(structure),
    ])


def featurize_composition_only(comp: Composition) -> np.ndarray:
    """For datasets without structures; structure block is zero-padded."""
    return np.concatenate([composition_features(comp), np.zeros(5)])


# ---------------------------------------------------------------- model

@dataclass
class TcPrediction:
    tc_mean: float        # K
    tc_std: float         # K (ensemble spread, propagated through expm1)
    ood_distance: float   # kNN distance in standardized feature space
    is_ood: bool


class TcEnsemble:
    """Ensemble of HistGradientBoostingRegressor on log1p(Tc)."""

    def __init__(self, n_members: int = 8, ood_quantile: float = 0.95,
                 random_state: int = 0):
        self.n_members = n_members
        self.ood_quantile = ood_quantile
        self.random_state = random_state
        self.members: list = []
        self.scaler = None
        self.knn = None
        self.ood_threshold: float = np.inf

    # ---------- training ----------
    def fit(self, X: np.ndarray, tc_k: np.ndarray) -> "TcEnsemble":
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.neighbors import NearestNeighbors
        from sklearn.preprocessing import StandardScaler

        y = np.log1p(np.clip(tc_k, 0, None))
        rng = np.random.default_rng(self.random_state)
        self.members = []
        for i in range(self.n_members):
            idx = rng.choice(len(X), size=len(X), replace=True)  # bagging
            m = HistGradientBoostingRegressor(
                max_iter=400, learning_rate=0.06, max_depth=None,
                l2_regularization=1e-2, random_state=self.random_state + i)
            m.fit(X[idx], y[idx])
            self.members.append(m)

        self.scaler = StandardScaler().fit(X)
        Xs = self.scaler.transform(X)
        self.knn = NearestNeighbors(n_neighbors=5).fit(Xs)
        d, _ = self.knn.kneighbors(Xs)
        self.ood_threshold = float(np.quantile(d.mean(1), self.ood_quantile))
        return self

    # ---------- inference ----------
    def predict(self, X: np.ndarray) -> list[TcPrediction]:
        preds_log = np.stack([m.predict(X) for m in self.members])  # (M, N)
        tc = np.expm1(preds_log)
        mean, std = tc.mean(0), tc.std(0)

        Xs = self.scaler.transform(X)
        d, _ = self.knn.kneighbors(Xs)
        dist = d.mean(1)
        return [TcPrediction(float(m), float(s), float(dd), bool(dd > self.ood_threshold))
                for m, s, dd in zip(mean, std, dist)]

    def predict_structure(self, structure: Structure) -> TcPrediction:
        return self.predict(featurize(structure)[None, :])[0]

    # ---------- persistence ----------
    def save(self, dirpath: Path) -> None:
        import joblib
        dirpath = Path(dirpath)
        dirpath.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"members": self.members, "scaler": self.scaler, "knn": self.knn},
            dirpath / "model.joblib")
        (dirpath / "meta.json").write_text(json.dumps({
            "n_members": self.n_members,
            "ood_quantile": self.ood_quantile,
            "ood_threshold": self.ood_threshold,
        }))

    @classmethod
    def load(cls, dirpath: Path) -> "TcEnsemble":
        import joblib
        dirpath = Path(dirpath)
        meta = json.loads((dirpath / "meta.json").read_text())
        obj = cls(n_members=meta["n_members"], ood_quantile=meta["ood_quantile"])
        blob = joblib.load(dirpath / "model.joblib")
        obj.members = blob["members"]
        obj.scaler = blob["scaler"]
        obj.knn = blob["knn"]
        obj.ood_threshold = meta["ood_threshold"]
        return obj
