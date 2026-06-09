"""Thermodynamic stability via MLIP relaxation + energy above the MP convex hull.

Heavy deps (torch + mace) are imported lazily so the rest of the oracle works
without them. Reference hull entries come from a local Materials Project
snapshot (see htsgen.data.datasets.fetch_mp_entries).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Optional

from pymatgen.core import Structure


class MLIPRelaxer:
    """Relax a structure with MACE-MP-0 (or any ASE calculator) and return
    relaxed structure + energy per atom."""

    def __init__(self, model: str = "small", device: str = "cpu", fmax: float = 0.05,
                 max_steps: int = 200):
        self.model = model
        self.device = device
        self.fmax = fmax
        self.max_steps = max_steps
        self._calc = None

    @property
    def calc(self):
        if self._calc is None:
            from mace.calculators import mace_mp
            self._calc = mace_mp(model=self.model, device=self.device,
                                 default_dtype="float64")
        return self._calc

    def relax(self, structure: Structure) -> tuple[Structure, float, bool]:
        """Returns (relaxed structure, energy_per_atom eV, converged)."""
        from ase.filters import FrechetCellFilter
        from ase.optimize import FIRE
        from pymatgen.io.ase import AseAtomsAdaptor

        atoms = AseAtomsAdaptor.get_atoms(structure)
        atoms.calc = self.calc
        opt = FIRE(FrechetCellFilter(atoms), logfile=None)
        converged = bool(opt.run(fmax=self.fmax, steps=self.max_steps))
        e = float(atoms.get_potential_energy()) / len(atoms)
        relaxed = AseAtomsAdaptor.get_structure(atoms)
        return relaxed, e, converged


class HullChecker:
    """Energy above hull against a reference entry set (MP snapshot).

    NOTE on energy mixing: the MLIP total energy scale (MACE-MP-0 is trained on
    MP r2SCAN/PBE data) is approximately compatible with MP PBE formation
    energies; for the paper-grade numbers the shortlist is re-relaxed with DFT.
    Here we build the hull in *formation energy* space using MLIP elemental
    references computed with the same calculator, which cancels the
    calculator-specific energy zero.
    """

    def __init__(self, mp_entries_path: Path, relaxer: Optional[MLIPRelaxer] = None):
        self.mp_entries_path = Path(mp_entries_path)
        self.relaxer = relaxer
        self._pd_cache: dict[frozenset, object] = {}
        self._entries = None
        self._elemental_ref: dict[str, float] = {}

    # ---------- reference entries ----------
    @property
    def entries(self):
        if self._entries is None:
            from pymatgen.entries.computed_entries import ComputedEntry
            opener = gzip.open if self.mp_entries_path.suffix == ".gz" else open
            with opener(self.mp_entries_path, "rt") as f:
                raw = json.load(f)
            self._entries = [ComputedEntry.from_dict(d) for d in raw]
        return self._entries

    def _phase_diagram(self, elements: frozenset):
        if elements not in self._pd_cache:
            from pymatgen.analysis.phase_diagram import PhaseDiagram
            sub = [e for e in self.entries
                   if set(map(str, e.composition.elements)) <= elements]
            if not sub:
                raise ValueError(f"no reference entries for {sorted(elements)}")
            self._pd_cache[elements] = PhaseDiagram(sub)
        return self._pd_cache[elements]

    # ---------- main API ----------
    def e_above_hull(self, structure: Structure, energy_per_atom: float) -> float:
        """E_hull (eV/atom) of (structure, MLIP energy) vs the MP hull.

        We align energy scales by shifting the candidate's energy so that the
        MLIP and MP elemental references coincide (standard MLIP-hull recipe).
        """
        from pymatgen.entries.computed_entries import ComputedEntry

        elements = frozenset(map(str, structure.composition.elements))
        pd = self._phase_diagram(elements)

        shift = 0.0
        if self.relaxer is not None:
            comp = structure.composition.fractional_composition
            for el, frac in comp.get_el_amt_dict().items():
                shift += frac * (self._mp_elemental_epa(pd, el)
                                 - self._mlip_elemental_epa(el))
        e_total = (energy_per_atom + shift) * len(structure)
        entry = ComputedEntry(structure.composition, e_total)
        return float(pd.get_e_above_hull(entry, allow_negative=True))

    def _mp_elemental_epa(self, pd, el: str) -> float:
        from pymatgen.core import Composition
        ref = pd.el_refs[Composition(el).elements[0]]
        return ref.energy_per_atom

    def _mlip_elemental_epa(self, el: str) -> float:
        if el not in self._elemental_ref:
            if self.relaxer is None:
                raise RuntimeError("elemental MLIP references require a relaxer")
            from pymatgen.core import Lattice, Structure as S
            # crude elemental reference: relax a simple cubic cell; adequate for
            # the screening gate, replaced by DFT for the shortlist.
            s = S(Lattice.cubic(3.5), [el], [[0, 0, 0]])
            _, epa, _ = self.relaxer.relax(s)
            self._elemental_ref[el] = epa
        return self._elemental_ref[el]
