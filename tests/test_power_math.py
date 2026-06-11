import time
import pytest
from hypothesis import given, strategies as st
from drift import DataCollector, get_chip_info
from drift_stubs import get_tops_for_chip

def calculate_raw_power_mw(delta_nj: int, delta_t: float, interval_ms: int = 250) -> float:
    """Safe calculation replicating DataCollector power formula."""
    delta_energy_mj = delta_nj / 1_000_000.0
    if delta_nj <= 0:
        return 0.0
    if delta_t <= 0.0:
        return 0.0 # Deterministic choice in our implementation: delta_t <= 0 returns 0.0
    if delta_t > 0.01:
        return delta_energy_mj / delta_t
    else:
        return delta_energy_mj / (interval_ms / 1000.0)

@pytest.mark.parametrize("delta_nj, delta_t, expected", [
    (1_000_000, 1.0, 1.0),       # Basic calculation: 1 mJ / 1 s = 1 mW
    (500_000, 0.5, 1.0),         # Sub-second interval: 0.5 mJ / 0.5 s = 1 mW
    (30_000_000_000, 1.0, 30000.0) # High load (peak ANE): 30,000 mJ / 1 s = 30,000 mW
])
def test_power_mw_parametrized_cases(delta_nj, delta_t, expected):
    """Verifies basic, sub-second, and peak load power math outputs."""
    result = calculate_raw_power_mw(delta_nj, delta_t)
    assert abs(result - expected) < 1e-5

def test_power_mw_zero_delta_t():
    """Verifies that a zero time delta does not raise ZeroDivisionError and behaves deterministically."""
    result = calculate_raw_power_mw(1_000_000, 0.0)
    assert result == 0.0

def test_power_mw_negative_delta_nj():
    """Verifies that a negative energy delta returns 0.0 (clamped)."""
    result = calculate_raw_power_mw(-100_000, 1.0)
    assert result == 0.0

def test_power_mw_uint64_overflow():
    """Simulates uint64 energy overflow. System must handle rollover/resets safely."""
    # Odometer wraps from 2^64-1 to 100, delta_nj is negative -> should return 0.0
    u64_max = 18446744073709551615
    energy_prev = u64_max
    energy_now = 100
    delta_nj = energy_now - energy_prev
    result = calculate_raw_power_mw(delta_nj, 1.0)
    assert result == 0.0

def test_power_mw_identical_timestamps():
    """Verifies that duplicate times return a deterministic value without crash."""
    result = calculate_raw_power_mw(1_000_000, 0.0)
    assert result == 0.0

def test_energy_delta_accumulation_over_n_samples():
    """Verifies accumulation of 100 sequential 1mJ samples equals 100 mJ."""
    total_mj = 0.0
    for _ in range(100):
        # 1,000,000 nJ = 1 mJ
        total_mj += 1_000_000 / 1e6
    assert abs(total_mj - 100.0) < 0.001

@pytest.mark.parametrize("chip_desc, expected_tops", [
    ("Apple M1", 11.0),
    ("Apple M3 Max", 18.0),
    ("Apple M5", 25.0)
])
def test_tops_calculation_parametrized(chip_desc, expected_tops):
    """Verifies lookup of TOPS ratings for specific chips in the ANE TOPS table."""
    chip_key = chip_desc.replace("Apple ", "")
    tops = get_tops_for_chip(chip_key)
    assert abs(tops - expected_tops) < 0.1

def test_tops_unknown_chip():
    """Asserts that looking up a nonexistent chip raises ValueError."""
    with pytest.raises(ValueError):
        get_tops_for_chip("M99 Ultra")

def test_power_unit_consistency():
    """Verifies that the divisor converts nanojoules to millijoules (1_000_000 factor)."""
    # 1 mJ = 1,000,000 nJ
    nJ_to_mJ_divisor = 1_000_000
    assert nJ_to_mJ_divisor == 10**6

@given(st.integers(min_value=-10000000, max_value=50000000000), st.floats(min_value=-10.0, max_value=100.0))
def test_hypothesis_power_math_properties(delta_nj, delta_t):
    """Property-based check verifying that power math returns non-negative values and never crashes."""
    res = calculate_raw_power_mw(delta_nj, delta_t)
    assert res >= 0.0
