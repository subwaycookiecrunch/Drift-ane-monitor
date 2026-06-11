import argparse
import datetime
import shutil
import ctypes
import ctypes.util
import json
import logging
import os
import platform
import queue
import re
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple

from model_detect import ModelScanner

try:
    import psutil
    from textual.app import App, ComposeResult
    from textual.containers import Container
    from textual.reactive import reactive
    from textual.widgets import Header as THeader, Footer, DataTable, Static, Label, OptionList, RichLog
    from textual.screen import Screen
    from textual.widgets.option_list import Option
    from textual.css.query import NoMatches
except ImportError:
    print("Error: Missing required packages. Run `pip install textual psutil`")
    sys.exit(1)

# ==============================================================================
# SMC/IOKit ctypes Definitions (Phase 3)
# ==============================================================================

class SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_ubyte),
        ("minor", ctypes.c_ubyte),
        ("build", ctypes.c_ubyte),
        ("reserved", ctypes.c_ubyte),
        ("release", ctypes.c_ushort),
    ]

class SMCPLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("cpuPLimit", ctypes.c_uint32),
        ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]

class SMCKeyInfoData(ctypes.Structure):
    _fields_ = [
        ("dataSize", ctypes.c_uint32),
        ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_ubyte),
    ]

class SMCParamStruct(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", SMCVersion),
        ("pLimitData", SMCPLimitData),
        ("keyInfo", SMCKeyInfoData),
        ("result", ctypes.c_ubyte),
        ("status", ctypes.c_ubyte),
        ("data8", ctypes.c_ubyte),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * 32),
    ]

class SMCTempReader:
    def __init__(self):
        self.conn = None
        self.iokit = None
        self.active_key = None
        self.active_key_val = None
        self.val_size = 0
        self.val_type_str = ""
        self._init_connection()
        self._find_active_key()
        
    def _init_connection(self) -> None:
        try:
            self.iokit = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
            libc = ctypes.CDLL("libc.dylib")
            mach_task_self = libc.mach_task_self
            mach_task_self.restype = ctypes.c_uint32
            
            self.iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
            self.iokit.IOServiceMatching.restype = ctypes.c_void_p
            self.iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            self.iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
            self.iokit.IOServiceOpen.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            self.iokit.IOServiceOpen.restype = ctypes.c_int
            self.iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
            self.iokit.IOServiceClose.restype = ctypes.c_int
            self.iokit.IOConnectCallStructMethod.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t)
            ]
            self.iokit.IOConnectCallStructMethod.restype = ctypes.c_int
            
            matching = self.iokit.IOServiceMatching(b"AppleSMC")
            if not matching:
                return
            service = self.iokit.IOServiceGetMatchingService(0, matching)
            if not service:
                return
                
            connect = ctypes.c_uint32()
            ret = self.iokit.IOServiceOpen(service, mach_task_self(), 0, ctypes.byref(connect))
            if ret == 0:
                self.conn = connect.value
        except Exception as e:
            logging.debug(f"Failed to initialize SMC connection: {e}")
            
    def _find_active_key(self) -> None:
        if not self.conn:
            return
            
        candidates = ["TC0P", "TC0D", "TCMb", "TCHP", "TH0T", "Tp00", "Tp01", "Ts0P"]
        for key_str in candidates:
            try:
                key_bytes = key_str.encode("ascii")
                key_val = struct.unpack(">I", key_bytes)[0]
                
                input_struct = SMCParamStruct()
                input_struct.key = key_val
                input_struct.data8 = 9  # SMC_CMD_READ_KEYINFO = 9
                
                output_struct = SMCParamStruct()
                output_size = ctypes.c_size_t(ctypes.sizeof(SMCParamStruct))
                
                ret = self.iokit.IOConnectCallStructMethod(
                    self.conn,
                    2,  # Selector 2
                    ctypes.byref(input_struct),
                    ctypes.sizeof(input_struct),
                    ctypes.byref(output_struct),
                    ctypes.byref(output_size)
                )
                if ret == 0 and output_struct.result == 0 and output_struct.keyInfo.dataSize > 0:
                    self.active_key = key_str
                    self.active_key_val = key_val
                    self.val_size = output_struct.keyInfo.dataSize
                    val_type = output_struct.keyInfo.dataType
                    self.val_type_str = struct.pack(">I", val_type).decode("ascii", errors="ignore")
                    logging.info(f"SMC selected sensor key '{key_str}' (type={self.val_type_str}, size={self.val_size})")
                    break
            except Exception:
                continue
                
    def read_temp(self) -> Optional[float]:
        if not self.conn or not self.active_key_val:
            return None
            
        try:
            input_struct = SMCParamStruct()
            input_struct.key = self.active_key_val
            input_struct.keyInfo.dataSize = self.val_size
            input_struct.data8 = 5  # SMC_CMD_READ_BYTES = 5
            
            output_struct = SMCParamStruct()
            output_size = ctypes.c_size_t(ctypes.sizeof(SMCParamStruct))
            
            ret = self.iokit.IOConnectCallStructMethod(
                self.conn,
                2,  # Selector 2
                ctypes.byref(input_struct),
                ctypes.sizeof(input_struct),
                ctypes.byref(output_struct),
                ctypes.byref(output_size)
            )
            if ret == 0 and output_struct.result == 0:
                raw_bytes = bytes(output_struct.bytes[:self.val_size])
                if self.val_type_str == "flt " and len(raw_bytes) == 4:
                    return struct.unpack("f", raw_bytes)[0]
                elif self.val_type_str == "sp78" and len(raw_bytes) == 2:
                    return struct.unpack(">h", raw_bytes)[0] / 256.0
        except Exception as e:
            logging.debug(f"Failed to read SMC temperature: {e}")
        return None
        
    def close(self) -> None:
        if self.conn and self.iokit:
            try:
                self.iokit.IOServiceClose(self.conn)
            except Exception:
                pass
            self.conn = None


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(filename="drift.log", level=level, format=LOG_FORMAT)

def check_compatibility() -> None:
    mac_ver_str = platform.mac_ver()[0]
    if not mac_ver_str:
        print("Error: Could not determine macOS version.")
        sys.exit(1)
    
    major_version = int(mac_ver_str.split('.')[0])
    if major_version < 13:
        print(f"Error: drift requires macOS 13.0 or later (Ventura+). Your version: {mac_ver_str}")
        sys.exit(1)
        
    try:
        arch = subprocess.check_output(["sysctl", "-n", "hw.optional.arm64"], text=True).strip()
        if arch != "1":
            raise ValueError("Not Apple Silicon")
    except Exception:
        print("Error: drift requires Apple Silicon (M1 or later). Intel Macs are not supported.")
        sys.exit(1)

@dataclass
class ChipInfo:
    family: str
    variant: str
    desc: str
    cores: str
    is_ultra: bool

def get_chip_info() -> ChipInfo:
    try:
        desc = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    except Exception:
        desc = "Apple Silicon"
        
    try:
        cores = subprocess.check_output(["sysctl", "-n", "hw.perflevel0.logicalcpu"], text=True).strip()
    except Exception:
        cores = "?"
        
    family = "Unknown"
    variant = "Base"
    is_ultra = False
    
    match = re.search(r"Apple (M\d+)(?:\s+(Pro|Max|Ultra))?", desc)
    if match:
        family = match.group(1)
        if match.group(2):
            variant = match.group(2)
            if variant == "Ultra":
                is_ultra = True
                
    if is_ultra:
        try:
            c = int(cores)
            cores = str(c * 2)
            desc = desc.replace("Ultra", "Ultra (dual die)")
        except ValueError:
            pass

    return ChipInfo(family=family, variant=variant, desc=desc, cores=cores, is_ultra=is_ultra)

class rusage_info_v6(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
        ("ri_flags", ctypes.c_uint64),
        ("ri_user_ptime", ctypes.c_uint64),
        ("ri_system_ptime", ctypes.c_uint64),
        ("ri_pinstructions", ctypes.c_uint64),
        ("ri_pcycles", ctypes.c_uint64),
        ("ri_energy_nj", ctypes.c_uint64),
        ("ri_penergy_nj", ctypes.c_uint64),
        ("ri_secure_time_in_system", ctypes.c_uint64),
        ("ri_secure_ptime_in_system", ctypes.c_uint64),
        ("ri_neural_footprint", ctypes.c_uint64),
        ("ri_lifetime_max_neural_footprint", ctypes.c_uint64),
        ("ri_interval_max_neural_footprint", ctypes.c_uint64),
        ("ri_reserved", ctypes.c_uint64 * 9),
    ]

try:
    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    proc_pid_rusage = libc.proc_pid_rusage
    proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    proc_pid_rusage.restype = ctypes.c_int
except Exception as e:
    logging.warning(f"Failed to load proc_pid_rusage: {e}")

RUSAGE_INFO_V6 = 6

def safe_call(func: Any, *args: Any, fallback: Any = None) -> Any:
    try:
        return func(*args)
    except Exception as e:
        logging.debug(f"safe_call failed for {func.__name__}: {e}")
        return fallback

@dataclass
class ProcessInfo:
    pid: int
    name: str
    start_abstime: int
    ane_pct: float = 0.0
    ane_mw: float = 0.0
    cumulative_mj: float = 0.0
    neural_footprint: float = 0.0
    peak_neural_footprint: float = 0.0
    interval_peak: float = 0.0
    ipc: float = 0.0
    valid_ipc: bool = False
    model_name: Optional[str] = None
    model_framework: Optional[str] = None
    model_size_gb: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=60))
    footprint_history: deque = field(default_factory=lambda: deque(maxlen=240))
    power_history: deque = field(default_factory=lambda: deque(maxlen=240))
    peak_mw: float = 0.0
    last_seen_time: float = 0.0
    delta_energy_mj: float = 0.0

class SortMode(Enum):
    ANE_PCT = 1
    CUMULATIVE_MJ = 2
    FOOTPRINT = 3
    NAME = 4
    IPC = 5

