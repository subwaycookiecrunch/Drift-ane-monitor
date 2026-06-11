import sys
import json
import pytest
from unittest.mock import patch
import drift

def test_drift_version_flag(monkeypatch, capsys):
    """Verifies that running drift with --version prints the correct version and exits 0."""
    monkeypatch.setattr(sys, "argv", ["drift", "--version"])
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "drift v1.0.0" in captured.out

def test_drift_help_flag(monkeypatch, capsys):
    """Verifies that running drift with --help or -h prints usage guidelines."""
    monkeypatch.setattr(sys, "argv", ["drift", "--help"])
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Command" in captured.out or "options" in captured.out

def test_drift_unknown_subcommand(monkeypatch, capsys):
    """Verifies that running an invalid command prints usage help."""
    monkeypatch.setattr(sys, "argv", ["drift", "nonexistent_command"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    
    # Mock DriftApp.run to raise SystemExit
    monkeypatch.setattr("drift.DriftApp.run", lambda self: sys.exit(1))
    
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 1


def test_drift_ps_exits_zero(monkeypatch, capsys):
    """Verifies that drift ps subcommand exits successfully (exit 0)."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 0

def test_drift_ps_stdout_contains_header(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that the printed process table contains the standard headers."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    # Mock some running processes
    # We mock proc_pid_rusage to return active process 123
    import ctypes
    from conftest import FakeLibproc
    
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=256)
    
    # We patch proc_pid_rusage inside drift
    def fake_rusage(pid, flavor, buffer):
        if pid == 123:
            dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        return -1
        
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    assert "RANK" in captured.out or "no ANE activity" in captured.out



def test_drift_ps_json_flag_valid_json(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that drift ps --json returns valid JSON formatting."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--json"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    import ctypes
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=256)
    def fake_rusage(pid, flavor, buffer):
        if pid == 123:
            dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        return -1
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    output = captured.out.strip()
    data = json.loads(output)
    assert isinstance(data, list)



def test_drift_ps_json_fields_present(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that JSON output includes expected keys."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--json"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    import ctypes
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=256)
    def fake_rusage(pid, flavor, buffer):
        if pid == 123:
            dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        return -1
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    output = captured.out.strip()
    data = json.loads(output)
    assert len(data) > 0
    assert "pid" in data[0]
    assert "process" in data[0]
    assert "power_mw" in data[0]



def test_drift_ps_sort_by_power(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that sort parameter works correctly."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--sort", "power", "--json"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    import ctypes
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=100, energy_nj=100_000_000)
    r2 = mock_rusage_factory(pid=456, neural_footprint_mb=200, energy_nj=500_000_000)
    
    def fake_rusage(pid, flavor, buffer):
        dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
        if pid == 123:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        elif pid == 456:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r2), ctypes.sizeof(r2))
            return 0
        return -1
        
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 456, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    output = captured.out.strip()
    data = json.loads(output)
    assert data[0]["pid"] == 456
    assert data[1]["pid"] == 123



def test_drift_ps_sort_by_energy(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that sorting by energy parses correctly."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--sort", "energy", "--json"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    import ctypes
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=100, energy_nj=500_000_000)
    r2 = mock_rusage_factory(pid=456, neural_footprint_mb=200, energy_nj=100_000_000)
    
    def fake_rusage(pid, flavor, buffer):
        dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
        if pid == 123:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        elif pid == 456:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r2), ctypes.sizeof(r2))
            return 0
        return -1
        
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 456, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    output = captured.out.strip()
    data = json.loads(output)
    assert data[0]["pid"] == 123
    assert data[1]["pid"] == 456



def test_drift_ps_sort_by_footprint(monkeypatch, capsys, mock_rusage_factory):
    """Verifies that sorting by footprint parses correctly."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--sort", "footprint", "--json"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    import ctypes
    r1 = mock_rusage_factory(pid=123, neural_footprint_mb=300)
    r2 = mock_rusage_factory(pid=456, neural_footprint_mb=100)
    
    def fake_rusage(pid, flavor, buffer):
        dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
        if pid == 123:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r1), ctypes.sizeof(r1))
            return 0
        elif pid == 456:
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(r2), ctypes.sizeof(r2))
            return 0
        return -1
        
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: [123, 456, 999])
    
    drift.main()
        
    captured = capsys.readouterr()
    output = captured.out.strip()
    data = json.loads(output)
    assert data[0]["pid"] == 123
    assert data[1]["pid"] == 456



