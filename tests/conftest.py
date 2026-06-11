import os
import sys
import time
import struct
import ctypes
import sqlite3
import random
import pytest
from unittest.mock import MagicMock

# Make sure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import model_detect

class FakeLibproc:
    def __getattr__(self, name):
        if name == "proc_pidinfo":
            return self.fake_proc_pidinfo
        if name == "proc_pidfdinfo":
            return self.fake_proc_pidfdinfo
        raise AttributeError(name)

    def fake_proc_pidinfo(self, pid, flavor, arg, buffer, buffersize):
        import errno
        if pid > 99000:
            ctypes.set_errno(errno.EPERM)
            return -1
        if pid == 0:
            ctypes.set_errno(errno.ESRCH)
            return -1

        if flavor == model_detect.PROC_PIDLISTFDS:
            num_fds = 3
            elem_size = ctypes.sizeof(model_detect.proc_fdinfo)
            if buffersize < num_fds * elem_size:
                return num_fds * elem_size

            if hasattr(buffer, "_obj"):
                arr = buffer._obj
            else:
                arr_type = model_detect.proc_fdinfo * num_fds
                arr = arr_type.from_buffer(buffer)
            arr[0].proc_fd = 10
            arr[0].proc_fdtype = model_detect.PROX_FDTYPE_VNODE
            arr[1].proc_fd = 11
            arr[1].proc_fdtype = model_detect.PROX_FDTYPE_VNODE
            arr[2].proc_fd = 12
            arr[2].proc_fdtype = model_detect.PROX_FDTYPE_VNODE
            return num_fds * elem_size
        return 0

    def fake_proc_pidfdinfo(self, pid, fd, flavor, buffer, buffersize):
        import errno
        if pid > 99000:
            ctypes.set_errno(errno.EPERM)
            return -1
        if pid == 0:
            ctypes.set_errno(errno.ESRCH)
            return -1

        if flavor == model_detect.PROC_PIDFDVNODEPATHINFO:
            elem_size = ctypes.sizeof(model_detect.vnode_fdinfowithpath)
            if buffersize < elem_size:
                return 0

            if hasattr(buffer, "_obj"):
                info = buffer._obj
            else:
                info = model_detect.vnode_fdinfowithpath.from_buffer(buffer)
            if fd == 10:
                info.pvip.vip_path = b"/Users/raj/.cache/huggingface/model.safetensors"
            elif fd == 11:
                info.pvip.vip_path = b"/var/folders/xx/YYY.mlmodelc/model.espresso.weights"
            elif fd == 12:
                info.pvip.vip_path = b"/Users/raj/.ollama/models/blobs/sha256:abc123.gguf"
            else:
                info.pvip.vip_path = b"/tmp/somefile.txt"
            return elem_size
        return 0

@pytest.fixture(autouse=True)
def mock_libproc(monkeypatch):
    """
    Patches ctypes.CDLL to return a fake libproc.dylib and binds proc_pidinfo/proc_pidfdinfo.
    """
    fake_lib = FakeLibproc()
    monkeypatch.setattr(model_detect, "_libproc", fake_lib)
    monkeypatch.setattr(model_detect, "_proc_pidinfo", fake_lib.fake_proc_pidinfo)
    monkeypatch.setattr(model_detect, "_proc_pidfdinfo", fake_lib.fake_proc_pidfdinfo)
    return fake_lib