class DataCollector:
    def __init__(self, interval_ms: int):
        self.interval_ms = interval_ms
        self.processes: Dict[Tuple[int, int], ProcessInfo] = {}
        self.session_processes: Dict[Tuple[int, int], ProcessInfo] = {}
        self.last_rusage: Dict[Tuple[int, int], Any] = {}
        self.lock = threading.Lock()
        self.model_scanner = ModelScanner(scan_interval=2.0)
        
        self.total_ane_pct = 0.0
        self.total_ane_mw = 0.0
        self.peak_mw = 0.0
        self.total_history = deque(maxlen=60)
        self.peak_history = deque(maxlen=120)
        self.session_cumulative_mj = 0.0
        self.last_collect_ts = 0.0
        
        self.temp_reader = SMCTempReader()
        self.current_temp = None
        
        chip = get_chip_info()
        self.budget_mw = 28000.0
        if chip.is_ultra: 
            self.budget_mw = 56000.0
        elif chip.variant == "Base": 
            self.budget_mw = 14000.0

    def collect(self) -> None:
        now = time.time()
        self.last_collect_ts = now
        current_pids = set(psutil.pids())
        new_rusage = {}
        new_processes = {}
        
        for pid in current_pids:
            info = rusage_info_v6()
            try:
                ret = proc_pid_rusage(pid, RUSAGE_INFO_V6, ctypes.byref(info))
                if ret != 0:
                    err = ctypes.get_errno()
                    if err not in (1, 3):
                        logging.debug(f"proc_pid_rusage error {err} for pid {pid}")
                    continue
                
                key = (pid, info.ri_proc_start_abstime)
                new_rusage[key] = info
            except ctypes.ArgumentError as e:
                logging.debug(f"ArgumentError for pid {pid}: {e}")
            except OSError as e:
                logging.debug(f"OSError for pid {pid}: {e}")
                
        delta_system_nj = 0
        with self.lock:
            for key, info in new_rusage.items():
                pid, start_abstime = key
                if key not in self.session_processes:
                    try:
                        p = psutil.Process(pid)
                        name = p.name()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        name = f"unknown_{pid}"
                    proc = ProcessInfo(pid=pid, name=name, start_abstime=start_abstime)
                    proc.last_seen_time = now
                    self.session_processes[key] = proc
                else:
                    proc = self.session_processes[key]
                
                prev = self.last_rusage.get(key)
                
                ane_pct = 0.0
                ane_mw = 0.0
                ipc = 0.0
                valid_ipc = False
                
                neural_footprint = getattr(info, "ri_neural_footprint", 0)
                cumulative_energy_nj = getattr(info, "ri_energy_nj", 0)
                proc.cumulative_mj = cumulative_energy_nj / 1e6
                proc.neural_footprint = neural_footprint / (1024.0 * 1024.0)
                proc.peak_neural_footprint = getattr(info, "ri_lifetime_max_neural_footprint", 0) / (1024.0 * 1024.0)
                proc.interval_peak = getattr(info, "ri_interval_max_neural_footprint", 0) / (1024.0 * 1024.0)
                
                if prev:
                    energy_now = getattr(info, "ri_energy_nj", 0)
                    energy_prev = getattr(prev, "ri_energy_nj", 0)
                    delta_energy = energy_now - energy_prev
                    
                    if delta_energy > 0:
                        proc.delta_energy_mj = delta_energy / 1e6
                        delta_system_nj += delta_energy
                        if neural_footprint > 0:
                            delta_t = now - proc.last_seen_time
                            if delta_t > 0.01:
                                power_mw = proc.delta_energy_mj / delta_t
                            else:
                                power_mw = proc.delta_energy_mj / (self.interval_ms / 1000.0)
                            ane_mw = power_mw
                            ane_pct = min(100.0, (ane_mw / self.budget_mw) * 100.0)
                    else:
                        proc.delta_energy_mj = 0.0
                        
                    ins_now = getattr(info, "ri_instructions", 0)
                    ins_prev = getattr(prev, "ri_instructions", 0)
                    cyc_now = getattr(info, "ri_cycles", 0)
                    cyc_prev = getattr(prev, "ri_cycles", 0)
                    
                    d_ins = ins_now - ins_prev
                    d_cyc = cyc_now - cyc_prev
                    if d_cyc > 0 and neural_footprint > 0:
                        ipc = d_ins / d_cyc
                        valid_ipc = True
                else:
                    proc.delta_energy_mj = 0.0
                        
                proc.ane_pct = ane_pct
                proc.ane_mw = ane_mw
                proc.ipc = ipc
                proc.valid_ipc = valid_ipc
                proc.peak_mw = max(proc.peak_mw, ane_mw)
                proc.last_seen_time = now
                
                # Model detection: scan FDs for ANE-active processes
                if neural_footprint > 0:
                    try:
                        model = self.model_scanner.get_primary_model(pid)
                        if model:
                            proc.model_name = model.display
                            proc.model_framework = model.framework
                            proc.model_size_gb = model.size_gb
                        else:
                            proc.model_name = None
                            proc.model_framework = None
                            proc.model_size_gb = 0.0
                    except OSError:
                        pass
                
                norm_val = 0.0
                if proc.interval_peak > 0:
                    norm_val = (proc.neural_footprint / proc.interval_peak) * 100.0
                elif neural_footprint > 0:
                    norm_val = 100.0
                    
                proc.history.append(norm_val if norm_val > 0 else ane_pct)
                proc.footprint_history.append(proc.neural_footprint)
                proc.power_history.append(ane_mw)
                
                new_processes[key] = proc
                
            self.processes = new_processes
            self.last_rusage = new_rusage
            
            # Prune dead PIDs from model scanner cache
            alive_pids = {pid for pid, _ in new_processes.keys()}
            self.model_scanner.prune_dead(alive_pids)
            
            self.total_ane_mw = sum(p.ane_mw for p in self.processes.values() if p.neural_footprint > 0)
            self.total_ane_pct = min(100.0, (self.total_ane_mw / self.budget_mw) * 100.0)
            self.total_history.append(self.total_ane_pct)
            
            self.peak_history.append(self.total_ane_mw)
            self.peak_mw = max(self.peak_history) if self.peak_history else 0.0
            self.session_cumulative_mj += delta_system_nj / 1e6
            self.current_temp = self.temp_reader.read_temp()
            
    def get_session_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            now = time.time()
            res = []
            for key, p in self.session_processes.items():
                is_active = key in self.processes
                res.append({
                    "pid": p.pid,
                    "name": p.name,
                    "peak_mw": p.peak_mw,
                    "cumulative_mj": p.cumulative_mj,
                    "model_name": p.model_name,
                    "last_seen_time": p.last_seen_time,
                    "is_active": is_active,
                })
            return res

    def get_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "ts": self.last_collect_ts or time.time(),
                "total_ane_pct": self.total_ane_pct,
                "total_ane_mw": self.total_ane_mw,
                "peak_mw": self.peak_mw,
                "total_history": list(self.total_history),
                "budget_mw": self.budget_mw,
                "session_cumulative_mj": self.session_cumulative_mj,
                "temp": self.current_temp,
                "sensor_key": self.temp_reader.active_key,
                "processes": [
                    {
                        "pid": p.pid,
                        "name": p.name,
                        "ane_pct": p.ane_pct,
                        "ane_mw": p.ane_mw,
                        "cumulative_mj": p.cumulative_mj,
                        "delta_energy_mj": p.delta_energy_mj,
                        "neural_footprint_mb": p.neural_footprint,
                        "peak_neural_footprint_mb": p.peak_neural_footprint,
                        "interval_peak_mb": p.interval_peak,
                        "ipc": p.ipc,
                        "valid_ipc": p.valid_ipc,
                        "model_name": p.model_name,
                        "model_framework": p.model_framework,
                        "model_size_gb": p.model_size_gb,
                        "history": list(p.history),
                        "footprint_history": list(p.footprint_history),
                        "power_history": list(p.power_history)
                    }
                    for p in self.processes.values()
                ]
            }

def render_sparkline(values: List[float], width: int = 12) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return "▁" * width
    m = max(values)
    if m == 0:
        return "▁" * width
    
    scaled = []
    if len(values) < width:
        scaled = [0.0] * (width - len(values)) + values
    elif len(values) > width:
        step = len(values) / width
        for i in range(width):
            idx = int(i * step)
            scaled.append(values[idx])
    else:
        scaled = values
        
    return "".join(blocks[min(7, int((v/m)*7))] for v in scaled)

def colorize_pct(pct: float) -> str:
    if pct < 20.0:
        return f"[#5DC9A5]{pct:5.1f}%[/]"
    elif pct < 60.0:
        return f"[#E8A045]{pct:5.1f}%[/]"
    else:
        return f"[#E05C5C]{pct:5.1f}%[/]"

def colorize_ipc(ipc: float, valid: bool) -> str:
    if not valid:
        return "  —  "
    if ipc > 3.0:
        return f"[#5DC9A5]{ipc:5.2f}[/]"
    elif ipc >= 1.5:
        return f"[#E8A045]{ipc:5.2f}[/]"
    else:
        return f"[#E05C5C]{ipc:5.2f}[/]"
        
def format_peak(current: float, peak: float) -> str:
    if peak == 0:
        return f"{peak:6.1f}"
    if current >= peak * 0.99:
        return f"[#E8A045]{peak:6.1f}[/]"
    return f"[dim #888888]{peak:6.1f}[/]"

def classify_name(name: str) -> str:
    sys_daemons = [".apple.", "com.apple", "kernel_task", "launchd", "WindowServer", "coreaudiod"]
    for d in sys_daemons:
        if d in name:
            return f"[dim #888888]{name[:30]}[/]"
    return f"[bold #FFFFFF]{name[:30]}[/]"

class DriftHeader(Static):
    peak_mw = reactive(0.0)
    flash = reactive(False)
    budget = reactive(0.0)
    cumulative_mj = reactive(0.0)
    temp = reactive(None)
    sensor_key = reactive(None)
    
    def render(self) -> str:
        if hasattr(self.app, "mode") and self.app.mode == "replay":
            frames = self.app.replay_frames
            idx = min(self.app.replay_index, len(frames) - 1)
            t_start = frames[0]['timestamp_ns']
            t_end = frames[-1]['timestamp_ns']
            t_curr = frames[idx]['timestamp_ns']
            elapsed_s = (t_curr - t_start) / 1e9
            total_s = (t_end - t_start) / 1e9
            
            elapsed_str = f"{int(elapsed_s)//60:02d}:{int(elapsed_s)%60:02d}"
            total_str = f"{int(total_s)//60:02d}:{int(total_s)%60:02d}"
            
            pct = elapsed_s / total_s if total_s > 0 else 0.0
            width = 15
            slider_pos = int(pct * width)
            slider = "─" * slider_pos + "►" + "─" * (width - slider_pos - 1)
            
            return f"[bold #5DC9A5]REPLAY[/] {elapsed_str} / {total_str} {slider}"
            
        chip = get_chip_info()
        t_str = ""
        if self.temp is not None:
            key_name = self.sensor_key or "SMC"
            t_str = f"  ·  [bold #E8A045]{key_name}: {self.temp:.1f}°C[/]"
        title = f"drift  ·  {chip.desc}  ·  {chip.cores}-core ANE{t_str}  ·  v1.0.0"
        
        rec_str = ""
        if hasattr(self.app, "record_file") and self.app.record_file:
            rec_str = "  [bold #E05C5C]● REC[/]"
            
        color = "[#E8A045]" if self.flash else "[#5DC9A5]"
        peak_str = f"{color}▲ peak: {self.peak_mw/1000.0:.1f}W[/]"
        
        budget_str = ""
        if self.budget > 0:
            pct = min(100.0, (self.cumulative_mj / self.budget) * 100.0)
            filled = int((pct / 100.0) * 10)
            if pct < 80.0:
                bar_color = "[#5DC9A5]"
            elif pct < 100.0:
                bar_color = "[#E8A045]"
            else:
                bar_color = "[#E05C5C]"
            bar = bar_color + ("█" * filled) + "[/]" + "[#333333]" + ("░" * (10 - filled)) + "[/]"
            budget_str = f"   Budget: {bar} {self.cumulative_mj:,.0f} / {self.budget:.0f} mJ ({pct:.0f}%)"
            
        return f"{title}    {peak_str}{rec_str}{budget_str}"
        
class UtilBar(Static):
    pct = reactive(0.0)
    mw = reactive(0.0)
    budget = reactive(28000.0)

    def render(self) -> str:
        filled = int((self.pct / 100.0) * 40)
        bar_char = "█"
        if self.pct < 20.0: color = "[#5DC9A5]"
        elif self.pct < 60.0: color = "[#E8A045]"
        else: color = "[#E05C5C]"
        bar = color + (bar_char * filled) + "[/]" + "[#333333]" + ("░" * (40 - filled)) + "[/]"
        return f"{bar}  {colorize_pct(self.pct)}   {self.mw/1000.0:5.3f}W / {self.budget/1000.0:.0f}W budget"

class GlobalSparkline(Static):
    history = reactive(list)

    def render(self) -> str:
        spark = render_sparkline(self.history, 40)
        return f"[#5D8AA8]{spark}[/]"

