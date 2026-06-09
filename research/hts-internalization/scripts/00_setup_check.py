#!/usr/bin/env python3
"""Smoke-test the environment: which oracle tiers are available on this machine."""
from __future__ import annotations

import importlib.util


def have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def main() -> None:
    core = {m: have(m) for m in ("numpy", "pymatgen", "smact", "spglib", "ase",
                                 "sklearn", "pandas")}
    mlip = {m: have(m) for m in ("torch", "mace")}
    extra = {m: have(m) for m in ("phonopy", "jarvis", "mp_api", "joblib")}

    print("core (tier-1 oracle + Tc surrogate):", core)
    print("mlip (tier-2 stability/phonons):   ", mlip)
    print("extras:                            ", extra)

    if all(core.values()):
        from pymatgen.core import Lattice, Structure
        from htsgen.oracle import Oracle

        nacl = Structure(Lattice.cubic(5.69), ["Na", "Cl"] * 4,
                         [[0, 0, 0], [.5, .5, .5], [.5, .5, 0], [0, 0, .5],
                          [.5, 0, .5], [0, .5, 0], [0, .5, .5], [.5, 0, 0]])
        rep = Oracle().evaluate(nacl)
        assert rep.charge_neutral and rep.geometry_pass, rep
        print("tier-1 oracle self-test: OK  (NaCl ->", rep.formula,
              "spg", rep.spacegroup, ")")
    else:
        print("tier-1 oracle self-test: SKIPPED (missing core deps)")

    if all(mlip.values()):
        print("MLIP available — tier-2 enabled (first call downloads MACE-MP-0)")
    else:
        print("MLIP missing — install with: uv pip install -e '.[mlip]'")


if __name__ == "__main__":
    main()
