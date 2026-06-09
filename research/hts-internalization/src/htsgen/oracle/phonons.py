"""Fast dynamical-stability gate: Gamma-point finite-difference modes with the MLIP.

A full phonopy supercell run is the slow-mode option (run_phonopy=True);
the Gamma-point gate catches a large share of soft-mode instabilities at a
fraction of the cost and is what we use inside training loops. Both are
documented limitations vs DFPT, which the shortlist receives.
"""
from __future__ import annotations

import numpy as np
from pymatgen.core import Structure


def gamma_point_check(structure: Structure, calc, imaginary_tol_ev: float = -0.03) -> dict:
    """Finite-displacement Gamma-point vibrational check using an ASE calculator.

    Returns dict with min mode energy (eV; imaginary modes reported negative)
    and pass/fail. The 3 acoustic translational modes are excluded.
    """
    import tempfile

    from ase.vibrations import Vibrations
    from pymatgen.io.ase import AseAtomsAdaptor

    atoms = AseAtomsAdaptor.get_atoms(structure)
    atoms.calc = calc
    with tempfile.TemporaryDirectory() as td:
        vib = Vibrations(atoms, name=f"{td}/vib", delta=0.01)
        vib.run()
        energies = vib.get_energies()  # complex array, eV
        vib.clean()

    # Signed energies: imaginary modes -> negative.
    signed = np.where(np.abs(energies.imag) > 1e-8, -np.abs(energies.imag),
                      energies.real)
    signed = np.sort(signed)
    # Drop the 3 lowest-|e| modes as acoustic at Gamma.
    order = np.argsort(np.abs(signed))
    optical = np.delete(signed, order[:3])
    min_mode = float(optical.min()) if len(optical) else 0.0
    return {"min_mode_ev": min_mode, "pass": min_mode > imaginary_tol_ev}


def phonopy_supercell_check(structure: Structure, calc, supercell=(2, 2, 2),
                            displacement: float = 0.01,
                            imaginary_tol_thz: float = -0.1) -> dict:
    """Full finite-displacement phonon band check via phonopy (slow mode)."""
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms
    from pymatgen.io.ase import AseAtomsAdaptor

    ph_atoms = PhonopyAtoms(symbols=[str(s.specie) for s in structure],
                            scaled_positions=structure.frac_coords,
                            cell=structure.lattice.matrix)
    phonon = Phonopy(ph_atoms, supercell_matrix=np.diag(supercell))
    phonon.generate_displacements(distance=displacement)

    forces = []
    for sc in phonon.supercells_with_displacements:
        atoms = AseAtomsAdaptor.get_atoms(
            Structure(sc.cell, sc.symbols, sc.scaled_positions))
        atoms.calc = calc
        forces.append(atoms.get_forces())
    phonon.forces = np.array(forces)
    phonon.produce_force_constants()

    mesh = [8, 8, 8]
    phonon.run_mesh(mesh)
    freqs = phonon.get_mesh_dict()["frequencies"]  # THz
    min_freq = float(np.min(freqs))
    return {"min_freq_thz": min_freq, "pass": min_freq > imaginary_tol_thz}
