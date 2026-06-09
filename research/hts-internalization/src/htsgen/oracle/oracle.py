"""The verifier oracle: one interface shared by training (reward), evaluation
(raw-sample compliance tables) and DFT-budget allocation.

Tiered execution — cheap gates always run; expensive gates (MLIP relax/hull,
Gamma-phonon, Tc) run only if enabled and only on structures that passed the
cheap gates, mirroring how the reward is computed during RL.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from pymatgen.core import Structure

from htsgen.config import THRESHOLDS, OracleThresholds
from htsgen.oracle.composition import charge_neutral_and_sane
from htsgen.oracle.geometry import geometry_ok
from htsgen.oracle.symmetry import detect_spacegroup


@dataclass
class StructureReport:
    formula: str
    n_sites: int
    # tier-1 (exact constraints)
    charge_neutral: bool = False
    geometry_pass: bool = False
    min_distance: float = 0.0
    volume_per_atom: float = 0.0
    spacegroup: int = 1
    beyond_p1: bool = False
    small_cell: bool = False
    # tier-2 (statistical constraints)
    mlip_converged: Optional[bool] = None
    e_above_hull: Optional[float] = None
    hull_pass: Optional[bool] = None
    phonon_min_mode_ev: Optional[float] = None
    phonon_pass: Optional[bool] = None
    # tier-3 (capped constraint)
    tc_mean: Optional[float] = None
    tc_std: Optional[float] = None
    tc_ood: Optional[bool] = None
    tc_pass: Optional[bool] = None
    error: Optional[str] = None

    @property
    def tier1_pass(self) -> bool:
        return self.charge_neutral and self.geometry_pass and self.small_cell

    def as_dict(self) -> dict:
        d = asdict(self)
        d["tier1_pass"] = self.tier1_pass
        return d


@dataclass
class Oracle:
    thresholds: OracleThresholds = field(default_factory=lambda: THRESHOLDS)
    relaxer: Optional[object] = None       # htsgen.oracle.stability.MLIPRelaxer
    hull: Optional[object] = None          # htsgen.oracle.stability.HullChecker
    tc_model: Optional[object] = None      # htsgen.models.tc_ensemble.TcEnsemble
    run_phonons: bool = False

    def evaluate(self, structure: Structure) -> StructureReport:
        t = self.thresholds
        rep = StructureReport(formula=structure.composition.reduced_formula,
                              n_sites=len(structure))
        try:
            # ---- tier 1: exact, cheap ----
            rep.charge_neutral = charge_neutral_and_sane(structure.composition)
            g = geometry_ok(structure, min_dist=t.min_distance_loose,
                            vpa_min=t.min_volume_per_atom,
                            vpa_max=t.max_volume_per_atom)
            rep.geometry_pass = g["pass"]
            rep.min_distance = g["min_distance"]
            rep.volume_per_atom = g["volume_per_atom"]
            spg = detect_spacegroup(structure, t.symprec)
            rep.spacegroup = spg["spacegroup_number"]
            rep.beyond_p1 = spg["beyond_p1"]
            rep.small_cell = len(structure) <= t.max_sites

            if not (rep.charge_neutral and rep.geometry_pass):
                return rep

            # ---- tier 2: MLIP stability ----
            relaxed = structure
            if self.relaxer is not None:
                relaxed, epa, conv = self.relaxer.relax(structure)
                rep.mlip_converged = conv
                if self.hull is not None:
                    eh = self.hull.e_above_hull(relaxed, epa)
                    rep.e_above_hull = eh
                    rep.hull_pass = eh < t.e_hull_metastable
                if self.run_phonons and rep.hull_pass:
                    from htsgen.oracle.phonons import gamma_point_check
                    ph = gamma_point_check(relaxed, self.relaxer.calc,
                                           t.gamma_imaginary_tol)
                    rep.phonon_min_mode_ev = ph["min_mode_ev"]
                    rep.phonon_pass = ph["pass"]

            # ---- tier 3: Tc surrogate (uncertainty-gated) ----
            if self.tc_model is not None:
                p = self.tc_model.predict_structure(relaxed)
                rep.tc_mean, rep.tc_std, rep.tc_ood = p.tc_mean, p.tc_std, p.is_ood
                rep.tc_pass = (p.tc_mean >= t.tc_threshold_k) and not p.is_ood
        except Exception as e:  # a single bad structure must not kill a batch
            rep.error = f"{type(e).__name__}: {e}"
        return rep

    def evaluate_many(self, structures: list[Structure], progress: bool = True
                      ) -> list[StructureReport]:
        it = structures
        if progress:
            try:
                from tqdm import tqdm
                it = tqdm(structures, desc="oracle")
            except ImportError:
                pass
        return [self.evaluate(s) for s in it]


# ------------------------------------------------------------ reporting

def compliance_table(reports: list[StructureReport]) -> dict:
    """The paper's headline numbers: per-constraint raw-sample compliance."""
    n = len(reports)
    if n == 0:
        return {}

    def frac(pred) -> float:
        return sum(1 for r in reports if pred(r)) / n

    table = {
        "n_samples": n,
        "charge_neutral": frac(lambda r: r.charge_neutral),
        "geometry": frac(lambda r: r.geometry_pass),
        "beyond_p1_symmetry": frac(lambda r: r.beyond_p1),
        "small_cell(<=12)": frac(lambda r: r.small_cell),
        "tier1_all": frac(lambda r: r.tier1_pass),
    }
    evaluated_hull = [r for r in reports if r.hull_pass is not None]
    if evaluated_hull:
        table["e_hull<0.1 (of all)"] = frac(lambda r: bool(r.hull_pass))
        table["median_e_hull(eV/atom)"] = float(
            sorted(r.e_above_hull for r in evaluated_hull)[len(evaluated_hull) // 2])
    evaluated_ph = [r for r in reports if r.phonon_pass is not None]
    if evaluated_ph:
        table["phonon_gamma (of all)"] = frac(lambda r: bool(r.phonon_pass))
    evaluated_tc = [r for r in reports if r.tc_pass is not None]
    if evaluated_tc:
        table["tc>=5K & in-dist (of all)"] = frac(lambda r: bool(r.tc_pass))
    table["error_rate"] = frac(lambda r: r.error is not None)
    return table


def table_markdown(table: dict, title: str = "Raw-sample compliance") -> str:
    lines = [f"### {title}", "", "| metric | value |", "|---|---|"]
    for k, v in table.items():
        lines.append(f"| {k} | {v:.4f} |" if isinstance(v, float) else f"| {k} | {v} |")
    return "\n".join(lines)


def save_reports(reports: list[StructureReport], path: Path) -> None:
    import json
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.as_dict() for r in reports], indent=1))