@pytest.fixture
def mock_rusage_factory():
    """
    Returns a callable: make_rusage(pid, neural_footprint_mb, energy_nj, cpu_time_ns)
    Produces a populated rusage_info_v6 ctypes Structure.
    """
    from drift import rusage_info_v6
    
    def make_rusage(pid=100, neural_footprint_mb=128.0, energy_nj=500_000_000, cpu_time_ns=1_000_000_000):
        info = rusage_info_v6()
        info.ri_neural_footprint = int(neural_footprint_mb * 1024 * 1024)
        info.ri_energy_nj = int(energy_nj)
        info.ri_proc_start_abstime = 1000
        info.ri_user_time = int(cpu_time_ns // 2)
        info.ri_system_time = int(cpu_time_ns // 2)
        info.ri_instructions = 1_000_000
        info.ri_cycles = 2_000_000
        info.ri_lifetime_max_neural_footprint = info.ri_neural_footprint
        info.ri_interval_max_neural_footprint = info.ri_neural_footprint
        return info
        
    return make_rusage

@pytest.fixture
def mock_smc(monkeypatch):
    """
    Patches IOKit IOConnectCallStructMethod.
    Provides a configure(sensor_key, value_celsius, val_type="flt ") method.
    """
    from drift import SMCTempReader
    
    config = {"TCMb": 45.0}
    types = {"TCMb": "flt "}
    
    class SMCConfigurator:
        def configure(self, key: str, val: float, val_type: str = "flt "):
            config[key] = val
            types[key] = val_type
            
    def fake_struct_method(conn, selector, input_ptr, input_size, output_ptr, output_size_ptr):
        from drift import SMCParamStruct
        if hasattr(input_ptr, "_obj"):
            input_struct = input_ptr._obj
        else:
            input_struct = SMCParamStruct.from_address(input_ptr)
            
        if hasattr(output_ptr, "_obj"):
            output_struct = output_ptr._obj
        else:
            output_struct = SMCParamStruct.from_address(output_ptr)
        
        req_key_val = input_struct.key
        req_key_str = struct.pack(">I", req_key_val).decode("ascii", errors="ignore")
        
        if input_struct.data8 == 9:  # SMC_CMD_READ_KEYINFO
            if req_key_str in config and config[req_key_str] >= 0.0:
                output_struct.result = 0
                val_type = types.get(req_key_str, "flt ")
                output_struct.keyInfo.dataSize = 4 if val_type == "flt " else 2
                output_struct.keyInfo.dataType = struct.unpack(">I", val_type.encode("ascii"))[0]
            else:
                output_struct.result = 0x84  # kIOReturnNotFound
                output_struct.keyInfo.dataSize = 0
            return 0
            
        elif input_struct.data8 == 5:  # SMC_CMD_READ_BYTES
            if req_key_str in config and config[req_key_str] >= 0.0:
                val = config[req_key_str]
                val_type = types.get(req_key_str, "flt ")
                output_struct.result = 0
                if val_type == "flt ":
                    raw = struct.pack("f", val)
                else:  # sp78
                    raw = struct.pack(">h", int(val * 256.0))
                for idx, b in enumerate(raw):
                    output_struct.bytes[idx] = b
            else:
                output_struct.result = 0x84
            return 0
        return 0

    class FakeIOKit:
        def __getattr__(self, name):
            if name == "IOServiceMatching":
                return lambda service: 1234
            if name == "IOServiceGetMatchingService":
                return lambda port, matching: 5678
            if name == "IOServiceOpen":
                def fake_open(service, task, type_val, connect_ref):
                    connect_ref.contents.value = 9999
                    return 0
                return fake_open
            if name == "IOServiceClose":
                return lambda conn: 0
            if name == "IOConnectCallStructMethod":
                return fake_struct_method
            raise AttributeError(name)

    def fake_init_connection(self):
        self.iokit = FakeIOKit()
        self.conn = 9999

    monkeypatch.setattr(SMCTempReader, "_init_connection", fake_init_connection)
    monkeypatch.setattr(SMCTempReader, "close", lambda self: None)
    
    return SMCConfigurator()

@pytest.fixture
def tmp_drift_db(tmp_path, monkeypatch):
    """
    Creates a fresh ~/.drift/history.db at tmp_path/history.db.
    """
    db_dir = tmp_path / ".drift"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "history.db"
    
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL,
        ended_at REAL,
        host TEXT,
        chip TEXT
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        ts REAL,
        pid INTEGER,
        process_name TEXT,
        model_name TEXT,
        framework TEXT,
        peak_power_mw REAL,
        total_energy_mj REAL,
        event_type TEXT
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        ts REAL,
        ane_util_pct REAL,
        die_temp_c REAL
    );
    """)
    conn.commit()
    conn.close()
    
    orig_expand = os.path.expanduser
    def fake_expand(path):
        if path.startswith("~/.drift"):
            suffix = path[len("~/.drift"):].lstrip("/")
            return str(db_dir / suffix)
        if path == "~":
            return str(tmp_path)
        return orig_expand(path)
        
    monkeypatch.setattr(os.path, "expanduser", fake_expand)
    return str(db_path)

@pytest.fixture
def sample_process_list():
    """
    Returns a list of 50 fake ProcessInfo dicts with randomized properties.
    """
    processes = []
    
    model_types = [
        ("/Users/raj/models/resnet.mlmodelc", "CoreML"),
        ("/Users/raj/.cache/huggingface/model.safetensors", "MLX"),
        ("/Users/raj/.ollama/models/blobs/sha256-123.gguf", "Ollama"),
        ("/usr/local/lib/llama.cpp/models/llama-7b.gguf", "llama.cpp"),
        (None, None)
    ]
    
    for i in range(50):
        pid = 100 + i
        pname = f"process_{pid}"
        model_path, framework = model_types[i % len(model_types)]
        model_name = os.path.basename(model_path) if model_path else None
        
        footprint = (i * 40.0) % 2048
        power = (i * 600.0) % 30000
        energy = (i * 10000.0) % 500000
        
        processes.append({
            "pid": pid,
            "name": pname,
            "ane_pct": (power / 28000.0) * 100.0,
            "ane_mw": power,
            "cumulative_mj": energy,
            "delta_energy_mj": 10.0,
            "neural_footprint_mb": footprint,
            "peak_neural_footprint_mb": footprint * 1.2,
            "interval_peak_mb": footprint,
            "ipc": 1.5,
            "valid_ipc": True,
            "model_name": model_name,
            "model_framework": framework,
            "model_size_gb": footprint / 1024.0,
            "history": [power] * 10,
            "footprint_history": [footprint] * 10,
            "power_history": [power] * 10
        })
    return processes

@pytest.fixture
def live_session_id(tmp_drift_db):
    """
    Inserts a live session row into tmp_drift_db and returns its id.
    """
    conn = sqlite3.connect(tmp_drift_db)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sessions (started_at, ended_at, host, chip) VALUES (?, NULL, 'Rajs-MacBook', 'Apple M5');",
        (time.time(),)
    )
    s_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return s_id
