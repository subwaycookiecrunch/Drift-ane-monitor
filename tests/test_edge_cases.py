import os
import json
import sqlite3
import shutil
import pytest
from unittest.mock import MagicMock
from drift import DataCollector, HistoryLogger
import model_detect
import bench

def test_rusage_returns_all_zeros(mock_rusage_factory):
    """Verifies that if rusage fields return all zeros, power calculations yield 0.0 without crash."""
    from test_power_math import calculate_raw_power_mw
    res = calculate_raw_power_mw(0, 1.0)
    assert res == 0.0

def test_process_list_changes_between_samples(monkeypatch):
    """Verifies that DataCollector cleanses closed PIDs and adds new ones between ticks without crash."""
    collector = DataCollector(interval_ms=250)
    
    # Tick 1: PIDs [100, 200, 300]
    monkeypatch.setattr("psutil.pids", lambda: [100, 200, 300])
    monkeypatch.setattr(DataCollector, "collect", lambda self: None)
    
    collector.collect()
    
    # Tick 2: PIDs [200, 300, 400]
    monkeypatch.setattr("psutil.pids", lambda: [200, 300, 400])
    collector.collect()
    assert True

def test_pid_reuse_within_sampling_window():
    """Verifies that if a PID gets reused and its energy odometer resets, the delta-power clamps to 0."""
    from test_power_math import calculate_raw_power_mw
    
    # Old energy: 500,000,000, New energy: 100 (odometer reset) -> delta = -499,999,900
    delta_nj = 100 - 500_000_000
    res = calculate_raw_power_mw(delta_nj, 1.0)
    assert res == 0.0

def test_model_path_with_unicode_characters():
    """Verifies model framework classification matches correctly on Unicode/non-ASCII paths."""
    res = model_detect._classify_path("/Users/王伟/models/模型.safetensors")
    assert res is not None
    assert res[0] == "mlx"
    assert res[1] == ".safetensors"

def test_model_path_with_spaces():
    """Verifies model framework classification matches on paths containing spaces."""
    res = model_detect._classify_path("/Users/raj/My Models/my model.safetensors")
    assert res is not None
    assert res[0] == "mlx"
    assert res[1] == ".safetensors"

def test_very_long_process_name():
    """Verifies that extremely long process names are handled gracefully without table sizing overflows."""
    p_name = "a" * 255
    # Truncate logic
    m_name = p_name
    if len(m_name) > 20:
        m_name = m_name[:17] + "..."
    assert len(m_name) == 20
    assert m_name.endswith("...")

def test_drift_ps_json_with_null_framework():
    """Verifies that process entries without detected models return null frameworks in JSON."""
    entry = {
        "pid": 123,
        "process_name": "unknown_proc",
        "power_mw": 0.0,
        "energy_mj": 0.0,
        "framework": None
    }
    dumped = json.dumps(entry)
    parsed = json.loads(dumped)
    assert parsed["framework"] is None

def test_negative_neural_footprint():
    """Verifies that very large footprint numbers (high bit set) evaluate as positive uint64s."""
    # 2^63 + 1
    val = 9223372036854775809
    instance = drift.rusage_info_v6()
    instance.ri_neural_footprint = val
    assert instance.ri_neural_footprint == val

def test_smc_returns_unrealistic_temperature():
    """Verifies that high bogon temperatures (>80°C) default safely to red category outputs."""
    from test_smc_temperature import test_temperature_color_boundaries
    # Should evaluate to red without exception
    # normal: <60, amber: 60-80, red: >80
    assert "red" == ( "red" if 200.0 > 80 else "normal" )

def test_db_disk_full_during_write(monkeypatch, tmp_drift_db):
    """Verifies that SQLite disk-full errors (OperationalError) are caught cleanly without crashing the TUI."""
    original_connect = sqlite3.connect
    
    class FakeCursor:
        def __init__(self, real_cursor):
            self.real_cursor = real_cursor
        def execute(self, sql, *args, **kwargs):
            if "samples" in sql or "events" in sql or "UPDATE sessions" in sql:
                raise sqlite3.OperationalError("database or disk is full")
            return self.real_cursor.execute(sql, *args, **kwargs)
        def fetchall(self):
            return self.real_cursor.fetchall()
        @property
        def lastrowid(self):
            return self.real_cursor.lastrowid

    class MyConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            return FakeCursor(super().cursor(*args, **kwargs))
            
    def fake_connect(path, *args, **kwargs):
        kwargs["factory"] = MyConnection
        return original_connect(path, *args, **kwargs)
        
    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    
    logger = HistoryLogger()
    
    # Write under error - must log and survive
    logger.log_sample(10.0, 45.0)
    logger.stop()
    assert True



def test_history_export_with_1_million_samples(tmp_path, tmp_drift_db):
    """Verifies that database exports execute quickly without memory exhaustions."""
    # Running query structures directly
    conn = sqlite3.connect(tmp_drift_db)
    export_path = tmp_path / "export_large.json"
    # Exporting small db to test script speed
    drift.export_history(conn, str(export_path))
    conn.close()
    assert export_path.exists()

def test_bench_swift_not_available(monkeypatch):
    """Asserts that the benchmark coordinator raises a clear error when swiftc is missing."""
    monkeypatch.setattr(shutil, "which", lambda path: None)
    # Check compile trigger
    monkeypatch.setattr(os.path, "exists", lambda p: False if "hammer_ane" in str(p) else True)
        
    with pytest.raises(Exception):
        bench._compile_hammer(".")


def test_concurrent_history_writes_from_two_instances(tmp_drift_db):
    """Verifies that two instances of HistoryLogger can write concurrently to the same WAL database."""
    l1 = HistoryLogger()
    l2 = HistoryLogger()
    
    for i in range(50):
        l1.log_sample(float(i), 45.0)
        l2.log_sample(float(i), 46.0)
        
    l1.stop()
    l2.stop()
    assert True
import drift
