"""Tier-1 oracle + reward + Tc-surrogate unit tests (no MLIP/network needed)."""
import numpy as np
import pytest
from pymatgen.core import Composition, Lattice, Structure

from htsgen.oracle.composition import charge_neutral_and_sane
from htsgen.oracle.geometry import geometry_ok, min_interatomic_distance
from htsgen.oracle.oracle import Oracle, compliance_table
from htsgen.oracle.symmetry import detect_spacegroup
from htsgen.rl.reward import RewardConfig, reward


def rocksalt(a=5.69, els=("Na", "Cl")) -> Structure:
    return Structure.from_spacegroup("Fm-3m", Lattice.cubic(a), els,
                                     [[0, 0, 0], [0.5, 0.5, 0.5]])


def mgb2() -> Structure:
    lat = Lattice.hexagonal(3.086, 3.524)
    return Structure(lat, ["Mg", "B", "B"],
                     [[0, 0, 0], [1 / 3, 2 / 3, 0.5], [2 / 3, 1 / 3, 0.5]])


def garbage() -> Structure:
    # two atoms nearly on top of each other
    return Structure(Lattice.cubic(3.0), ["Fe", "Fe"],
                     [[0, 0, 0], [0.05, 0, 0]])


# ---------------- composition ----------------

def test_charge_neutrality_known_good():
    for f in ("NaCl", "MgB2", "TiO2", "BaTiO3"):
        assert charge_neutral_and_sane(Composition(f)), f


def test_charge_neutrality_known_bad():
    assert not charge_neutral_and_sane(Composition("Na3Cl7"))


# ---------------- geometry ----------------

def test_min_distance_and_geometry():
    s = rocksalt()
    assert min_interatomic_distance(s) == pytest.approx(5.69 / 2, rel=1e-3)
    assert geometry_ok(s, min_dist=0.5, vpa_min=4, vpa_max=100)["pass"]
    g = geometry_ok(garbage(), min_dist=0.5, vpa_min=4, vpa_max=100)
    assert not g["pass"] and g["min_distance"] < 0.5


# ---------------- symmetry ----------------

def test_spacegroup_detection():
    assert detect_spacegroup(rocksalt())["spacegroup_number"] == 225
    assert detect_spacegroup(mgb2())["spacegroup_number"] == 191


# ---------------- oracle composition ----------------

def test_oracle_tier1_and_table():
    oracle = Oracle()
    reports = oracle.evaluate_many([rocksalt(), mgb2(), garbage()], progress=False)
    assert reports[0].tier1_pass and reports[1].tier1_pass
    assert not reports[2].tier1_pass  # geometry fails
    t = compliance_table(reports)
    assert t["n_samples"] == 3
    assert t["geometry"] == pytest.approx(2 / 3)
    assert t["error_rate"] == 0.0


# ---------------- reward ----------------

def test_reward_hard_zero_on_tier1_violation():
    oracle = Oracle()
    bad = oracle.evaluate(garbage())
    good = oracle.evaluate(mgb2())
    cfg = RewardConfig()
    assert reward(bad, cfg) == 0.0
    assert reward(good, cfg) > 0.0


def test_reward_ood_gating():
    oracle = Oracle()
    rep = oracle.evaluate(mgb2())
    rep.tc_mean, rep.tc_std, rep.tc_ood = 30.0, 5.0, True
    cfg = RewardConfig(w_tc=1.0, ood_gate=True)
    r_gated = reward(rep, cfg)
    rep.tc_ood = False
    r_open = reward(rep, cfg)
    assert r_open > r_gated  # OOD gate removes the Tc term


# ---------------- Tc surrogate ----------------

def test_tc_ensemble_train_predict_roundtrip(tmp_path):
    from htsgen.models.tc_ensemble import TcEnsemble, featurize

    rng = np.random.default_rng(0)
    structs = [rocksalt(a) for a in np.linspace(4.5, 6.5, 40)] + \
              [mgb2() for _ in range(10)]
    X = np.array([featurize(s) for s in structs])
    y = rng.uniform(0, 30, size=len(X))

    m = TcEnsemble(n_members=3).fit(X, y)
    p = m.predict_structure(mgb2())
    assert 0 <= p.tc_mean < 100 and p.tc_std >= 0

    m.save(tmp_path / "tc")
    m2 = TcEnsemble.load(tmp_path / "tc")
    p2 = m2.predict_structure(mgb2())
    assert p2.tc_mean == pytest.approx(p.tc_mean)
    assert p2.is_ood == p.is_ood
