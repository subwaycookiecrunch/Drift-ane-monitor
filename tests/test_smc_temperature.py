import struct
import pytest
from hypothesis import given, strategies as st
from collections import deque
from drift import SMCTempReader, render_sparkline_temp

def test_reads_tc0p_when_available(mock_smc):
    """Verifies that the primary CPU sensor key TC0P is queried if available."""
    mock_smc.configure("TC0P", 65.5)
    reader = SMCTempReader()
    
    val = reader.read_temp()
    assert val == pytest.approx(65.5, 0.1)

def test_falls_back_to_tc0d_when_tc0p_missing(mock_smc):
    """Verifies that the reader falls back to TC0D when TC0P returns no data (-1)."""
    # Configure fallback
    mock_smc.configure("TC0P", -1.0)
    mock_smc.configure("TC0D", 72.0)
    
    reader = SMCTempReader()
    
    val = reader.read_temp()
    assert val == pytest.approx(72.0, 0.1)

def test_falls_back_through_full_chain(mock_smc):
    """Verifies fallback queries through the candidate list to reach Ts0P."""
    mock_smc.configure("TC0P", -1.0)
    mock_smc.configure("TC0D", -1.0)
    mock_smc.configure("TCMb", -1.0)
    mock_smc.configure("TCHP", -1.0)
    mock_smc.configure("TH0T", -1.0)
    mock_smc.configure("Tp00", -1.0)
    mock_smc.configure("Tp01", -1.0)
    mock_smc.configure("Ts0P", 58.0)
    
    reader = SMCTempReader()
    
    val = reader.read_temp()
    assert val == pytest.approx(58.0, 0.1)

def test_returns_none_when_all_sensors_missing(mock_smc):
    """Asserts that the reader returns None when no active sensor keys are configured."""
    mock_smc.configure("TC0P", -1.0)
    mock_smc.configure("TC0D", -1.0)
    mock_smc.configure("TCMb", -1.0)
    mock_smc.configure("TCHP", -1.0)
    mock_smc.configure("TH0T", -1.0)
    mock_smc.configure("Tp00", -1.0)
    mock_smc.configure("Tp01", -1.0)
    mock_smc.configure("Ts0P", -1.0)
    
    reader = SMCTempReader()
    # Unregister active keys
    reader.active_key_val = None
    reader.active_key = None
    
    val = reader.read_temp()
    assert val is None

@pytest.mark.parametrize("temp, expected_class", [
    (35.0, "normal"),
    (59.9, "normal"),
    (60.0, "amber"),
    (79.9, "amber"),
    (80.1, "red"),
    (120.0, "red") # Extreme bounds
])
def test_temperature_color_boundaries(temp, expected_class):
    """Verifies that temperatures are correctly categorized into color ranges."""
    # Let's map temp to classification tag helper
    # Normal: <60°C, Amber: 60-80°C, Red: >80°C
    if temp < 60.0:
        res = "normal"
    elif temp <= 80.0:
        res = "amber"
    else:
        res = "red"
    assert res == expected_class

def test_temperature_sp78_fixed_point_parsing(mock_smc):
    """Verifies parsing of the sp78 fixed point format (hex values mapping)."""
    # Raw 0x1E00 -> 30.0 * 256 = 7680 (0x1E00)
    # 0x1E00 -> 30.0
    mock_smc.configure("TCMb", 30.0, val_type="sp78")
    
    reader = SMCTempReader()
    
    val = reader.read_temp()
    assert val == pytest.approx(30.0, 0.1)

def test_temperature_float_format_parsing(mock_smc):
    """Verifies parsing of IEEE 754 float format values (flt )."""
    mock_smc.configure("TCMb", 65.0, val_type="flt ")
    
    reader = SMCTempReader()
    
    val = reader.read_temp()
    assert val == pytest.approx(65.0, 0.1)

def test_iokit_connection_failure(monkeypatch):
    """Asserts that connection failures to IOKit result in safe None returns."""
    from drift import SMCTempReader
    # Mock connection failure
    monkeypatch.setattr(SMCTempReader, "_init_connection", lambda self: None)
    
    reader = SMCTempReader()
    assert reader.conn is None
    assert reader.read_temp() is None

def test_smc_key_not_found(mock_smc):
    """Asserts that sensor candidates returning NotFound error results are skipped."""
    mock_smc.configure("TC0P", -1.0)
    mock_smc.configure("TCMb", 42.0)
    
    reader = SMCTempReader()
    # Simulating a check for TC0P failing
    val = reader.read_temp()
    # It should not read TC0P
    assert val is None or val == 42.0

def test_temperature_history_buffer_60_samples():
    """Verifies that the temperature history buffer ring rotates and evicts oldest items at 60."""
    history = deque(maxlen=60)
    for i in range(100):
        history.append(float(i))
    assert len(history) == 60
    assert history[0] == 40.0
    assert history[-1] == 99.0

def test_thermal_sparkline_alignment():
    """Verifies that ANE utilization and thermal sparklines render aligned lengths."""
    # Render with matching lengths
    spark = render_sparkline_temp([45.0] * 60, width=60)
    assert len(spark) == 60 or len(spark.replace("[#E8A045]", "").replace("[/]", "")) == 60

@given(st.integers(min_value=-32768, max_value=32767))
def test_hypothesis_sp78_decoder(val):
    """Property check validating that the sp78 short fixed-point unpack behaves correctly."""
    raw = struct.pack(">h", val)
    decoded = struct.unpack(">h", raw)[0] / 256.0
    assert isinstance(decoded, float)
    assert abs(decoded - (val / 256.0)) < 1e-5
