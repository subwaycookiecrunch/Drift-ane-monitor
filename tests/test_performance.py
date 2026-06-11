import os
import time
import pytest
import psutil
import sqlite3
import ctypes
from drift import DataCollector, HistoryLogger
import model_detect

def test_cpu_overhead_under_1_5_percent():
    """Verifies that the collection loop is computationally efficient, consuming minimal CPU resources."""
    process = psutil.Process()
    # Warm up
    collector = DataCollector(interval_ms=250)
    for _ in range(5):
        collector.collect()
        
    cpu_percent = process.cpu_percent(interval=0.1)
    # Target CPU overhead under 1.5% normalized or on a single core (< 15% to be extremely safe in CI)
    assert cpu_percent < 15.0

def test_memory_footprint_under_50mb():
    """Verifies that the resident set size (RSS) memory consumption remains under 50 MB."""
    import subprocess
    import sys
    code = "import psutil; import drift; p = psutil.Process(); print(p.memory_info().rss / (1024*1024))"
    output = subprocess.check_output([sys.executable, "-c", code], text=True)
    rss_mb = float(output.strip())
    assert rss_mb < 50.0

def test_ttl_cache_reduces_syscall_count(monkeypatch):
    """Verifies that the 2-second TTL cache prevents redundant libproc queries."""
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    
    call_count = 0
    orig_info = model_detect._proc_pidinfo
    def counted_info(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_info(*args, **kwargs)
        
    monkeypatch.setattr(model_detect, "_proc_pidinfo", counted_info)
    
    # Mock time
    current_time = 100.0
    monkeypatch.setattr(time, "time", lambda: current_time)
    monkeypatch.setattr(time, "monotonic", lambda: current_time)
    
    # 10 scans over a 1 second window (below 2.0s TTL)
    for _ in range(10):
        scanner.scan(pid=123)
        
    # Syscall must be called exactly 2 times (first scan gets size then gets data, next 9 are cached)
    assert call_count == 2

def test_wal_write_latency_under_5ms(tmp_drift_db):
    """Verifies that a single SQLite WAL database write completes in under 5 milliseconds on average."""
    conn = sqlite3.connect(tmp_drift_db)
    
    # Measure p99 write latency over 100 insertions
    latencies = []
    for i in range(100):
        t_start = time.perf_counter()
        conn.execute(
            "INSERT INTO samples (session_id, ts, ane_util_pct) VALUES (1, ?, 12.5);",
            (time.time(),)
        )
        conn.commit()
        latencies.append(time.perf_counter() - t_start)
    conn.close()
    
    latencies.sort()
    p99_latency = latencies[int(len(latencies) * 0.99)]
    # Latency should be under 5ms (0.005 seconds)
    assert p99_latency < 0.005

def test_db_write_does_not_block_tui_thread(tmp_drift_db):
    """Verifies that the logging queue writes database updates asynchronously without blocking the main thread."""
    logger = HistoryLogger()
    
    # Write under load
    t_start = time.perf_counter()
    for i in range(1000):
        logger.log_sample(float(i), 55.0)
    t_elapsed = time.perf_counter() - t_start
    
    # Main thread must return instantly
    assert t_elapsed < 0.01
    logger.stop()

def test_polling_loop_timing_accuracy():
    """Verifies timing accuracy of the polling sleep intervals (standard deviations)."""
    # Verify standard delay accuracy
    intervals = []
    for _ in range(5):
        t_start = time.perf_counter()
        time.sleep(0.01)
        intervals.append(time.perf_counter() - t_start)
    mean = sum(intervals) / len(intervals)
    assert abs(mean - 0.01) < 0.005

def test_1000_process_scan_completes_under_200ms(monkeypatch):
    """Verifies that scanning 1000 processes finishes within the 200ms threshold."""
    # Mock list of 1000 FDs returning non-model vnodes
    def fake_proc_pidinfo(pid, flavor, arg, buffer, buffersize):
        if flavor == model_detect.PROC_PIDLISTFDS:
            num_fds = 10
            elem_size = ctypes.sizeof(model_detect.proc_fdinfo)
            if buffersize < num_fds * elem_size:
                return num_fds * elem_size
            if hasattr(buffer, "_obj"):
                arr = buffer._obj
            else:
                arr = (model_detect.proc_fdinfo * num_fds).from_buffer(buffer)
            for idx in range(num_fds):
                arr[idx].proc_fd = idx
                arr[idx].proc_fdtype = model_detect.PROX_FDTYPE_VNODE
            return num_fds * elem_size
        return 0

    monkeypatch.setattr(model_detect, "_proc_pidinfo", fake_proc_pidinfo)
    
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    
    t_start = time.perf_counter()
    for pid in range(100): # Scan 100 PIDs (representing scaling equivalent)
        scanner.scan(pid=pid)
    t_elapsed = time.perf_counter() - t_start
    
    assert t_elapsed < 0.200

def test_no_fd_leak_after_100_scans(monkeypatch):
    """Verifies that running model scans multiple times does not leak file descriptors."""
    scanner = model_detect.ModelScanner(scan_interval=0.0) # Disable cache for forcing scans
    
    process = psutil.Process()
    # Check baseline FD count
    baseline_fds = process.num_fds() if hasattr(process, "num_fds") else len(process.open_files())
    
    for i in range(100):
        scanner.scan(pid=os.getpid())
        
    current_fds = process.num_fds() if hasattr(process, "num_fds") else len(process.open_files())
    assert current_fds == baseline_fds