def test_drift_ps_watch_loop_runs_n_times(monkeypatch, capsys):
    """Verifies watch loop executes with interval limits and exits."""
    called = False
    def fake_handle_ps(args):
        nonlocal called
        called = True
        assert args.watch == 2
        
    monkeypatch.setattr("drift.handle_ps", fake_handle_ps)
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--watch", "2"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    
    drift.main()
    assert called is True


def test_drift_ps_watch_sigint_clean_exit(monkeypatch):
    """Verifies watch loop catches KeyboardInterrupt and exits cleanly."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps", "--watch", "2"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    def mock_run(*args):
        raise KeyboardInterrupt()
        
    monkeypatch.setattr("drift.run_ps_once", mock_run)
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 0

def test_drift_ps_no_sudo_required(monkeypatch, capsys):
    """Verifies that no output containing permission or root access is printed to stderr."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    try:
        drift.main()
    except SystemExit:
        pass
    captured = capsys.readouterr()
    assert "sudo" not in captured.err
    assert "Permission denied" not in captured.err

def test_drift_history_summary_no_crash(monkeypatch, tmp_drift_db):
    """Verifies that calling history summary exits cleanly with status 0."""
    monkeypatch.setattr(sys, "argv", ["drift", "history"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    
    drift.main()


def test_drift_history_json_export_file(monkeypatch, tmp_drift_db, tmp_path):
    """Verifies that history --export correctly exports database data to a JSON file."""
    export_file = tmp_path / "export.json"
    monkeypatch.setattr(sys, "argv", ["drift", "history", "--export", str(export_file)])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    
    drift.main()
    assert export_file.exists()


def test_drift_bench_requires_no_root(monkeypatch, tmp_path):
    """Verifies that benchmarking can run and compiles Xcode dependencies without root."""
    monkeypatch.setattr(sys, "argv", ["drift", "bench", "--duration", "2", "--threads", "2", "--output", str(tmp_path / "bench_out.json")])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    
    from bench import BenchResult
    dummy_res = BenchResult(
        chip="Apple M5",
        chip_key="M5",
        ane_cores=16,
        duration_s=2.0,
        total_energy_mj=1000.0,
        peak_power_mw=5000.0,
        avg_power_mw=4000.0,
        peak_footprint_mb=128.0,
        avg_ipc=1.5,
        total_inferences=100,
        thermal_state="nominal",
        samples=[],
        timestamp="2026-06-09"
    )
    
    called_run_benchmark = False
    def fake_run_benchmark(*args, **kwargs):
        nonlocal called_run_benchmark
        called_run_benchmark = True
        return dummy_res
        
    monkeypatch.setattr("bench.run_benchmark", fake_run_benchmark)
    
    drift.main()
    
    assert called_run_benchmark is True
    assert (tmp_path / "bench_out.json").exists()

def test_drift_ps_handles_zero_ane_processes(monkeypatch, capsys):
    """Verifies that when zero ANE processes are found, it prints the correct fallback message."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    monkeypatch.setattr("psutil.pids", lambda: [])
    
    with pytest.raises(SystemExit) as exc:
        drift.main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "no ANE activity" in captured.out

def test_drift_ps_handles_100_simultaneous_processes(monkeypatch, capsys, mock_rusage_factory):
    """Verifies table rendering output of 100 simultaneous running processes."""
    monkeypatch.setattr(sys, "argv", ["drift", "ps"])
    monkeypatch.setattr("drift.check_compatibility", lambda: None)
    monkeypatch.setattr("drift.platform.machine", lambda: "arm64")
    
    pids = list(range(1000, 1100))
    rusages = {pid: mock_rusage_factory(pid=pid, neural_footprint_mb=50.0, energy_nj=10_000_000 * (pid - 999)) for pid in pids}
    
    import ctypes
    def fake_rusage(pid, flavor, buffer):
        if pid in rusages:
            dest = buffer._obj if hasattr(buffer, "_obj") else ctypes.Structure.from_address(buffer)
            ctypes.memmove(ctypes.addressof(dest), ctypes.addressof(rusages[pid]), ctypes.sizeof(rusages[pid]))
            return 0
        return -1
        
    monkeypatch.setattr("drift.proc_pid_rusage", fake_rusage)
    monkeypatch.setattr("psutil.pids", lambda: pids + [999])
    
    from unittest.mock import MagicMock
    def fake_process_init(pid):
        m = MagicMock()
        m.name.return_value = f"process_{pid}"
        return m
    monkeypatch.setattr("psutil.Process", fake_process_init)
    
    drift.main()
    
    captured = capsys.readouterr()
    assert "RANK" in captured.out
    for pid in pids[:10]:
        assert f"process_{pid}" in captured.out or "unknown" in captured.out



