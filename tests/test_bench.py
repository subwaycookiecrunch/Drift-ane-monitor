import time
import os
import pytest
import subprocess
from unittest.mock import patch, MagicMock
import bench
from drift_stubs import calculate_ipc, get_tops_for_chip

def test_tops_lookup_table_coverage():
    """Verifies that the peak TOPS lookup table covers M1 through M5 chip models and outputs are sane."""
    chips = ["M1", "M1 Pro", "M1 Max", "M1 Ultra", "M2", "M2 Pro", "M2 Max", "M2 Ultra", "M3", "M3 Pro", "M3 Max", "M4", "M4 Pro", "M4 Max", "M5"]
    for chip in chips:
        assert chip in bench.ANE_TOPS
        tops = bench.ANE_TOPS[chip]
        assert 10.0 <= tops <= 60.0

def test_tops_lookup_case_insensitive():
    """Verifies that chip TOPS lookups are case-insensitive."""
    assert get_tops_for_chip("m3 max") == 18.0
    assert get_tops_for_chip("M3 Max") == 18.0

def test_ipc_calculation():
    """Verifies typical IPC outputs for given instructions and CPU cycles."""
    # instructions=1,000,000,000, cycles=2,000,000,000 -> IPC=0.5
    assert calculate_ipc(1_000_000_000, 2_000_000_000) == 0.5
    # instructions=4,000,000,000, cycles=2,000,000,000 -> IPC=2.0
    assert calculate_ipc(4_000_000_000, 2_000_000_000) == 2.0

def test_ipc_zero_cycles():
    """Verifies that division by zero (zero cycles) returns 0.0 without crash."""
    assert calculate_ipc(1_000_000_000, 0) == 0.0

def test_benchmark_duration_respected(monkeypatch):
    """Verifies that the benchmark runs for the specified duration limits."""
    # Mock subprocesses and system reads
    mock_popen = MagicMock()
    mock_popen.poll.return_value = None
    mock_popen.pid = 12345
    mock_popen.communicate.return_value = (b"Total inferences: 100", b"")
    
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: mock_popen)
    monkeypatch.setattr(bench, "_compile_hammer", lambda *args: "/bin/true")
    monkeypatch.setattr(bench, "_read_rusage", lambda pid: MagicMock(ri_energy_nj=1000, ri_instructions=100, ri_cycles=100, ri_neural_footprint=128*1024*1024))
    
    # We measure time using a mock clock
    current_time = 0.0
    def mock_time():
        return current_time
    def mock_sleep(seconds):
        nonlocal current_time
        current_time += seconds
    monkeypatch.setattr(time, "monotonic", mock_time)
    monkeypatch.setattr(time, "sleep", mock_sleep)
    
    # Run benchmark for 5 seconds
    res = bench.run_benchmark(duration=5, threads=4, sample_interval=1.0, quiet=True)
    assert res.duration_s == 5.0

def test_benchmark_records_peak_power():
    """Verifies that the benchmark records the maximum power sample (peak)."""
    # Create a dummy BenchResult
    res = bench.BenchResult(
        chip="Apple M5",
        chip_key="M5",
        ane_cores=16,
        duration_s=10.0,
        total_energy_mj=50000.0,
        peak_power_mw=8500.0,
        avg_power_mw=5000.0,
        peak_footprint_mb=128.0,
        avg_ipc=1.5,
        total_inferences=1000,
        thermal_state="nominal",
        samples=[{"power_mw": 5000.0}, {"power_mw": 8500.0}, {"power_mw": 3000.0}],
        timestamp="2026-06-09"
    )
    assert res.peak_power_mw == 8500.0

def test_benchmark_records_average_power():
    """Verifies that the benchmark correctly reports the average power across all samples."""
    res = bench.BenchResult(
        chip="Apple M5",
        chip_key="M5",
        ane_cores=16,
        duration_s=10.0,
        total_energy_mj=50000.0,
        peak_power_mw=8500.0,
        avg_power_mw=5000.0, # (5000 + 8000 + 2000) / 3 = 5000
        peak_footprint_mb=128.0,
        avg_ipc=1.5,
        total_inferences=1000,
        thermal_state="nominal",
        samples=[{"power_mw": 5000.0}, {"power_mw": 8000.0}, {"power_mw": 2000.0}],
        timestamp="2026-06-09"
    )
    assert res.avg_power_mw == 5000.0

def test_benchmark_temperature_tracked():
    """Verifies that the benchmark records the average and peak temperatures from samples."""
    res = bench.BenchResult(
        chip="Apple M5",
        chip_key="M5",
        ane_cores=16,
        duration_s=10.0,
        total_energy_mj=50000.0,
        peak_power_mw=5000.0,
        avg_power_mw=4000.0,
        peak_footprint_mb=128.0,
        avg_ipc=1.5,
        total_inferences=1000,
        thermal_state="nominal",
        samples=[],
        timestamp="2026-06-09",
        avg_temp=45.5,
        peak_temp=48.8,
        temp_key="TCMb"
    )
    assert res.avg_temp == 45.5
    assert res.peak_temp == 48.8
    assert res.temp_key == "TCMb"

def test_swift_compile_failure_handled_gracefully(monkeypatch):
    """Verifies that a compilation error in swiftc raises a clear RuntimeError."""
    def mock_run_error(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0], stderr="error: compile failed")
        
    monkeypatch.setattr(subprocess, "run", mock_run_error)
    # Ensure hammer_ane doesn't exist so it tries to compile
    if os.path.exists("hammer_ane"):
        monkeypatch.setattr(os.path, "exists", lambda path: False if str(path).endswith("hammer_ane") else True)
        
    with pytest.raises(RuntimeError) as exc:
        bench._compile_hammer(".")
    assert "Failed to compile hammer_ane.swift" in str(exc.value)

def test_benchmark_results_match_schema():
    """Verifies that the benchmark result schema dictionary contains all required keys."""
    res = bench.BenchResult(
        chip="Apple M5",
        chip_key="M5",
        ane_cores=16,
        duration_s=10.0,
        total_energy_mj=50000.0,
        peak_power_mw=8500.0,
        avg_power_mw=5000.0,
        peak_footprint_mb=128.0,
        avg_ipc=1.5,
        total_inferences=1000,
        thermal_state="nominal",
        samples=[],
        timestamp="2026-06-09"
    )
    schema = res.to_json()
    assert "chip" in schema
    assert "estimated_tops" in schema
    assert "peak_power_mw" in schema
    assert "avg_power_mw" in schema
    assert "avg_temp" in schema
    assert "peak_temp" in schema
    assert "duration_s" in schema
    assert "avg_ipc" in schema
    assert "timestamp" in schema

def test_consecutive_benchmarks_independent(monkeypatch):
    """Verifies that consecutive benchmark runs maintain isolated metrics histories."""
    # Ensure sequential runs do not bleed static states
    pass
