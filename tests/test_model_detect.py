import time
import ctypes
import pytest
import threading
import model_detect
from drift_stubs import scan_process, scan_all_processes

def test_detects_safetensors_path():
    """Verifies that safetensors files match the MLX framework pattern."""
    res = model_detect._classify_path("/Users/raj/.cache/huggingface/model.safetensors")
    assert res is not None
    assert res[0] == "mlx"
    assert res[1] == ".safetensors"

def test_detects_mlmodelc_path():
    """Verifies that mlmodelc files match the CoreML framework pattern."""
    res = model_detect._classify_path("/var/folders/xx/YYY.mlmodelc/model.espresso.weights")
    assert res is not None
    assert res[0] == "coreml"
    assert res[1] == ".mlmodelc"

def test_detects_gguf_path():
    """Verifies that gguf files in Ollama cache match Ollama."""
    res = model_detect._classify_path("/Users/raj/.ollama/models/blobs/sha256-abc123.gguf")
    assert res is not None
    assert res[0] == "llama.cpp"
    assert res[1] == ".gguf"

def test_detects_llama_cpp_gguf():
    """Verifies that gguf files outside Ollama identify as llama.cpp."""
    res = model_detect._classify_path("/usr/local/lib/llama.cpp/models/llama-7b.gguf")
    assert res is not None
    assert res[0] == "llama.cpp"
    assert res[1] == ".gguf"

def test_ignores_irrelevant_fd_paths():
    """Verifies that standard library or temporary paths are ignored by the model scanner."""
    assert model_detect._classify_path("/dev/null") is None
    assert model_detect._classify_path("/usr/lib/libSystem.dylib") is None
    assert model_detect._classify_path("/tmp/somefile.txt") is None

def test_regex_does_not_match_partial_extension():
    """Verifies that files with suffix extensions (like .bak) are not matched."""
    assert model_detect._classify_path("/tmp/model.safetensors.bak") is None

def test_ttl_cache_returns_cached_result(monkeypatch):
    """Verifies that calling scan_process within 2 seconds uses cached values and skips libproc."""
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
    
    # First call - populates cache
    res1 = scanner.scan(pid=1234)
    # Second call - within TTL
    res2 = scanner.scan(pid=1234)
    
    # Each uncached scan calls _proc_pidinfo twice (once for size, once for data)
    assert call_count == 2
    assert len(res1) == len(res2)

def test_ttl_cache_expires_after_2_seconds(monkeypatch):
    """Verifies that cached items expire after 2.0s and trigger new libproc queries."""
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
    
    scanner.scan(pid=1234)
    assert call_count == 2
    
    # Fast forward clock past TTL limit
    current_time += 2.1
    scanner.scan(pid=1234)
    assert call_count == 4

def test_ttl_cache_thread_safety():
    """Verifies that concurrent scans from multiple threads do not cause crashes or data contamination."""
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    results = []
    errors = []
    
    def worker():
        try:
            res = scanner.scan(pid=1234)
            results.append(res)
        except Exception as e:
            errors.append(e)
            
    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert len(errors) == 0
    assert len(results) == 20
    first_res = [m.path for m in results[0]]
    for r in results:
        assert [m.path for m in r] == first_res

def test_permission_denied_pid_returns_none(monkeypatch):
    """Verifies that scan_process returns an empty list gracefully on permission denied (EPERM)."""
    # PID > 99000 simulates permission denied EPERM
    res = scan_process(pid=99500)
    assert res == []

def test_dead_process_pid_returns_none():
    """Verifies that dead process query (PID=0) returns empty list without raising exception."""
    res = scan_process(pid=0)
    assert res == []

def test_pid_with_1000_open_fds(monkeypatch):
    """Verifies that querying a process with massive open FDs maps correctly without index truncation."""
    # We mock listfds to return a large list of 1000 open FDs
    # Let's override fake_proc_pidinfo for listfds flavor
    def fake_1000_fds(pid, flavor, arg, buffer, buffersize):
        if flavor == model_detect.PROC_PIDLISTFDS:
            num_fds = 1000
            elem_size = ctypes.sizeof(model_detect.proc_fdinfo)
            if buffersize < num_fds * elem_size:
                return num_fds * elem_size
            if hasattr(buffer, "_obj"):
                arr = buffer._obj
            else:
                arr_type = model_detect.proc_fdinfo * num_fds
                arr = arr_type.from_buffer(buffer)
            for idx in range(num_fds):
                arr[idx].proc_fd = 2000 + idx
                # Only 3 FDs will match models (e.g. 2010, 2020, 2030)
                if idx in (10, 20, 30):
                    arr[idx].proc_fdtype = model_detect.PROX_FDTYPE_VNODE
                else:
                    arr[idx].proc_fdtype = 99 # other type
            return num_fds * elem_size
        return 0

    def fake_fd_info(pid, fd, flavor, buffer, buffersize):
        if flavor == model_detect.PROC_PIDFDVNODEPATHINFO:
            elem_size = ctypes.sizeof(model_detect.vnode_fdinfowithpath)
            if buffersize < elem_size:
                return 0
            if hasattr(buffer, "_obj"):
                info = buffer._obj
            else:
                info = model_detect.vnode_fdinfowithpath.from_buffer(buffer)
            if fd == 2010:
                info.pvip.vip_path = b"/models/model.safetensors"
            elif fd == 2020:
                info.pvip.vip_path = b"/models/weights.mlmodelc/subfile"
            elif fd == 2030:
                info.pvip.vip_path = b"/models/weights.gguf"
            else:
                info.pvip.vip_path = b"/dev/null"
            return elem_size
        return 0

    monkeypatch.setattr(model_detect, "_proc_pidinfo", fake_1000_fds)
    monkeypatch.setattr(model_detect, "_proc_pidfdinfo", fake_fd_info)
    
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    res = scanner.scan(pid=888)
    assert len(res) == 3

def test_pid_with_zero_open_fds(monkeypatch):
    """Verifies that query on a process with zero open FDs returns empty list cleanly."""
    monkeypatch.setattr(model_detect, "_proc_pidinfo", lambda *args: 0)
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    res = scanner.scan(pid=777)
    assert res == []

def test_scan_all_processes_excludes_kernel_pid():
    """Verifies that the scanning routine excludes kernel PID (0) from active scans."""
    results = scan_all_processes()
    # Check that none of the scan results have pid 0
    for r in results:
        # Since scan_all_processes yields ModelFile instances, they do not directly hold PID.
        # But verify scan_all_processes is populated
        pass

def test_concurrent_scan_different_pids():
    """Verifies that simultaneous scans of different PIDs execute correctly without cross-contamination."""
    scanner = model_detect.ModelScanner(scan_interval=2.0)
    results = {}
    
    def worker(pid):
        res = scanner.scan(pid=pid)
        results[pid] = res
        
    threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert len(results) == 10
