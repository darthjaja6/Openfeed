"""Space-group detection. We report the detected space group and whether the
structure has symmetry beyond P1 — raw diffusion samples are typically P1,
symmetry-constrained generators should not be."""
from __future__ import annotations

from pymatgen.core import Structure
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer


def detect_spacegroup(structure: Structure, symprec: float = 0.1) -> dict:
    try:
        sga = SpacegroupAnalyzer(structure, symprec=symprec)
        num = sga.get_space_group_number()
        sym = sga.get_space_group_symbol()
    except Exception:
        num, sym = 1, "P1"
    return {"spacegroup_number": num, "spacegroup_symbol": sym, "beyond_p1": num > 1}