class MainScreen(Screen):
    def __init__(self, app_ref: Any):
        super().__init__()
        self.app_ref = app_ref
        self.show_ipc = False
        
    def compose(self) -> ComposeResult:
        yield DriftHeader(id="main-header")
        with Container(id="util-container"):
            yield Label("ANE UTILIZATION", classes="section-title")
            yield UtilBar(id="util-bar")
            yield GlobalSparkline(id="global-sparkline")
            
        with Container(id="table-container"):
            yield DataTable(id="proc-table")
            
        yield Footer()

    def on_mount(self) -> None:
        self.update_table_columns()
        
    def update_table_columns(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        table.clear(columns=True)
        cols = ["PROCESS", "PID", "ANE%", "mW"]
        
        try:
            term_width = os.get_terminal_size().columns
        except OSError:
            term_width = 80
        show_model = term_width >= 120
        show_mj = term_width >= 100
        show_ipc = term_width >= 100 and self.show_ipc
        show_peak = term_width >= 80

        if show_model: cols.append("MODEL")
        if show_ipc: cols.append("IPC")
        if show_mj: cols.append("∑ mJ")
        if show_peak: cols.append("Peak MB")
        cols.append("TREND")
        
        table.add_columns(*cols)

    def action_toggle_ipc(self) -> None:
        self.show_ipc = not self.show_ipc
        self.update_table_columns()

class WatchScreen(Screen):
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("p", "toggle_pause", "Pause"),
        ("escape", "back", "Back"),
    ]

    def __init__(self, app_ref: Any, target_name: str, target_pid: Optional[int] = None):
        super().__init__()
        self.app_ref = app_ref
        self.target_name = target_name
        self.target_pid = target_pid
        self.events: List[Tuple[str, str]] = []
        self.session_peak = 0.0
        self.is_paused = False
        self.stable_logged = False
        self.loaded_logged = False
        self.last_active_time = time.time()
        self.has_fired_idle = False
        self.thermal_history = deque(maxlen=240)
        self.last_thermal_time = 0.0
        self.last_known_temp = None
        
    def compose(self) -> ComposeResult:
        chip = get_chip_info()
        header_text = f"drift watch · {self.target_name} (PID {self.target_pid or '?'}) · {chip.desc} · v1.0.0"
        yield Label(header_text, id="watch-header")
        
        with Container(id="watch-container"):
            yield Label("ANE FOOTPRINT", classes="watch-title")
            yield Label("Waiting...", id="watch-footprint-box", classes="watch-data")
            
            yield Label("POWER", classes="watch-title")
            yield Label("Waiting...", id="watch-power-box", classes="watch-data")
            
            yield Label("TIMELINE (last 60s)", classes="watch-title")
            if getattr(self.app_ref, "thermal", False):
                yield Label("ANE %", classes="sparkline-label")
                yield Label("▁" * 100, id="watch-timeline")
                yield Label("°C", classes="sparkline-label", id="thermal-label")
                yield Label("▁" * 100, id="thermal-timeline")
            else:
                yield Label("▁" * 100, id="watch-timeline")
            
            yield Label("EVENTS", classes="watch-title")
            with Container(id="events-container"):
                yield Label("", id="watch-events")
                
        yield Footer()

    def add_event(self, desc: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.events.append((ts, desc))
        if len(self.events) > 20:
            self.events.pop(0)
            
    def action_quit_app(self) -> None:
        self.app_ref.exit()
        
    def action_toggle_pause(self) -> None:
        self.is_paused = not self.is_paused
        
    def action_back(self) -> None:
        self.app_ref.pop_screen()

    def update_data(self, data: Dict[str, Any]) -> None:
        if self.is_paused:
            return
            
        procs = data["processes"]
        target = None
        
        if self.target_pid:
            target = next((p for p in procs if p["pid"] == self.target_pid), None)
        else:
            matches = [p for p in procs if self.target_name.lower() in p["name"].lower()]
            if len(matches) == 1:
                target = matches[0]
                self.target_pid = target["pid"]
                
        if not target:
            self.query_one("#watch-footprint-box", Label).update(f"Process {self.target_name} exited or not found — waiting...")
            return

        current_fp = target["neural_footprint_mb"]
        model_name = target.get("model_name")
        
        if self.app_ref.budget > 0:
            proc_cum = target["cumulative_mj"]
            pct = (proc_cum / self.app_ref.budget) * 100.0
            if pct >= 80.0 and not self.app_ref.has_flashed_80:
                self.app_ref.has_flashed_80 = True
                try:
                    header = self.query_one("#watch-header", Label)
                    orig_text = header.renderable
                    header.update(f"[bold #E8A045]{orig_text}[/]")
                    self.app_ref.set_timer(0.5, lambda: header.update(orig_text))
                except NoMatches:
                    pass
            if pct >= 100.0 and not self.app_ref.has_notified_100:
                self.app_ref.has_notified_100 = True
                self.app_ref.fire_notification(f"ANE budget of {self.app_ref.budget} mJ exceeded", subtitle=target["name"])

        if self.app_ref.notify_on_spike:
            last_energy_mj = target.get("delta_energy_mj", 0.0)
            if last_energy_mj >= self.app_ref.notify_on_spike:
                now = time.time()
                last_t = self.app_ref.last_notify_time.get("spike", 0.0)
                if now - last_t >= 10.0:
                    self.app_ref.last_notify_time["spike"] = now
                    self.app_ref.fire_notification(f"Energy spike of {last_energy_mj:.1f} mJ detected", subtitle=target["name"])
                    
        is_active = target["ane_mw"] > 10.0 or target.get("delta_energy_mj", 0.0) > 0.1
        now = time.time()
        if is_active:
            self.last_active_time = now
            self.has_fired_idle = False
        else:
            idle_dur = now - self.last_active_time
            if self.app_ref.notify_on_idle and idle_dur >= self.app_ref.notify_on_idle and not self.has_fired_idle:
                last_t = self.app_ref.last_notify_time.get("idle", 0.0)
                if now - last_t >= 10.0:
                    self.app_ref.last_notify_time["idle"] = now
                    self.has_fired_idle = True
                    self.app_ref.fire_notification(f"Process went idle for {int(idle_dur)} seconds", subtitle=target["name"])

        if current_fp > 10.0 and not self.loaded_logged:
            model_info = f" ({model_name})" if model_name else ""
            self.add_event(f"Model loaded{model_info} (footprint jumped to {current_fp:.1f} MB)")
            self.loaded_logged = True
            
        if current_fp == 0.0 and self.loaded_logged:
            self.add_event("Model unloaded (footprint dropped to 0)")
            self.loaded_logged = False
            self.stable_logged = False
            
        if current_fp > self.session_peak:
            if self.session_peak > 0:
                self.add_event(f"Peak ANE footprint reached: {current_fp:.1f} MB")
            self.session_peak = current_fp
            
        fp_hist = target["footprint_history"]
        if len(fp_hist) > 30 and current_fp > 10.0 and not self.stable_logged:
            recent = fp_hist[-30:]
            mean = sum(recent) / len(recent)
            variance = sum((x - mean) ** 2 for x in recent) / len(recent)
            std_dev = variance ** 0.5
            if mean > 0 and (std_dev / mean) < 0.02:
                self.add_event("Footprint stable (±2% for 30s)")
                self.stable_logged = True

        power_hist = target["power_history"]
        if len(power_hist) >= 2:
            if power_hist[-1] > power_hist[-2] * 1.5 and power_hist[-1] > 1000.0:
                self.add_event(f"Inference burst ({power_hist[-1]/1000.0:.1f}W)")
                
        rec_str = ""
        if hasattr(self.app_ref, "record_file") and self.app_ref.record_file:
            rec_str = "  [bold #E05C5C]● REC[/]"
            
        budget_str = ""
        if self.app_ref.budget > 0:
            proc_cum = target["cumulative_mj"]
            pct = min(100.0, (proc_cum / self.app_ref.budget) * 100.0)
            filled = int((pct / 100.0) * 10)
            if pct < 80.0:
                bar_color = "[#5DC9A5]"
            elif pct < 100.0:
                bar_color = "[#E8A045]"
            else:
                bar_color = "[#E05C5C]"
            bar = bar_color + ("█" * filled) + "[/]" + "[#333333]" + ("░" * (10 - filled)) + "[/]"
            budget_str = f"   Budget: {bar} {proc_cum:,.0f} / {self.app_ref.budget:.0f} mJ ({pct:.0f}%)"
            
        model_str = f" → {target['model_name']}" if target.get('model_name') else ""
        t_str = ""
        temp_val = data.get("temp")
        if temp_val is not None:
            key_name = data.get("sensor_key") or "SMC"
            t_str = f" · {key_name}: {temp_val:.1f}°C"
        header_text = f"drift watch · {target['name']}{model_str} (PID {self.target_pid}) · {get_chip_info().desc}{t_str} · v1.0.0{rec_str}{budget_str}"
        self.query_one("#watch-header", Label).update(header_text)
        
        ts = time.strftime("%H:%M:%S")
        model_line = ""
        if model_name:
            model_line = f"\n🧠 {model_name}"
        fp_str = f"Current: {current_fp:6.1f} MB    Peak: {self.session_peak:6.1f} MB    ▲ at {ts}"
        
        bar_width = 30
        filled = 0
        if self.session_peak > 0:
            filled = int((current_fp / self.session_peak) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        fp_display = f"{fp_str}\n{bar}  {target['ane_pct']:.1f}%{model_line}"
        self.query_one("#watch-footprint-box", Label).update(fp_display)
        
        ipc_str = "—"
        if target["valid_ipc"]:
            ipc_str = f"{target['ipc']:.2f}"
            if target["ipc"] > 3.0: ipc_str += " ✓"
            elif target["ipc"] < 1.5: ipc_str += " ⚠"
            
        pwr_str = f"Now: {target['ane_mw']/1000.0:.2f}W    Session ∑: {target['cumulative_mj']:,.0f} mJ    IPC: {ipc_str}"
        self.query_one("#watch-power-box", Label).update(pwr_str)
        
        try:
            term_width = os.get_terminal_size().columns
        except OSError:
            term_width = 80
        spark_width = max(20, term_width - 4)
        spark = render_sparkline(target["footprint_history"], spark_width)
        self.query_one("#watch-timeline", Label).update(spark)
        
        if self.app_ref.thermal:
            now_t = time.time()
            if now_t - self.last_thermal_time >= 2.0:
                self.last_thermal_time = now_t
                temp_val = data.get("temp")
                stale = False
                if temp_val is None:
                    if self.last_known_temp is not None:
                        temp_val = self.last_known_temp
                        stale = True
                else:
                    self.last_known_temp = temp_val
                    
                if temp_val is not None:
                    self.thermal_history.append(temp_val)
                    
                t_spark = render_sparkline_temp(list(self.thermal_history), spark_width)
                if temp_val is not None:
                    if temp_val < 60.0:
                        color_tag = ""
                        end_tag = ""
                    elif temp_val <= 80.0:
                        color_tag = "[#E8A045]"
                        end_tag = "[/]"
                    else:
                        color_tag = "[#E05C5C]"
                        end_tag = "[/]"
                else:
                    color_tag = ""
                    end_tag = ""
                self.query_one("#thermal-timeline", Label).update(f"{color_tag}{t_spark}{end_tag}")
                label_text = "°C ~" if stale else "°C"
                self.query_one("#thermal-label", Label).update(label_text)
                
        events_str = "\n".join(f"[dim]{t}[/dim]  {d}" for t, d in self.events)
        self.query_one("#watch-events", Label).update(events_str)

class PickerScreen(Screen):
    def __init__(self, app_ref: Any, matches: List[Dict[str, Any]]):
        super().__init__()
        self.app_ref = app_ref
        self.matches = matches
        
    def compose(self) -> ComposeResult:
        yield Label("Multiple processes found. Select one:", classes="watch-title")
        options = [Option(f"{p['name']} (PID {p['pid']})", id=str(p["pid"])) for p in self.matches]
        yield OptionList(*options, id="picker-list")
        
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        pid = int(event.option.id)
        name = next(p["name"] for p in self.matches if p["pid"] == pid)
        self.app_ref.pop_screen()
        self.app_ref.push_screen(WatchScreen(self.app_ref, name, pid))

class LeaderboardScreen(Screen):
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("r", "reset", "Reset"),
    ]

    def __init__(self, app_ref: Any):
        super().__init__()
        self.app_ref = app_ref

    def compose(self) -> ComposeResult:
        yield DriftHeader(id="main-header")
        with Container(id="util-container"):
            yield Label("SESSION LEADERBOARD", classes="section-title")
            yield Label("Tracks cumulative ANE energy (∑ mJ) since startup", id="leaderboard-desc")
        with Container(id="table-container"):
            yield DataTable(id="leaderboard-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#leaderboard-table", DataTable)
        table.add_columns("RANK", "PROCESS", "PID", "MODEL", "PEAK mW", "∑ mJ", "LAST SEEN")

    def update_leaderboard(self) -> None:
        try:
            table = self.query_one("#leaderboard-table", DataTable)
        except NoMatches:
            return

        session_data = self.app_ref.collector.get_session_snapshot()
        session_data = [p for p in session_data if p["cumulative_mj"] > 0.0 or p["peak_mw"] > 0.0]
        session_data.sort(key=lambda x: (x["cumulative_mj"], x["peak_mw"]), reverse=True)

        table.clear()
        
        now = time.time()
        for idx, p in enumerate(session_data):
            rank = idx + 1
            if rank == 1:
                rank_str = "[bold #E8A045]🥇 1[/]"
            elif rank == 2:
                rank_str = "🥈 2"
            elif rank == 3:
                rank_str = "🥉 3"
            else:
                rank_str = f"  {rank}"
                
            if p["is_active"]:
                last_seen_str = "[#5DC9A5]active now[/]"
            else:
                dt = now - p["last_seen_time"]
                if dt < 2.0:
                    last_seen_str = "just now"
                elif dt < 60.0:
                    last_seen_str = f"[#888888]{int(dt)}s ago[/]"
                elif dt < 3600.0:
                    last_seen_str = f"[#888888]{int(dt // 60)}m ago[/]"
                else:
                    last_seen_str = f"[#888888]{int(dt // 3600)}h ago[/]"

            model_str = p["model_name"] or "—"
            if len(model_str) > 24:
                model_str = model_str[:22] + "…"
            model_styled = f"[#4EC9B0]{model_str}[/]" if p["model_name"] else "—"

            if rank == 1:
                row = [
                    rank_str,
                    f"[bold #5DC9A5]{p['name']}[/]",
                    f"[bold #5DC9A5]{p['pid']}[/]",
                    f"[bold]{model_styled}[/]",
                    f"[bold #5DC9A5]{p['peak_mw']:.1f}[/]",
                    f"[bold #5DC9A5]{p['cumulative_mj']:,.1f}[/]",
                    f"[bold]{last_seen_str}[/]"
                ]
            else:
                row = [
                    rank_str,
                    p["name"],
                    str(p["pid"]),
                    model_styled,
                    f"{p['peak_mw']:.1f}",
                    f"{p['cumulative_mj']:,.1f}",
                    last_seen_str
                ]
            table.add_row(*row)

    def action_quit_app(self) -> None:
        self.app_ref.exit()


class CompareScreen(Screen):
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("s", "swap_panels", "Swap Panels"),
    ]

    def __init__(self, app_ref: Any, model1: str, model2: str):
        super().__init__()
        self.app_ref = app_ref
        self.model1 = model1
        self.model2 = model2
        self.left_target = model1
        self.right_target = model2

    def compose(self) -> ComposeResult:
        yield DriftHeader(id="main-header")
        with Container(id="compare-container"):
            with Container(id="compare-panel-left", classes="compare-panel"):
                yield Label("Panel Left", classes="watch-title", id="left-title")
                yield Label("Waiting...", id="left-footprint-box", classes="watch-data")
                yield Label("Waiting...", id="left-power-box", classes="watch-data")
                yield Label("TIMELINE (last 60s)", classes="watch-title")
                yield Label("▁" * 50, id="left-timeline")
                
            with Container(id="compare-panel-right", classes="compare-panel"):
                yield Label("Panel Right", classes="watch-title", id="right-title")
                yield Label("Waiting...", id="right-footprint-box", classes="watch-data")
                yield Label("Waiting...", id="right-power-box", classes="watch-data")
                yield Label("TIMELINE (last 60s)", classes="watch-title")
                yield Label("▁" * 50, id="right-timeline")
        yield Label("Comparing energy...", id="compare-footer")
        yield Footer()

    def action_quit_app(self) -> None:
        self.app_ref.exit()

    def action_swap_panels(self) -> None:
        self.left_target, self.right_target = self.right_target, self.left_target

    def find_process(self, target_str: str, processes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        target_lower = target_str.lower()
        for p in processes:
            if p["name"].lower() == target_lower:
                return p
            if p["model_name"] and p["model_name"].lower() == target_lower:
                return p
        for p in processes:
            if target_lower in p["name"].lower():
                return p
            if p["model_name"] and target_lower in p["model_name"].lower():
                return p
        return None

    def update_panel(self, side: str, target_str: str, target: Optional[Dict[str, Any]]) -> None:
        title_id = f"#{side}-title"
        fp_id = f"#{side}-footprint-box"
        pwr_id = f"#{side}-power-box"
        time_id = f"#{side}-timeline"
        
        if not target:
            pulse = ["...", "..", ".", ".."][int(time.time() * 2) % 4]
            self.query_one(title_id, Label).update(f"[dim]Waiting for {target_str}{pulse}[/]")
            self.query_one(fp_id, Label).update("—")
            self.query_one(pwr_id, Label).update("—")
            self.query_one(time_id, Label).update("▁" * 40)
            return

        name = target["name"]
        model = target["model_name"]
        model_suffix = f" ({model})" if model else ""
        self.query_one(title_id, Label).update(f"[bold #5DC9A5]🧠 {name}{model_suffix}[/] (PID {target['pid']})")
        
        current_fp = target["neural_footprint_mb"]
        fp_str = f"Footprint: {current_fp:.1f} MB"
        self.query_one(fp_id, Label).update(fp_str)
        
        pwr_str = f"Power: {target['ane_mw']/1000.0:.2f}W  ∑: {target['cumulative_mj']:,.0f} mJ"
        self.query_one(pwr_id, Label).update(pwr_str)
        
        try:
            term_width = os.get_terminal_size().columns
        except OSError:
            term_width = 80
        panel_width = max(15, (term_width // 2) - 6)
        spark = render_sparkline(target["footprint_history"], panel_width)
        self.query_one(time_id, Label).update(spark)

    def update_panels(self, data: Dict[str, Any]) -> None:
        left_proc = self.find_process(self.left_target, data["processes"])
        right_proc = self.find_process(self.right_target, data["processes"])
        
        self.update_panel("left", self.left_target, left_proc)
        self.update_panel("right", self.right_target, right_proc)
        
        footer_label = self.query_one("#compare-footer", Label)
        if left_proc and right_proc:
            left_e = left_proc["cumulative_mj"]
            right_e = right_proc["cumulative_mj"]
            left_name = left_proc["model_name"] or left_proc["name"]
            right_name = right_proc["model_name"] or right_proc["name"]
            
            if left_e > 0 and right_e > 0:
                if left_e >= right_e:
                    ratio = left_e / right_e
                    footer_text = f"[bold #5DC9A5]{left_name}[/] is using [bold #E8A045]{ratio:.1f}x[/] more energy than [bold #5DC9A5]{right_name}[/]"
                else:
                    ratio = right_e / left_e
                    footer_text = f"[bold #5DC9A5]{right_name}[/] is using [bold #E8A045]{ratio:.1f}x[/] more energy than [bold #5DC9A5]{left_name}[/]"
            else:
                left_p = left_proc["ane_mw"]
                right_p = right_proc["ane_mw"]
                if left_p > 0 and right_p > 0:
                    if left_p >= right_p:
                        ratio = left_p / right_p
                        footer_text = f"[bold #5DC9A5]{left_name}[/] is using [bold #E8A045]{ratio:.1f}x[/] more power than [bold #5DC9A5]{right_name}[/]"
                    else:
                        ratio = right_p / left_p
                        footer_text = f"[bold #5DC9A5]{right_name}[/] is using [bold #E8A045]{ratio:.1f}x[/] more power than [bold #5DC9A5]{left_name}[/]"
                else:
                    footer_text = f"Comparing energy between [bold #5DC9A5]{left_name}[/] and [bold #5DC9A5]{right_name}[/]..."
        else:
            footer_text = "Waiting for both processes to run to compare energy..."
        footer_label.update(footer_text)

class DriftApp(App):
    CSS_PATH = "drift.tcss"
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("p", "toggle_pause", "Pause"),
        ("s", "toggle_sort", "Sort"),
        ("e", "sort_mj", "Sort ∑mJ"),
        ("i", "toggle_ipc", "Toggle IPC"),
        ("w", "watch_mode", "Watch"),
        ("r", "reset", "Reset"),
        ("t", "switch_monitor", "Toggle Monitor"),
        ("tab", "cycle_screens", "Cycle Screens"),
        ("?", "help", "Help")
    ]

    def __init__(self, interval_ms: int, watch_target: Optional[str] = None, top_n: Optional[int] = None, filter_name: Optional[str] = None,
                 mode: str = "main", budget: int = 0, silent: bool = False, notify_on_spike: Optional[int] = None, notify_on_idle: Optional[int] = None,
                 record_file: Optional[str] = None, replay_file: Optional[str] = None, replay_speed: float = 1.0, compare_models: Optional[Tuple[str, str]] = None,
                 thermal: bool = False, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.interval_ms = interval_ms
        self.watch_target = watch_target
        self.top_n = top_n
        self.filter_name = filter_name
        self.mode = mode
        self.thermal = thermal
        self.history_logger = HistoryLogger()
        self.running_models = {}
        self.event_log = []
        self.last_history_sample_time = 0.0
        self.budget = budget or 0
        self.silent = silent
        self.notify_on_spike = notify_on_spike
        self.notify_on_idle = notify_on_idle
        
        self.record_file = record_file
        self.write_queue = queue.Queue()
        self.db_writer_thread = None
        
        self.replay_file = replay_file
        self.replay_speed = replay_speed or 1.0
        self.replay_proc_histories = {}
        self.replay_total_history = deque(maxlen=60)
        self.replay_peak_mw = 0.0
        self.replay_paused = False
        
        self.compare_models = compare_models
        
        self.collector = DataCollector(interval_ms)
        self.paused = False
        self.sort_mode = SortMode.ANE_PCT
        self.stop_event = threading.Event()
        self.bg_thread: Optional[threading.Thread] = None
        
        self.has_flashed_80 = False
        self.has_notified_100 = False
        self.last_notify_time = {}

    def on_mount(self) -> None:
        if self.record_file:
            self.db_writer_thread = threading.Thread(target=self.db_writer_worker, daemon=True)
            self.db_writer_thread.start()
                
        if self.mode == "top":
            self.push_screen(LeaderboardScreen(self))
        elif self.mode == "replay":
            self.replay_frames = self.load_replay_file(self.replay_file)
            self.replay_index = 0
            self.replay_paused = False
            self.bg_thread = threading.Thread(target=self.replay_loop, daemon=True)
            self.bg_thread.start()
            return
        elif self.mode == "compare":
            m1, m2 = self.compare_models
            self.push_screen(CompareScreen(self, m1, m2))
        elif self.mode == "monitor":
            self.push_screen(MainScreen(self))
            if self.watch_target:
                self.action_watch_mode(self.watch_target)
        else:
            self.push_screen(FingerprintScreen(self))
            
        self.bg_thread = threading.Thread(target=self.collect_loop, daemon=True)
        self.bg_thread.start()

    def on_unmount(self) -> None:
        self.stop_event.set()
        if self.bg_thread:
            self.bg_thread.join(timeout=2.0)
        if self.record_file:
            self.write_queue.put(None)
            if self.db_writer_thread:
                self.db_writer_thread.join(timeout=2.0)
        if hasattr(self, "history_logger") and self.history_logger:
            self.history_logger.stop()

    def db_writer_worker(self) -> None:
        try:
            if os.path.exists(self.record_file):
                try:
                    os.remove(self.record_file)
                except Exception as e:
                    logging.warning(f"Could not remove existing database file {self.record_file}: {e}")
            
            conn = sqlite3.connect(self.record_file)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ns INTEGER,
                ane_util_pct REAL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS process_samples (
                snapshot_id INTEGER,
                pid INTEGER,
                process_name TEXT,
                footprint_kb INTEGER,
                power_mw REAL,
                energy_nj INTEGER,
                model TEXT,
                ts REAL,
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_process_samples_snapshot ON process_samples(snapshot_id);")
            conn.commit()
        except Exception as e:
            logging.error(f"Failed to initialize SQLite recording database: {e}")
            return

        while True:
            item = self.write_queue.get()
            if item is None:
                self.write_queue.task_done()
                break
            
            try:
                snapshot_data, processes_data = item
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO snapshots (timestamp_ns, ane_util_pct) VALUES (?, ?);",
                    (snapshot_data["timestamp_ns"], snapshot_data["ane_util_pct"])
                )
                snapshot_id = cursor.lastrowid
                
                if processes_data:
                    cursor.executemany(
                        """
                        INSERT INTO process_samples 
                        (snapshot_id, pid, process_name, footprint_kb, power_mw, energy_nj, model, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        [(snapshot_id, p["pid"], p["process_name"], p["footprint_kb"], p["power_mw"], p["energy_nj"], p["model"], p["ts"]) for p in processes_data]
                    )
                conn.commit()
            except Exception as e:
                logging.error(f"Failed to write snapshot to SQLite: {e}")
            finally:
                self.write_queue.task_done()
        
        try:
            conn.close()
        except Exception as e:
            logging.error(f"Error closing SQLite database: {e}")

    def load_replay_file(self, path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            logging.error(f"Replay file {path} does not exist.")
            print(f"Error: Replay file {path} does not exist.")
            sys.exit(1)
            
        frames = []
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            snapshots = cursor.execute(
                "SELECT id, timestamp_ns, ane_util_pct FROM snapshots ORDER BY id ASC;"
            ).fetchall()
            
            samples = cursor.execute(
                "SELECT snapshot_id, pid, process_name, footprint_kb, power_mw, energy_nj, model FROM process_samples;"
            ).fetchall()
            
            samples_by_snapshot = {}
            for sample in samples:
                sid = sample["snapshot_id"]
                if sid not in samples_by_snapshot:
                    samples_by_snapshot[sid] = []
                samples_by_snapshot[sid].append({
                    "pid": sample["pid"],
                    "name": sample["process_name"],
                    "footprint_kb": sample["footprint_kb"],
                    "power_mw": sample["power_mw"],
                    "energy_nj": sample["energy_nj"],
                    "model": sample["model"]
                })
                
            for snap in snapshots:
                sid = snap["id"]
                frames.append({
                    "timestamp_ns": snap["timestamp_ns"],
                    "ane_util_pct": snap["ane_util_pct"],
                    "processes": samples_by_snapshot.get(sid, [])
                })
                
            conn.close()
        except Exception as e:
            logging.error(f"Error reading SQLite replay file: {e}")
            print(f"Error reading SQLite replay file: {e}")
            sys.exit(1)
            
        if not frames:
            logging.error("Replay database is empty.")
            print("Error: Replay database is empty.")
            sys.exit(1)
            
        return frames

    def frame_to_snapshot(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        processes = []
        for p in frame["processes"]:
            pid = p["pid"]
            if pid not in self.replay_proc_histories:
                self.replay_proc_histories[pid] = {
                    "history": deque(maxlen=60),
                    "footprint_history": deque(maxlen=240),
                    "power_history": deque(maxlen=240)
                }
            hist = self.replay_proc_histories[pid]
            footprint_mb = p["footprint_kb"] / 1024.0
            power_mw = p["power_mw"]
            ane_pct = min(100.0, (power_mw / 28000.0) * 100.0)
            
            hist["history"].append(ane_pct)
            hist["footprint_history"].append(footprint_mb)
            hist["power_history"].append(power_mw)
            
            processes.append({
                "pid": pid,
                "name": p["name"],
                "ane_pct": ane_pct,
                "ane_mw": power_mw,
                "cumulative_mj": p["energy_nj"] / 1e6,
                "delta_energy_mj": 0.0,
                "neural_footprint_mb": footprint_mb,
                "peak_neural_footprint_mb": footprint_mb,
                "interval_peak_mb": footprint_mb,
                "ipc": 0.0,
                "valid_ipc": False,
                "model_name": p["model"],
                "model_framework": "unknown",
                "model_size_gb": 0.0,
                "history": list(hist["history"]),
                "footprint_history": list(hist["footprint_history"]),
                "power_history": list(hist["power_history"])
            })
            
        self.replay_total_history.append(frame["ane_util_pct"])
        total_ane_mw = sum(p["ane_mw"] for p in processes)
        self.replay_peak_mw = max(self.replay_peak_mw, total_ane_mw)
        
        return {
            "ts": frame["timestamp_ns"] / 1e9,
            "total_ane_pct": frame["ane_util_pct"],
            "total_ane_mw": total_ane_mw,
            "peak_mw": self.replay_peak_mw,
            "total_history": list(self.replay_total_history),
            "budget_mw": 28000.0,
            "processes": processes
        }

    def replay_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.replay_paused:
                time.sleep(0.1)
                continue
                
            if self.replay_index >= len(self.replay_frames):
                self.replay_paused = True
                self.replay_index = len(self.replay_frames) - 1
                continue
                
            frame = self.replay_frames[self.replay_index]
            snapshot = self.frame_to_snapshot(frame)
            self.call_from_thread(self.update_display, snapshot)
            
            self.replay_index += 1
            if self.replay_index < len(self.replay_frames):
                next_frame = self.replay_frames[self.replay_index]
                delta_ns = next_frame["timestamp_ns"] - frame["timestamp_ns"]
                delta_s = max(0.0, delta_ns / 1e9)
                sleep_s = delta_s / self.replay_speed
                time.sleep(sleep_s)

    def collect_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.paused:
                self.collector.collect()
                data = self.collector.get_snapshot()
                
                if self.record_file:
                    self.write_record_frame(data)
                    
                self.call_from_thread(self.update_display, data)
            time.sleep(self.interval_ms / 1000.0)

    def write_record_frame(self, data: Dict[str, Any]) -> None:
        frame_processes = []
        for p in data["processes"]:
            if p["neural_footprint_mb"] > 0.1 or p["cumulative_mj"] > 0.0 or p["ane_mw"] > 0.0:
                frame_processes.append({
                    "pid": p["pid"],
                    "process_name": p["name"],
                    "footprint_kb": int(p["neural_footprint_mb"] * 1024),
                    "power_mw": p["ane_mw"],
                    "energy_nj": int(p["cumulative_mj"] * 1e6),
                    "model": p["model_name"],
                    "ts": data["ts"]
                })
        
        snapshot_data = {
            "timestamp_ns": int(data["ts"] * 1e9),
            "ane_util_pct": data["total_ane_pct"]
        }
        self.write_queue.put((snapshot_data, frame_processes))

    def update_display(self, data: Dict[str, Any]) -> None:
        # Always-on model start/stop detection
        now = time.time()
        current_active = []
        for p in data["processes"]:
            model_name = p.get("model_name")
            pid = p["pid"]
            cumulative_mj = p["cumulative_mj"]
            framework = p.get("model_framework") or "unknown"
            
            if model_name:
                current_active.append((pid, model_name))
                if (pid, model_name) not in self.running_models:
                    for prev_key in list(self.running_models.keys()):
                        prev_pid, prev_model = prev_key
                        if prev_pid == pid:
                            self.app_stop_model(prev_pid, prev_model, cumulative_mj)
                    self.app_start_model(pid, model_name, framework, cumulative_mj)
                else:
                    self.running_models[(pid, model_name)]["last_energy"] = cumulative_mj

        for key in list(self.running_models.keys()):
            pid, model_name = key
            if key not in current_active:
                latest_energy = 0.0
                for p in self.collector.session_processes.values():
                    if p.pid == pid:
                        latest_energy = p.cumulative_mj
                        break
                self.app_stop_model(pid, model_name, latest_energy)

        # Log history sample every 10 seconds
        now_mon = time.monotonic()
        if not hasattr(self, "last_history_sample_time") or now_mon - self.last_history_sample_time >= 10.0:
            self.last_history_sample_time = now_mon
            if self.history_logger:
                self.history_logger.log_sample(data["total_ane_pct"], data.get("temp"))

        try:
            header = self.query_one("#main-header", DriftHeader)
            if data["peak_mw"] > header.peak_mw:
                header.peak_mw = data["peak_mw"]
                header.flash = True
                self.set_timer(0.5, lambda: setattr(header, 'flash', False))
            header.budget = self.budget
            header.cumulative_mj = data.get("session_cumulative_mj", 0.0)
            header.temp = data.get("temp")
            header.sensor_key = data.get("sensor_key")
        except NoMatches:
            pass

        if isinstance(self.screen, FingerprintScreen):
            self.screen.update_fingerprints(data)
            return

        if isinstance(self.screen, LeaderboardScreen):
            self.screen.update_leaderboard()
            return
            
        if isinstance(self.screen, CompareScreen):
            self.screen.update_panels(data)
            return

        if isinstance(self.screen, WatchScreen):
            self.screen.update_data(data)
            return
            
        if not isinstance(self.screen, MainScreen):
            return

        if self.budget > 0:
            cum_mj = data.get("session_cumulative_mj", 0.0)
            pct = (cum_mj / self.budget) * 100.0
            if pct >= 80.0 and not self.has_flashed_80:
                self.has_flashed_80 = True
                try:
                    header = self.query_one("#main-header", DriftHeader)
                    header.flash = True
                    self.set_timer(0.5, lambda: setattr(header, 'flash', False))
                except NoMatches:
                    pass
            if pct >= 100.0 and not self.has_notified_100:
                self.has_notified_100 = True
                self.fire_notification(f"ANE budget of {self.budget} mJ exceeded")

        try:
            util_bar = self.query_one("#util-bar", UtilBar)
            util_bar.pct = data["total_ane_pct"]
            util_bar.mw = data["total_ane_mw"]
            util_bar.budget = data["budget_mw"]
            
            global_spark = self.query_one("#global-sparkline", GlobalSparkline)
            global_spark.history = data["total_history"]
        except NoMatches:
            pass
        
        try:
            table = self.query_one("#proc-table", DataTable)
        except NoMatches:
            return
            
        procs = data["processes"]
        if self.filter_name:
            procs = [p for p in procs if self.filter_name.lower() in p["name"].lower()]
            
        valid_procs = [p for p in procs if p["ane_pct"] > 0.1 or p["neural_footprint_mb"] > 1.0]
        
        if self.sort_mode == SortMode.ANE_PCT:
            valid_procs.sort(key=lambda x: x["ane_pct"], reverse=True)
        elif self.sort_mode == SortMode.CUMULATIVE_MJ:
            valid_procs.sort(key=lambda x: x["cumulative_mj"], reverse=True)
        elif self.sort_mode == SortMode.FOOTPRINT:
            valid_procs.sort(key=lambda x: x["neural_footprint_mb"], reverse=True)
        elif self.sort_mode == SortMode.NAME:
            valid_procs.sort(key=lambda x: x["name"].lower())
        elif self.sort_mode == SortMode.IPC:
            valid_procs.sort(key=lambda x: x["ipc"], reverse=True)
            
        if self.top_n:
            valid_procs = valid_procs[:self.top_n]
            
        table.clear()
        
        try:
            term_width = os.get_terminal_size().columns
        except OSError:
            term_width = 80
        show_model = term_width >= 120
        show_mj = term_width >= 100
        show_ipc = term_width >= 100 and self.screen.show_ipc
        show_peak = term_width >= 80

        for p in valid_procs:
            spark = render_sparkline(p["history"])
            row = [
                classify_name(p["name"]),
                str(p["pid"]),
                colorize_pct(p["ane_pct"]),
                f"{p['ane_mw']:.1f}"
            ]
            
            if show_model:
                model_str = p.get("model_name") or "—"
                if len(model_str) > 24:
                    model_str = model_str[:22] + "…"
                row.append(f"[#4EC9B0]{model_str}[/]")
            if show_ipc:
                row.append(colorize_ipc(p["ipc"], p["valid_ipc"]))
            if show_mj:
                row.append(f"{p['cumulative_mj']:,.1f}")
            if show_peak:
                row.append(format_peak(p["neural_footprint_mb"], p["peak_neural_footprint_mb"]))
                
            row.append(spark)
            table.add_row(*row)
            
    def action_quit_app(self) -> None:
        self.exit()
        
    def action_toggle_pause(self) -> None:
        if self.mode == "replay":
            self.replay_paused = not self.replay_paused
        else:
            self.paused = not self.paused
        
    def action_toggle_sort(self) -> None:
        modes = list(SortMode)
        idx = modes.index(self.sort_mode)
        self.sort_mode = modes[(idx + 1) % len(modes)]
        
    def action_sort_mj(self) -> None:
        self.sort_mode = SortMode.CUMULATIVE_MJ
        
    def action_toggle_ipc(self) -> None:
        if isinstance(self.screen, MainScreen):
            self.screen.action_toggle_ipc()
            
    def action_watch_mode(self, target: Optional[str] = None) -> None:
        if target is None:
            if isinstance(self.screen, MainScreen):
                try:
                    table = self.query_one("#proc-table", DataTable)
                    if table.row_count > 0:
                        row_data = table.get_row_at(table.cursor_coordinate.row)
                        pid_str = row_data[1]
                        target = str(pid_str)
                        pid = int(pid_str)
                        import re
                        name = re.sub(r'\[.*?\]', '', row_data[0])
                        self.push_screen(WatchScreen(self, name, pid))
                except Exception:
                    pass
            return
            
        data = self.collector.get_snapshot()
        matches = [p for p in data["processes"] if target.lower() in p["name"].lower()]
        
        if len(matches) > 1:
            self.push_screen(PickerScreen(self, matches))
        else:
            pid = matches[0]["pid"] if len(matches) == 1 else None
            self.push_screen(WatchScreen(self, target, pid))
            
    def action_reset(self) -> None:
        with self.collector.lock:
            for p in self.collector.processes.values():
                p.history.clear()
                p.footprint_history.clear()
                p.power_history.clear()
                p.cumulative_mj = 0.0
                p.peak_mw = 0.0
            self.collector.session_processes.clear()
            self.collector.processes.clear()
            self.collector.peak_history.clear()
            self.collector.peak_mw = 0.0
            self.collector.session_cumulative_mj = 0.0
        self.has_flashed_80 = False
        self.has_notified_100 = False

    def action_replay_back(self) -> None:
        if self.mode != "replay":
            return
        frames = self.replay_frames
        t_curr = frames[min(self.replay_index, len(frames)-1)]["timestamp_ns"]
        t_target = t_curr - 10 * 1e9
        new_idx = 0
        for idx, f in enumerate(frames):
            if f["timestamp_ns"] >= t_target:
                new_idx = idx
                break
        self.replay_index = new_idx

    def action_replay_forward(self) -> None:
        if self.mode != "replay":
            return
        frames = self.replay_frames
        t_curr = frames[min(self.replay_index, len(frames)-1)]["timestamp_ns"]
        t_target = t_curr + 10 * 1e9
        new_idx = len(frames) - 1
        for idx, f in enumerate(frames):
            if f["timestamp_ns"] >= t_target:
                new_idx = idx
                break
        self.replay_index = new_idx

    def action_switch_monitor(self) -> None:
        if isinstance(self.screen, FingerprintScreen):
            self.pop_screen()
            self.push_screen(MainScreen(self))
        elif isinstance(self.screen, MainScreen):
            self.pop_screen()
            self.push_screen(FingerprintScreen(self))

    def action_cycle_screens(self) -> None:
        if isinstance(self.screen, FingerprintScreen):
            self.pop_screen()
            self.push_screen(LeaderboardScreen(self))
        elif isinstance(self.screen, LeaderboardScreen):
            self.pop_screen()
            if self.compare_models:
                m1, m2 = self.compare_models
                self.push_screen(CompareScreen(self, m1, m2))
            else:
                self.push_screen(FingerprintScreen(self))
        elif isinstance(self.screen, CompareScreen):
            self.pop_screen()
            self.push_screen(FingerprintScreen(self))

    def app_start_model(self, pid: int, model_name: str, framework: str, start_energy: float) -> None:
        fw_display = framework.title() if framework else "Unknown"
        ts_str = datetime.datetime.now().strftime("%H:%M:%S")
        log_line = f"[#888888]{ts_str}[/]  [bold #00E676]▶[/]  [bold #ffffff]{model_name}[/]  [dim]({fw_display})[/]  [#00E676]started[/]"
        self.event_log.append(log_line)
        if len(self.event_log) > 500:
            self.event_log.pop(0)
            
        if isinstance(self.screen, FingerprintScreen):
            try:
                log_widget = self.screen.query_one("#inference-log", RichLog)
                log_widget.write(log_line)
            except Exception:
                pass
                
        process_name = "unknown"
        if hasattr(self, "collector") and self.collector:
            for (p_pid, _), proc in self.collector.session_processes.items():
                if p_pid == pid:
                    process_name = proc.name
                    break
        if process_name == "unknown":
            try:
                process_name = psutil.Process(pid).name()
            except Exception:
                pass
                
        if self.history_logger:
            self.history_logger.log_event(
                pid=pid,
                process_name=process_name,
                model_name=model_name,
                framework=framework,
                peak_power_mw=0.0,
                total_energy_mj=0.0,
                event_type="start"
            )
            
        self.running_models[(pid, model_name)] = {
            "start_time": time.time(),
            "start_energy": start_energy,
            "last_energy": start_energy,
            "peak_power": 0.0,
            "framework": framework
        }

    def app_stop_model(self, pid: int, model_name: str, end_energy: float) -> None:
        key = (pid, model_name)
        if key not in self.running_models:
            return
            
        info = self.running_models.pop(key)
        start_energy = info["start_energy"]
        total_mj = max(0.0, end_energy - start_energy)
        
        ts_str = datetime.datetime.now().strftime("%H:%M:%S")
        log_line = f"[#888888]{ts_str}[/]  [bold #FF1744]■[/]  [bold #ffffff]{model_name}[/]  [dim]({info['framework'].title()})[/]  [#FF1744]stopped[/]  [dim]—[/]  [bold #E8A045]{total_mj:,.0f} mJ total[/]"
        self.event_log.append(log_line)
        if len(self.event_log) > 500:
            self.event_log.pop(0)
            
        if isinstance(self.screen, FingerprintScreen):
            try:
                log_widget = self.screen.query_one("#inference-log", RichLog)
                log_widget.write(log_line)
            except Exception:
                pass
                
        process_name = "unknown"
        if hasattr(self, "collector") and self.collector:
            for (p_pid, _), proc in self.collector.session_processes.items():
                if p_pid == pid:
                    process_name = proc.name
                    break
        if process_name == "unknown":
            try:
                process_name = psutil.Process(pid).name()
            except Exception:
                pass
                
        if self.history_logger:
            peak_power_mw = 0.0
            if hasattr(self, "collector") and self.collector:
                for (p_pid, _), proc in self.collector.session_processes.items():
                    if p_pid == pid:
                        peak_power_mw = proc.peak_mw
                        break
            self.history_logger.log_event(
                pid=pid,
                process_name=process_name,
                model_name=model_name,
                framework=info["framework"],
                peak_power_mw=peak_power_mw,
                total_energy_mj=total_mj,
                event_type="stop"
            )

    def fire_notification(self, message: str, subtitle: Optional[str] = None) -> None:
        if self.silent:
            return
        import subprocess
        msg_escaped = message.replace('"', '\\"')
        sub_escaped = subtitle.replace('"', '\\"') if subtitle else ""
        sub_arg = f' subtitle "{sub_escaped}"' if sub_escaped else ""
        cmd = f'display notification "{msg_escaped}" with title "drift ⚡"{sub_arg} sound name "Funk"'
        subprocess.Popen(['osascript', '-e', cmd])

def render_sparkline_temp(values: List[float], width: int = 12) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return "▁" * width
        
    min_val = min(values)
    max_val = max(values)
    rng = max_val - min_val
    
    scaled = []
    if len(values) < width:
        scaled = [None] * (width - len(values)) + values
    elif len(values) > width:
        step = len(values) / width
        for i in range(width):
            idx = int(i * step)
            scaled.append(values[idx])
    else:
        scaled = values

    result = []
    for temp in scaled:
        if temp is None or temp == 0.0:
            result.append("▁")
        else:
            if rng > 0:
                h = min(7, int(((temp - min_val) / rng) * 7))
            else:
                h = 0
            char = blocks[h]
            if temp < 60.0:
                result.append(char)
            elif temp <= 80.0:
                result.append(f"[#E8A045]{char}[/]")
            else:
                result.append(f"[#E05C5C]{char}[/]")
    return "".join(result)

def run_ps_once(args: argparse.Namespace) -> None:
    import psutil
    scanner = ModelScanner()
    
    pids1 = set(psutil.pids())
    t1 = time.time()
    rusage1 = {}
    for pid in pids1:
        info = rusage_info_v6()
        if proc_pid_rusage(pid, RUSAGE_INFO_V6, ctypes.byref(info)) == 0:
            rusage1[pid] = (info.ri_energy_nj, getattr(info, "ri_neural_footprint", 0))
            
    time.sleep(0.2)
    
    pids2 = set(psutil.pids())
    t2 = time.time()
    dt = t2 - t1
    
    rows = []
    for pid in pids2:
        info2 = rusage_info_v6()
        if proc_pid_rusage(pid, RUSAGE_INFO_V6, ctypes.byref(info2)) == 0:
            footprint = getattr(info2, "ri_neural_footprint", 0)
            if footprint == 0:
                continue
                
            energy2 = getattr(info2, "ri_energy_nj", 0)
            power_mw = 0.0
            if pid in rusage1:
                energy1, fp1 = rusage1[pid]
                de_nj = energy2 - energy1
                if de_nj > 0 and dt > 0:
                    power_mw = (de_nj / 1e6) / dt
                    
            try:
                p = psutil.Process(pid)
                name = p.name()
            except Exception:
                name = "unknown"
                
            model_obj = scanner.get_primary_model(pid)
            model_name = model_obj.display if model_obj else ""
            
            rows.append({
                "pid": pid,
                "process": name,
                "model": model_name,
                "footprint_kb": int(footprint / 1024),
                "power_mw": power_mw,
                "energy_mj": energy2 / 1e6
            })
            
    if not rows:
        print("no ANE activity")
        sys.exit(0)
        
    sort_key = args.sort if hasattr(args, "sort") and args.sort else "power"
    if sort_key == "power":
        rows.sort(key=lambda r: r["power_mw"], reverse=True)
    elif sort_key == "energy":
        rows.sort(key=lambda r: r["energy_mj"], reverse=True)
    elif sort_key == "footprint":
        rows.sort(key=lambda r: r["footprint_kb"], reverse=True)
        
    for idx, r in enumerate(rows):
        r["rank"] = idx + 1
        
    if args.json:
        print(json.dumps(rows, indent=2))
        return
        
    term_width, _ = shutil.get_terminal_size()
    headers = ["RANK", "PID", "PROCESS", "MODEL", "FOOTPRINT (KB)", "POWER (mW)", "∑ mJ"]
    col_widths = {
        "rank": len(headers[0]),
        "pid": len(headers[1]),
        "process": len(headers[2]),
        "model": len(headers[3]),
        "footprint": len(headers[4]),
        "power": len(headers[5]),
        "energy": len(headers[6])
    }
    
    for r in rows:
        col_widths["rank"] = max(col_widths["rank"], len(str(r["rank"])))
        col_widths["pid"] = max(col_widths["pid"], len(str(r["pid"])))
        col_widths["process"] = max(col_widths["process"], len(r["process"]))
        col_widths["model"] = max(col_widths["model"], len(r["model"]))
        col_widths["footprint"] = max(col_widths["footprint"], len(f"{r['footprint_kb']:,}"))
        col_widths["power"] = max(col_widths["power"], len(f"{r['power_mw']:.1f}"))
        col_widths["energy"] = max(col_widths["energy"], len(f"{r['energy_mj']:,.1f}"))
        
    total_req = sum(col_widths.values()) + 20
    if total_req > term_width:
        diff = total_req - term_width
        if col_widths["model"] > len(headers[3]) + 5:
            shrink = min(diff, col_widths["model"] - (len(headers[3]) + 5))
            col_widths["model"] -= shrink
            diff -= shrink
        if diff > 0 and col_widths["process"] > len(headers[2]) + 5:
            shrink = min(diff, col_widths["process"] - (len(headers[2]) + 5))
            col_widths["process"] -= shrink
            diff -= shrink
            
    fmt_rank = f"{{:>{col_widths['rank']}}}"
    fmt_pid = f"{{:>{col_widths['pid']}}}"
    fmt_process = f"{{:<{col_widths['process']}}}"
    fmt_model = f"{{:<{col_widths['model']}}}"
    fmt_footprint = f"{{:>{col_widths['footprint']}}}"
    fmt_power = f"{{:>{col_widths['power']}}}"
    fmt_energy = f"{{:>{col_widths['energy']}}}"
    
    header_str = (
        fmt_rank.format("RANK") + " │ " +
        fmt_pid.format("PID") + " │ " +
        fmt_process.format("PROCESS") + " │ " +
        fmt_model.format("MODEL") + " │ " +
        fmt_footprint.format("FOOTPRINT (KB)") + " │ " +
        fmt_power.format("POWER (mW)") + " │ " +
        fmt_energy.format("∑ mJ")
    )
    
    separator_str = (
        "─" * col_widths['rank'] + "─┼─" +
        "─" * col_widths['pid'] + "─┼─" +
        "─" * col_widths['process'] + "─┼─" +
        "─" * col_widths['model'] + "─┼─" +
        "─" * col_widths['footprint'] + "─┼─" +
        "─" * col_widths['power'] + "─┼─" +
        "─" * col_widths['energy']
    )
    
    print(header_str)
    print(separator_str)
    
    for r in rows:
        proc_name = r["process"]
        if len(proc_name) > col_widths["process"]:
            proc_name = proc_name[:col_widths["process"]-3] + "..."
        model_name = r["model"]
        if len(model_name) > col_widths["model"]:
            model_name = model_name[:col_widths["model"]-3] + "..."
            
        line = (
            fmt_rank.format(r["rank"]) + " │ " +
            fmt_pid.format(r["pid"]) + " │ " +
            fmt_process.format(proc_name) + " │ " +
            fmt_model.format(model_name) + " │ " +
            fmt_footprint.format(f"{r['footprint_kb']:,}") + " │ " +
            fmt_power.format(f"{r['power_mw']:.1f}") + " │ " +
            fmt_energy.format(f"{r['energy_mj']:,.1f}")
        )
        print(line)

def handle_ps(args: argparse.Namespace) -> None:
    if platform.machine() != "arm64":
        sys.exit(1)
        
    watch_interval = args.watch if hasattr(args, "watch") else None
    
    try:
        if watch_interval is not None:
            while True:
                os.system("clear")
                run_ps_once(args)
                time.sleep(watch_interval)
        else:
            run_ps_once(args)
    except KeyboardInterrupt:
        sys.exit(0)

class IdleWidget(Static):
    def on_mount(self) -> None:
        self.update_text()
        self.animate_pulse()
        
    def update_text(self) -> None:
        self.update("[bold #888888]◌  ANE idle[/]")
        
    def animate_pulse(self) -> None:
        self.styles.opacity = 0.3
        self.animate("opacity", 1.0, duration=1.2, callback=self.animate_pulse_reverse)
        
    def animate_pulse_reverse(self) -> None:
        self.animate("opacity", 0.3, duration=1.2, callback=self.animate_pulse)

class FingerprintScreen(Screen):
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("l", "toggle_log", "Toggle Log"),
        ("t", "switch_monitor", "Toggle Monitor"),
        ("tab", "cycle_screens", "Cycle Screens"),
    ]
    
    def __init__(self, app_ref: Any):
        super().__init__()
        self.app_ref = app_ref
        self.show_log = True

    def compose(self) -> ComposeResult:
        yield DriftHeader(id="main-header")
        with Container(id="fingerprint-container"):
            with Container(id="live-panel"):
                yield Label("ACTIVE ANE MODELS", classes="section-title")
                yield DataTable(id="live-model-list")
                yield IdleWidget(id="ane-idle-widget")
            with Container(id="log-panel"):
                yield Label("INFERENCE EVENT LOG", classes="section-title")
                yield RichLog(id="inference-log", max_lines=500, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#live-model-list", DataTable)
        table.add_columns("MODEL", "FRAMEWORK", "POWER", "TREND")
        
        log_widget = self.query_one("#inference-log", RichLog)
        for log_entry in self.app_ref.event_log:
            log_widget.write(log_entry)

    def action_quit_app(self) -> None:
        self.app.exit()
        
    def action_toggle_log(self) -> None:
        self.show_log = not self.show_log
        try:
            log_panel = self.query_one("#log-panel", Container)
            log_panel.display = self.show_log
        except Exception:
            pass
            
    def action_switch_monitor(self) -> None:
        self.app.action_switch_monitor()
        
    def action_cycle_screens(self) -> None:
        self.app.action_cycle_screens()

    def update_fingerprints(self, data: Dict[str, Any]) -> None:
        active_procs = [p for p in data["processes"] if p.get("neural_footprint_mb", 0.0) > 0.0]
        
        try:
            table = self.query_one("#live-model-list", DataTable)
            idle = self.query_one("#ane-idle-widget", IdleWidget)
        except NoMatches:
            return
            
        if not active_procs:
            table.display = False
            idle.display = True
        else:
            table.display = True
            idle.display = False
            
            table.clear()
            for p in active_procs:
                model_name = p.get("model_name") or p["name"]
                model_styled = f"[bold #ffffff]{model_name}[/]"
                
                fw = (p.get("model_framework") or "unknown").lower()
                if "mlx" in fw:
                    fw_styled = "[bold #00E5FF]MLX[/]"
                elif "coreml" in fw:
                    fw_styled = "[bold #2979FF]CoreML[/]"
                elif "ollama" in fw:
                    fw_styled = "[bold #00E676]Ollama[/]"
                elif "llama" in fw:
                    fw_styled = "[bold #FFD600]llama.cpp[/]"
                else:
                    fw_styled = "[dim #888888]unknown[/]"
                    
                power = p.get("ane_mw", 0.0)
                power_styled = f"{power:,.1f} mW"
                
                power_hist = list(p.get("power_history", []))[-20:]
                spark = render_sparkline(power_hist, 20)
                
                table.add_row(model_styled, fw_styled, power_styled, spark)

class HistoryLogger:
    def __init__(self):
        self.db_dir = os.path.expanduser("~/.drift")
        try:
            os.makedirs(self.db_dir, exist_ok=True)
        except Exception:
            pass
        self.db_path = os.path.join(self.db_dir, "history.db")
        
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.session_id = None
        self.stop_event = threading.Event()
        self.thread.start()
        self.queue.put(("init_and_start_session", (platform.node(), get_chip_info().desc)))

    def _worker(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
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
        except Exception as e:
            logging.error(f"Failed to initialize history database: {e}")
            return

        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
                
            if item is None:
                self.queue.task_done()
                break
                
            action, args = item
            try:
                cursor = conn.cursor()
                if action == "init_and_start_session":
                    host, chip = args
                    cutoff = time.time() - 2592000
                    session_ids_to_prune = [r[0] for r in cursor.execute("SELECT id FROM sessions WHERE started_at < ?;", (cutoff,)).fetchall()]
                    if session_ids_to_prune:
                        placeholders = ",".join("?" for _ in session_ids_to_prune)
                        cursor.execute(f"DELETE FROM events WHERE session_id IN ({placeholders});", session_ids_to_prune)
                        cursor.execute(f"DELETE FROM samples WHERE session_id IN ({placeholders});", session_ids_to_prune)
                        cursor.execute(f"DELETE FROM sessions WHERE id IN ({placeholders});", session_ids_to_prune)
                    
                    cursor.execute(
                        "INSERT INTO sessions (started_at, ended_at, host, chip) VALUES (?, NULL, ?, ?);",
                        (time.time(), host, chip)
                    )
                    self.session_id = cursor.lastrowid
                    conn.commit()
                    
                elif action == "log_sample":
                    ane_util, temp = args
                    if self.session_id is not None:
                        cursor.execute(
                            "INSERT INTO samples (session_id, ts, ane_util_pct, die_temp_c) VALUES (?, ?, ?, ?);",
                            (self.session_id, time.time(), ane_util, temp)
                        )
                        conn.commit()
                        
                elif action == "log_event":
                    pid, proc_name, model_name, framework, peak_power, total_energy, event_type = args
                    if self.session_id is not None:
                        cursor.execute(
                            """
                            INSERT INTO events 
                            (session_id, ts, pid, process_name, model_name, framework, peak_power_mw, total_energy_mj, event_type)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                            """,
                            (self.session_id, time.time(), pid, proc_name, model_name, framework, peak_power, total_energy, event_type)
                        )
                        conn.commit()
                        
                elif action == "end_session":
                    if self.session_id is not None:
                        cursor.execute(
                            "UPDATE sessions SET ended_at = ? WHERE id = ?;",
                            (time.time(), self.session_id)
                        )
                        conn.commit()
            except Exception as e:
                logging.error(f"HistoryLogger SQLite error in action '{action}': {e}")
            finally:
                self.queue.task_done()
                
        try:
            conn.close()
        except Exception:
            pass

    def log_sample(self, ane_util_pct: float, die_temp_c: Optional[float]) -> None:
        self.queue.put(("log_sample", (ane_util_pct, die_temp_c)))

    def log_event(self, pid: int, process_name: str, model_name: str, framework: str, peak_power_mw: float, total_energy_mj: float, event_type: str) -> None:
        self.queue.put(("log_event", (pid, process_name, model_name, framework, peak_power_mw, total_energy_mj, event_type)))

    def stop(self) -> None:
        self.queue.put(("end_session", ()))
        self.stop_event.set()
        self.queue.put(None)
        self.thread.join(timeout=2.0)

def format_duration(seconds: float) -> str:
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}h {m}m"
    elif m > 0:
        return f"{m}m {sec}s"
    else:
        return f"{sec}s"

def format_session_duration(seconds: float) -> str:
    s = int(seconds)
    m = s // 60
    sec = s % 60
    return f"{m}m {sec:02d}s"

def print_weekly_summary(conn: sqlite3.Connection, today_only: bool = False) -> None:
    now = time.time()
    now_dt = datetime.datetime.fromtimestamp(now)
    if today_only:
        start_dt = datetime.datetime(now_dt.year, now_dt.month, now_dt.day)
        end_dt = start_dt + datetime.timedelta(days=1)
        title = f"drift  —  today {start_dt.strftime('%b %d %Y')}"
    else:
        monday = now_dt.date() - datetime.timedelta(days=now_dt.weekday())
        start_dt = datetime.datetime(monday.year, monday.month, monday.day)
        end_dt = start_dt + datetime.timedelta(days=7)
        title = f"drift  —  week of {start_dt.strftime('%b %d %Y')}"
        
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()
    
    cursor = conn.cursor()
    sessions_cnt = cursor.execute(
        "SELECT COUNT(*) FROM sessions WHERE started_at >= ? AND started_at < ?;",
        (start_ts, end_ts)
    ).fetchone()[0]
    
    total_energy_mj = cursor.execute(
        "SELECT SUM(total_energy_mj) FROM events WHERE event_type = 'stop' AND ts >= ? AND ts < ?;",
        (start_ts, end_ts)
    ).fetchone()[0] or 0.0
    
    active_samples = cursor.execute(
        "SELECT COUNT(*) FROM samples WHERE ane_util_pct > 0.0 AND ts >= ? AND ts < ?;",
        (start_ts, end_ts)
    ).fetchone()[0]
    active_time_s = active_samples * 10.0
    
    top_models = cursor.execute(
        """
        SELECT model_name, SUM(total_energy_mj) as energy 
        FROM events 
        WHERE event_type = 'stop' AND ts >= ? AND ts < ? AND model_name IS NOT NULL AND model_name != ''
        GROUP BY model_name 
        ORDER BY energy DESC 
        LIMIT 5;
        """,
        (start_ts, end_ts)
    ).fetchall()
    
    busiest_hour_row = cursor.execute(
        """
        SELECT strftime('%H', datetime(ts, 'unixepoch', 'localtime')) as hr, COUNT(*) as cnt
        FROM samples
        WHERE ane_util_pct > 0.0 AND ts >= ? AND ts < ?
        GROUP BY hr
        ORDER BY cnt DESC
        LIMIT 1;
        """,
        (start_ts, end_ts)
    ).fetchone()
    
    busiest_hour_str = "—"
    if busiest_hour_row:
        hr_int = int(busiest_hour_row[0])
        busiest_hour_str = f"{hr_int:02d}:00 – {hr_int+1:02d}:00"
        
    width = 38
    print(f"╔{'═' * width}╗")
    
    title_padded = f"  {title}".ljust(width)
    print(f"║{title_padded}║")
    print(f"╠{'═' * width}╣")
    
    active_dur = format_duration(active_time_s)
    line = f"  ANE active      {active_dur}".ljust(width)
    print(f"║{line}║")
    
    energy_str = f"{total_energy_mj:,.0f} mJ"
    line = f"  total energy    {energy_str}".ljust(width)
    print(f"║{line}║")
    
    line = f"  sessions        {sessions_cnt}".ljust(width)
    print(f"║{line}║")
    print(f"╠{'═' * width}╣")
    
    line = "  top models".ljust(width)
    print(f"║{line}║")
    
    if not top_models:
        line = "  (none)".ljust(width)
        print(f"║{line}║")
    else:
        for idx, (m_name, energy) in enumerate(top_models):
            rank = idx + 1
            m_display = m_name
            if len(m_display) > 18:
                m_display = m_display[:16] + ".."
            energy_val_str = f"{energy:,.0f} mJ"
            line = f"  {rank}  {m_display:<18} {energy_val_str:>12}".ljust(width)
            print(f"║{line}║")
            
    print(f"╠{'═' * width}╣")
    line = f"  busiest hour    {busiest_hour_str}".ljust(width)
    print(f"║{line}║")
    print(f"╚{'═' * width}╝")

def list_sessions(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    sessions = cursor.execute(
        "SELECT id, started_at, ended_at FROM sessions ORDER BY started_at DESC LIMIT 50;"
    ).fetchall()
    
    if not sessions:
        print("No sessions recorded yet.")
        return
        
    for s_id, start_ts, end_ts in sessions:
        dt = datetime.datetime.fromtimestamp(start_ts)
        start_str = dt.strftime("%Y-%m-%d %H:%M")
        
        duration_s = (end_ts or time.time()) - start_ts
        dur_str = format_session_duration(duration_s)
        
        energy = cursor.execute(
            "SELECT SUM(total_energy_mj) FROM events WHERE session_id = ? AND event_type = 'stop';",
            (s_id,)
        ).fetchone()[0] or 0.0
        energy_str = f"{energy:,.0f} mJ"
        
        models_count = cursor.execute(
            "SELECT COUNT(DISTINCT model_name) FROM events WHERE session_id = ? AND model_name IS NOT NULL AND model_name != '';",
            (s_id,)
        ).fetchone()[0] or 0
        
        model_word = "models" if models_count != 1 else "model"
        models_str = f"{models_count} {model_word}"
        
        print(f"{start_str}  {dur_str:>8}  {energy_str:>8}   {models_str}")

def query_model_history(conn: sqlite3.Connection, model_pattern: str) -> None:
    cursor = conn.cursor()
    like_pattern = f"%{model_pattern}%"
    
    matched_models = [r[0] for r in cursor.execute(
        "SELECT DISTINCT model_name FROM events WHERE model_name LIKE ? AND model_name IS NOT NULL AND model_name != '';",
        (like_pattern,)
    ).fetchall()]
    
    if not matched_models:
        print(f"No history found for model matching '{model_pattern}'")
        return
        
    for m_name in matched_models:
        runs = cursor.execute(
            "SELECT COUNT(*) FROM events WHERE model_name = ? AND event_type = 'start';",
            (m_name,)
        ).fetchone()[0] or 0
        
        sessions = cursor.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE model_name = ?;",
            (m_name,)
        ).fetchone()[0] or 0
        
        total_energy = cursor.execute(
            "SELECT SUM(total_energy_mj) FROM events WHERE model_name = ? AND event_type = 'stop';",
            (m_name,)
        ).fetchone()[0] or 0.0
        
        peak_power = cursor.execute(
            "SELECT MAX(peak_power_mw) FROM events WHERE model_name = ?;",
            (m_name,)
        ).fetchone()[0] or 0.0
        
        avg_energy = total_energy / runs if runs > 0 else 0.0
        
        print(f"MODEL STATS: {m_name}")
        print("───────────────────────────────")
        print(f"  sessions active  {sessions}")
        print(f"  total runs       {runs}")
        print(f"  total energy     {total_energy:,.0f} mJ")
        print(f"  avg energy/run   {avg_energy:,.0f} mJ")
        print(f"  peak power       {peak_power:,.0f} mW")
        print()

def export_history(conn: sqlite3.Connection, filepath: str) -> None:
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    
    sessions = [dict(r) for r in cursor.execute("SELECT * FROM sessions ORDER BY id ASC;").fetchall()]
    events = [dict(r) for r in cursor.execute("SELECT * FROM events ORDER BY id ASC;").fetchall()]
    samples = [dict(r) for r in cursor.execute("SELECT * FROM samples ORDER BY id ASC;").fetchall()]
    
    data = {
        "sessions": sessions,
        "events": events,
        "samples": samples
    }
    
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Exported history to: {filepath}")
    except Exception as e:
        print(f"Error exporting history to {filepath}: {e}")
        sys.exit(1)

def handle_history(args: argparse.Namespace) -> None:
    db_path = os.path.expanduser("~/.drift/history.db")
    if not os.path.exists(db_path):
        print("No history recorded yet.")
        sys.exit(0)
        
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        print(f"Error opening history database: {e}")
        sys.exit(1)
        
    if args.export:
        export_history(conn, args.export)
    elif args.sessions:
        list_sessions(conn)
    elif args.model:
        query_model_history(conn, args.model)
    else:
        print_weekly_summary(conn, today_only=args.today)
        
    conn.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="drift - ANE Monitor")
    parser.add_argument("command", nargs="?", help="Command (e.g. watch, bench, top, record, replay, compare, ps, finger, monitor, history)")
    parser.add_argument("target", nargs="?", help="Target / model1 / log file")
    parser.add_argument("extra_target", nargs="?", help="Model2 for compare")
    parser.add_argument("--interval", type=int, default=250, help="Refresh interval in ms (100-2000)")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--top", type=int, help="Show only top N processes")
    parser.add_argument("--filter", type=str, help="Filter by name")
    parser.add_argument("--duration", type=int, default=30, help="Benchmark duration in seconds")
    parser.add_argument("--threads", type=int, default=8, help="Benchmark threads")
    parser.add_argument("--output", type=str, help="Output file")
    parser.add_argument("--budget", type=int, help="ANE budget in mJ")
    parser.add_argument("--silent", action="store_true", help="Suppress all notifications")
    parser.add_argument("--notify-on-spike", type=int, help="Notify on process energy spike in mJ")
    parser.add_argument("--notify-on-idle", type=int, help="Notify on process idle in seconds")
    parser.add_argument("--speed", type=str, default="1x", help="Replay speed (e.g. 1x, 2x, 0.5x)")
    parser.add_argument("--sort", type=str, choices=["power", "energy", "footprint"], default="power", help="Sort order for drift ps")
    parser.add_argument("--watch", type=int, help="Re-run every N seconds (drift ps)")
    parser.add_argument("--thermal", action="store_true", help="Enable SMC thermal sparkline overlay / benchmark scorecard")
    parser.add_argument("--sessions", action="store_true", help="List recent sessions (drift history)")
    parser.add_argument("--model", type=str, help="Show stats for a specific model (drift history)")
    parser.add_argument("--today", action="store_true", help="Today's sessions only (drift history)")
    parser.add_argument("--export", type=str, help="Export history as JSON to filepath (drift history)")
    args = parser.parse_args()
    
    if args.version:
        print("drift v1.0.0")
        sys.exit(0)
        
    setup_logging(args.debug)
    check_compatibility()
    
    if args.command == "ps":
        handle_ps(args)
        return
        
    if args.command == "history":
        handle_history(args)
        return
        
    if args.command == "bench":
        from bench import main as bench_main
        bench_main(
            duration=args.duration,
            threads=args.threads,
            json_output=args.json,
            output_file=args.output or args.target,
            thermal=args.thermal,
        )
        return
    
    interval_ms = max(100, min(args.interval, 2000))
    
    speed_val = 1.0
    if args.speed:
        try:
            speed_val = float(args.speed.replace("x", ""))
        except ValueError:
            pass
            
    watch_target = args.target if args.command == "watch" else None
    
    if args.command == "watch":
        if not args.target:
            print("Error: Specify a process to watch (e.g. drift watch llama)")
            sys.exit(1)
        app = DriftApp(interval_ms=interval_ms, mode="monitor", watch_target=args.target, top_n=args.top, filter_name=args.filter,
                       budget=args.budget, silent=args.silent, notify_on_spike=args.notify_on_spike,
                       notify_on_idle=args.notify_on_idle, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "monitor":
        app = DriftApp(interval_ms=interval_ms, mode="monitor", watch_target=args.target, top_n=args.top, filter_name=args.filter,
                       budget=args.budget, silent=args.silent, notify_on_spike=args.notify_on_spike,
                       notify_on_idle=args.notify_on_idle, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "finger":
        app = DriftApp(interval_ms=interval_ms, mode="finger", watch_target=args.target, top_n=args.top, filter_name=args.filter,
                       budget=args.budget, silent=args.silent, notify_on_spike=args.notify_on_spike,
                       notify_on_idle=args.notify_on_idle, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "top":
        interval = args.interval if args.interval != 250 else 500
        app = DriftApp(interval_ms=interval, mode="top", top_n=args.top, filter_name=args.filter,
                       budget=args.budget, silent=args.silent, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "compare":
        if not args.target or not args.extra_target:
            print("Error: Two targets/models are required for comparison (e.g. drift compare llama phi)")
            sys.exit(1)
        app = DriftApp(interval_ms=interval_ms, mode="compare", compare_models=(args.target, args.extra_target),
                       budget=args.budget, silent=args.silent, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "record":
        out_file = args.output or args.target or "session.db"
        app = DriftApp(interval_ms=interval_ms, record_file=out_file, budget=args.budget, silent=args.silent, thermal=args.thermal)
        app.run()
        return
        
    if args.command == "replay":
        log_file = args.target
        if not log_file:
            print("Error: Specify a replay log file (e.g. drift replay session.db)")
            sys.exit(1)
        app = DriftApp(interval_ms=interval_ms, mode="replay", replay_file=log_file, replay_speed=speed_val,
                       budget=args.budget, silent=args.silent, thermal=args.thermal)
        app.run()
        return
        
    if args.json:
        collector = DataCollector(interval_ms)
        chip = get_chip_info()
        try:
            while True:
                collector.collect()
                snap = collector.get_snapshot()
                out = {
                    "ts": snap["ts"],
                    "chip": chip.desc,
                    "ane_cores": int(chip.cores) if chip.cores.isdigit() else 0,
                    "ane_pct": snap["total_ane_pct"],
                    "ane_mw": snap["total_ane_mw"],
                    "peak_mw": snap["peak_mw"],
                    "processes": snap["processes"]
                }
                print(json.dumps(out))
                sys.stdout.flush()
                time.sleep(interval_ms / 1000.0)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        app = DriftApp(interval_ms=interval_ms, mode="finger", watch_target=watch_target, top_n=args.top, filter_name=args.filter,
                       budget=args.budget, silent=args.silent, notify_on_spike=args.notify_on_spike,
                       notify_on_idle=args.notify_on_idle, thermal=args.thermal)
        app.run()

if __name__ == "__main__":
    main()
