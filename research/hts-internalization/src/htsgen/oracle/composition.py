"""Composition-level checks: SMACT charge neutrality + electronegativity balance.

This is the cheapest gate (ms per structure) and one of the two constraints we
claim can be internalized *exactly* (tier 1 of the internalization ladder).
"""
from __future__ import annotations

from functools import lru_cache

from pymatgen.core import Composition


@lru_cache(maxsize=100_000)
def _smact_validity_cached(comp_str: str) -> bool:
    import smact
    from smact.screening import pauling_test

    comp = Composition(comp_str)
    el_amt = comp.get_el_amt_dict()
    symbols = tuple(sorted(el_amt))
    counts = tuple(int(round(el_amt[s])) for s in symbols)
    if 0 in counts:
        return False
    # Single elements are trivially neutral.
    if len(symbols) == 1:
        return True

    space = smact.element_dictionary(symbols)
    elements = [space[s] for s in symbols]
    electronegs = [e.pauling_eneg for e in elements]
    ox_combos = [e.oxidation_states for e in elements]
    # If any element lacks tabulated electronegativity (noble gases etc.),
    # fall back to charge-only test.
    has_eneg = all(x is not None for x in electronegs)

    from itertools import product

    from smact import neutral_ratios

    for ox_states in product(*ox_combos):
        # smact>=3 returns a list of neutral ratio tuples; older versions
        # returned (exists, ratios).
        res = neutral_ratios(ox_states, stoichs=[[c] for c in counts])
        neutral = bool(res[0]) if isinstance(res, tuple) and len(res) == 2 \
            and isinstance(res[0], bool) else bool(res)
        if not neutral:
            continue
        if not has_eneg:
            return True
        if pauling_test(ox_states, electronegs, symbols=symbols):
            return True
    return False


def charge_neutral_and_sane(comp: Composition) -> bool:
    """SMACT validity check as used by CDVAE/MatterGen evaluation suites."""
    try:
        return _smact_validity_cached(comp.reduced_formula)
    except Exception:
        # Elements outside SMACT's table (heavy actinides, etc.): be conservative.
        return False
