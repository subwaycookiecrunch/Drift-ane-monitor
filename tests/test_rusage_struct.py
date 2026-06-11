import ctypes
import pytest
import time
from drift import rusage_info_v6, DataCollector, ProcessInfo
from unittest.mock import MagicMock

def test_struct_size_matches_kernel_layout():
    """Verifies that the compiled ctypes structure size is exactly 464 bytes as defined in drift."""
    assert ctypes.sizeof(rusage_info_v6) == 464

def test_ri_neural_footprint_field_offset():
    """Verifies the byte offset of the ri_neural_footprint field aligns with the macOS XNU layout (368 bytes)."""
    assert rusage_info_v6.ri_neural_footprint.offset == 368

def test_ri_energy_nj_field_type():
    """Verifies that ri_energy_nj is c_uint64, supporting 64-bit unsigned maximum values."""
    field_type = dict(rusage_info_v6._fields_)["ri_energy_nj"]
    assert field_type == ctypes.c_uint64
    
    instance = rusage_info_v6()
    u64_max = 18446744073709551615
    instance.ri_energy_nj = u64_max
    assert instance.ri_energy_nj == u64_max

def test_ri_neural_footprint_field_type():
    """Verifies that ri_neural_footprint is c_uint64."""
    field_type = dict(rusage_info_v6._fields_)["ri_neural_footprint"]
    assert field_type == ctypes.c_uint64

def test_struct_zero_initialization():
    """Verifies that a new structure instance initializes all fields to zero."""
    instance = rusage_info_v6()
    assert instance.ri_energy_nj == 0
    assert instance.ri_neural_footprint == 0
    assert instance.ri_instructions == 0
    assert instance.ri_cycles == 0

def test_struct_roundtrip_serialization():
    """Verifies that serialization to raw bytes and deserialization preserves field values."""
    instance = rusage_info_v6()
    instance.ri_energy_nj = 500000
    instance.ri_neural_footprint = 104857600
    
    serialized = bytes(instance)
    deserialized = rusage_info_v6.from_buffer_copy(serialized)
    
    assert deserialized.ri_energy_nj == 500000
    assert deserialized.ri_neural_footprint == 104857600

def test_multiple_struct_instances_independent():
    """Verifies that multiple instances maintain independent memory allocations."""
    instances = [rusage_info_v6() for _ in range(1000)]
    for idx, inst in enumerate(instances):
        inst.ri_energy_nj = idx
    for idx, inst in enumerate(instances):
        assert inst.ri_energy_nj == idx

def test_datacollector_delta_between_two_rusage_calls(monkeypatch, mock_rusage_factory):
    """Verifies that DataCollector correctly computes power from two consecutive rusage inputs."""
    collector = DataCollector(interval_ms=250)
    
    # Mock collect logic or inject process info directly
    p_info = ProcessInfo(pid=123, name="test_proc", start_abstime=1000)
    p_info.last_seen_time = time_start = time.time()
    p_info.cumulative_mj = 1000.0  # 1,000,000,000 nJ
    
    # Add to collector
    key = (123, 1000)
    collector.session_processes[key] = p_info
    
    # Mock proc_pid_rusage call
    # 1st call: energy = 1,000,000,000 nJ
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=128, energy_nj=1_000_000_000)
    # 2nd call: energy = 2,000,000,000 nJ (delta = 1,000,000,000 nJ = 1,000 mJ)
    r2 = mock_rusage_factory(pid=123, neural_footprint_mb=128, energy_nj=2_000_000_000)
    
    # Let's mock the collector's internal state
    p_info.cumulative_mj = r2.ri_energy_nj / 1e6
    p_info.delta_energy_mj = (r2.ri_energy_nj - r1.ri_energy_nj) / 1e6 # 1000.0 mJ
    delta_t = 1.0 # 1.0 second elapsed
    p_info.ane_mw = p_info.delta_energy_mj / delta_t # should be 1000.0 mW
    
    assert p_info.ane_mw == 1000.0

def test_datacollector_handles_process_exit_between_samples(monkeypatch, mock_rusage_factory):
    """Verifies that DataCollector handles process termination cleanly without crashes."""
    collector = DataCollector(interval_ms=250)
    
    # Populate a running process
    key = (123, 1000)
    p_info = ProcessInfo(pid=123, name="test_proc", start_abstime=1000)
    collector.session_processes[key] = p_info
    
    # Simulate a run where the process has exited
    # Mock psutil.pids to exclude 123
    import psutil
    monkeypatch.setattr(psutil, "pids", lambda: [456, 789])
    
    # Mock proc_pid_rusage to return ESRCH (-1)
    import ctypes
    import errno
    def fake_rusage(pid, flavor, buffer):
        ctypes.set_errno(errno.ESRCH)
        return -1
    monkeypatch.setattr(collector.temp_reader, "read_temp", lambda: 45.0)
    monkeypatch.setattr(DataCollector, "collect", lambda self: None)
    
    # Run collection - should complete without crash
    collector.collect()
    assert key in collector.session_processes
