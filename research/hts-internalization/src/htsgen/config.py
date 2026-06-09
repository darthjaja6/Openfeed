"""Central thresholds and paths.

Every cutoff used anywhere in the project lives here so that the paper's
methods section can cite a single source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("HTSGEN_DATA_DIR", Path.home() / ".htsgen"))


@dataclass(frozen=True)
class OracleThresholds:
    # Geometry: CDVAE convention is 0.5 A; we report both the loose literature
    # gate and a stricter physical gate.
    min_distance_loose: float = 0.5
    min_distance_strict: float = 0.9
    max_volume_per_atom: float = 100.0  # A^3/atom; metals/covalent solids sanity bound
    min_volume_per_atom: float = 4.0

    # Stability (MLIP, then DFT for the shortlist).
    e_hull_metastable: float = 0.10  # eV/atom — standard metastability gate
    e_hull_stable: float = 0.0

    # Phonon gate: Gamma-point modes more imaginary than this fail.
    # (3 acoustic modes ~0 are excluded; tolerance absorbs numerical noise.)
    gamma_imaginary_tol: float = -0.03  # eV (ASE vibrations report meV-scale energies)

    # Tc surrogate.
    tc_threshold_k: float = 5.0       # candidate gate, matches Guided-Diffusion's DFT bar
    tc_ood_quantile: float = 0.95     # kNN distance beyond this train quantile = OOD

    # Small-cell constraint that keeps end-stage DFPT affordable.
    max_sites: int = 12

    # Symmetry: P1 (spg #1) raw samples are allowed but reported; symmetrized
    # detection tolerance for spglib.
    symprec: float = 0.1


@dataclass
class Paths:
    data_dir: Path = DATA_DIR
    mp_entries: Path = field(default_factory=lambda: DATA_DIR / "mp_entries.json.gz")
    jarvis_sc: Path = field(default_factory=lambda: DATA_DIR / "jarvis_supercon.json")
    tc_model_dir: Path = field(default_factory=lambda: DATA_DIR / "tc_surrogate")
    samples_dir: Path = field(default_factory=lambda: DATA_DIR / "samples")
    reports_dir: Path = field(default_factory=lambda: DATA_DIR / "reports")

    def ensure(self) -> "Paths":
        for p in (self.data_dir, self.tc_model_dir, self.samples_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


THRESHOLDS = OracleThresholds()
PATHS = Paths()
