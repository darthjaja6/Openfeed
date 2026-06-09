"""Structure-level sanity: minimum interatomic distance and cell volume bounds."""
from __future__ import annotations

import numpy as np
from pymatgen.core import Structure


def min_interatomic_distance(structure: Structure) -> float:
    """Minimum pairwise distance including periodic images (A)."""
    if len(structure) == 1:
        # Single atom: nearest periodic image distance.
        return float(min(structure.lattice.abc))
    dmat = structure.distance_matrix
    iu = np.triu_indices(len(structure), k=1)
    d = float(dmat[iu].min())
    # Also guard against an atom sitting too close to its own image.
    self_image = float(min(structure.lattice.abc))
    return min(d, self_image)


def volume_per_atom(structure: Structure) -> float:
    return float(structure.volume / len(structure))


def geometry_ok(structure: Structure, *, min_dist: float, vpa_min: float, vpa_max: float) -> dict:
    md = min_interatomic_distance(structure)
    vpa = volume_per_atom(structure)
    return {
        "min_distance": md,
        "volume_per_atom": vpa,
        "pass": (md >= min_dist) and (vpa_min <= vpa <= vpa_max),
    }
