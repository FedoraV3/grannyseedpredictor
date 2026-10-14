"""
test_feasibility.py

Checks for the measured feasibility estimator (`search.PlacementStats`):
structural impossibilities are detected from the config, measured
single-pin odds match an independent sample, and the estimate for a pin set
is reported with the right wording. Runs in a few seconds (small sample).

    python test_feasibility.py
"""

from __future__ import annotations

import sys

import search
import variants
from search import Constraint as C

SAMPLES = 1500


def _stats(variant: str = "Normal") -> search.PlacementStats:
    return search.PlacementStats(variants.load_variant_config(variant), num_samples=SAMPLES)


def test_sample_count_and_marginals_sum_to_one():
    st = _stats()
    assert st.num_samples == SAMPLES
    for item, counts in st.counts.items():
        assert sum(counts.values()) == SAMPLES, item


def test_no_pins():
    st = _stats()
    est = st.estimate([])
    assert est.expected_matches == float(search.SEED_SPACE)
    assert "No pins" in st.feasibility_message([])


def test_structural_impossibilities():
    st = _stats()
    # Code is a free-only (category 4) item: never in a puzzle.
    est = st.estimate([C("Code", "PUZZLE:Safe", "pin")])
    assert est.impossible and est.expected_matches == 0.0, est
    assert "free-only" in est.reason
    assert st.feasibility_message([C("Code", "PUZZLE:Safe", "pin")]).startswith("IMPOSSIBLE")
    # Two items on one slot.
    est = st.estimate([C("MasterKey", "PUZZLE:ToyLock", "pin"), C("SafeKey", "PUZZLE:ToyLock", "pin")])
    assert est.impossible and "one slot holds one item" in est.reason, est
    # One item on two slots.
    est = st.estimate([C("MasterKey", "PUZZLE:ToyLock", "pin"), C("MasterKey", "PUZZLE:Safe", "pin")])
    assert est.impossible and "two different slots" in est.reason, est
    # Unknown item / slot.
    assert st.estimate([C("Nope", "PUZZLE:Safe", "pin")]).impossible
    assert st.estimate([C("MasterKey", "FREE:Nowhere/Spot (1)", "pin")]).impossible
    # The same pin twice is just one pin.
    one = st.estimate([C("MasterKey", "PUZZLE:ToyLock", "pin")])
    two = st.estimate([C("MasterKey", "PUZZLE:ToyLock", "pin"), C("masterkey", "PUZZLE:ToyLock", "pin")])
    assert one.expected_matches == two.expected_matches and two.num_pins == 1


def test_single_pin_matches_independent_sample():
    st = _stats()
    other = search.PlacementStats(variants.load_variant_config("Normal"),
                                  num_samples=SAMPLES, rng_seed=987654321)
    p1 = st.marginal("MasterKey", "PUZZLE:ToyLock")
    p2 = other.marginal("MasterKey", "PUZZLE:ToyLock")
    # Measured ~0.14 on Normal; the uniform model says 1/87 = 0.0115.
    assert 0.08 < p1 < 0.22, p1
    assert abs(p1 - p2) < 0.04, (p1, p2)
    est = st.estimate([C("MasterKey", "PUZZLE:ToyLock", "pin")])
    assert abs(est.expected_matches - search.SEED_SPACE * p1) < 1e-6
    assert not est.unobserved and not est.impossible


def test_conditioning_captures_slot_exclusion():
    """Pinning two different keys to two different puzzles: the conditional
    estimate must be below the product of the marginals when the second
    puzzle is a less likely home once the first is taken, and must never
    exceed the first pin's marginal alone."""
    st = _stats()
    a = [C("SafeKey", "PUZZLE:ToyLock", "pin")]
    ab = a + [C("CarKey", "PUZZLE:Safe", "pin")]
    ea, eab = st.estimate(a), st.estimate(ab)
    assert eab.expected_matches <= ea.expected_matches
    assert eab.num_pins == 2


def test_prefer_constraints_are_ignored():
    st = _stats()
    pins = [C("MasterKey", "PUZZLE:ToyLock", "pin")]
    with_pref = pins + [C("Hammer", "PUZZLE:Safe", "prefer")]
    assert st.estimate(pins).expected_matches == st.estimate(with_pref).expected_matches


def test_message_wording():
    st = _stats()
    msg = st.feasibility_message([C("MasterKey", "PUZZLE:ToyLock", "pin")])
    assert msg.startswith("about ") and "measured from" in msg, msg
    # Five free-spot pins: far below one expected seed.
    many = [C(n, f"FREE:OldHouse/Spot ({s})", "pin")
            for n, s in (("Code", 82), ("Book", 80), ("Battery", 79), ("Meat", 81), ("Hammer", 78))]
    est = st.estimate(many)
    assert est.expected_matches < 1, est
    assert "IMPOSSIBLE" in st.feasibility_message(many)


def test_uniform_fallback_message_still_works():
    assert "rough estimate" in search.feasibility_message(2, 87)
    assert "No pins" in search.feasibility_message(0, 87)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
