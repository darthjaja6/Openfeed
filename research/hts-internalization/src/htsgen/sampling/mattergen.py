"""MatterGen sampling/fine-tuning wrappers (subprocess around the official CLI).

We treat MatterGen as an external tool pinned by runpod/bootstrap.sh
(github.com/microsoft/mattergen). Two operations:

  sample(...)    -> mattergen-generate: unconditional raw samples (no guidance)
  finetune(...)  -> mattergen-train/finetune on an expert-iteration dataset

NOTE: exact CLI flags are pinned against the MatterGen version checked out in
bootstrap.sh; if upstream changes flags, fix them HERE only.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pymatgen.core import Structure


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True, cwd=cwd)


def sample(out_dir: Path, n_samples: int = 1024, batch_size: int = 64,
           model_name: str = "mattergen_base", checkpoint_path: Path | None = None) -> Path:
    """Generate raw (unguided, unfiltered) samples. Returns dir with CIFs."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    num_batches = max(1, (n_samples + batch_size - 1) // batch_size)
    cmd = ["mattergen-generate", str(out_dir),
           f"--batch_size={batch_size}", f"--num_batches={num_batches}",
           "--record_trajectories=False"]
    if checkpoint_path is not None:
        cmd.append(f"--model_path={checkpoint_path}")
    else:
        cmd.append(f"--pretrained-name={model_name}")
    run(cmd)
    return out_dir


def load_cifs(sample_dir: Path, limit: int | None = None) -> list[Structure]:
    """Load generated structures (handles both per-file CIFs and the zip/extxyz
    outputs MatterGen produces)."""
    sample_dir = Path(sample_dir)
    structures: list[Structure] = []

    cifs = sorted(sample_dir.rglob("*.cif"))
    for p in cifs:
        try:
            structures.append(Structure.from_file(p))
        except Exception:
            continue
        if limit and len(structures) >= limit:
            return structures

    if not structures:  # extxyz fallback
        try:
            from ase.io import iread
            from pymatgen.io.ase import AseAtomsAdaptor
            for xyz in sorted(sample_dir.rglob("*.extxyz")):
                for atoms in iread(xyz):
                    structures.append(AseAtomsAdaptor.get_structure(atoms))
                    if limit and len(structures) >= limit:
                        return structures
        except Exception as e:
            print(f"extxyz fallback failed: {e}", file=sys.stderr)
    return structures


def export_finetune_dataset(structures: list[Structure], out_dir: Path) -> Path:
    """Write selected structures as a MatterGen-ingestible dataset (CIF folder +
    csv manifest, consumed by mattergen's csv-to-dataset tooling in bootstrap)."""
    import csv

    out_dir = Path(out_dir)
    cif_dir = out_dir / "cifs"
    cif_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, s in enumerate(structures):
        name = f"sel-{i:06d}"
        (cif_dir / f"{name}.cif").write_text(s.to(fmt="cif"))
        rows.append({"material_id": name, "cif_file": f"cifs/{name}.cif",
                     "pretty_formula": s.composition.reduced_formula})
    with open(out_dir / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["material_id", "cif_file", "pretty_formula"])
        w.writeheader()
        w.writerows(rows)
    return out_dir
