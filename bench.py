"""
bench.py — First-ever terminal Apple Neural Engine benchmark

Runs a standardized ANE workload (hammer_ane.swift Vision OCR) for a configurable
duration, monitors energy consumption via proc_pid_rusage, and computes:
  - Sustained power (W)
  - Total energy (mJ)
  - ANE efficiency score (% of thermal budget)
  - Grade (A/B/C/D)

The result is a shareable, reproducible benchmark card.
"""

import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

# ──────────────────────────────────────────────────────────────
# Import the rusage struct from drift
# ──────────────────────────────────────────────────────────────

try:
    from drift import (
        rusage_info_v6, proc_pid_rusage, RUSAGE_INFO_V6,
        get_chip_info, render_sparkline
    )
except ImportError:
    print("Error: bench.py must be run from the drift directory.")
    print("Usage: python drift.py bench")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────
# ANE TOPS table — peak INT8 operations per second by chip
# ──────────────────────────────────────────────────────────────

# Within each generation, base/Pro/Max share the same 16-core ANE.
# Ultra doubles it via UltraFusion (2× Max die).
ANE_TOPS: dict[str, float] = {
    "M1": 11.0,
    "M1 Pro": 11.0,
    "M1 Max": 11.0,
    "M1 Ultra": 22.0,
    "M2": 15.8,
    "M2 Pro": 15.8,
    "M2 Max": 15.8,
    "M2 Ultra": 31.6,
    "M3": 18.0,
    "M3 Pro": 18.0,
    "M3 Max": 18.0,
    "M4": 38.0,
    "M4 Pro": 38.0,
    "M4 Max": 38.0,
    # M5: Apple didn't publish official TOPS; ~25 estimated for ANE alone
    "M5": 25.0,
    "M5 Pro": 25.0,
    "M5 Max": 25.0,
}

# Estimated ANE power budget per chip variant (milliwatts)
# Used for efficiency scoring
ANE_POWER_BUDGET_MW: dict[str, float] = {
    "M1": 8000.0,
    "M1 Pro": 11000.0,
    "M1 Max": 14000.0,
    "M1 Ultra": 28000.0,
    "M2": 8000.0,
    "M2 Pro": 11000.0,
    "M2 Max": 14000.0,
    "M2 Ultra": 28000.0,
    "M3": 8000.0,
    "M3 Pro": 11000.0,
    "M3 Max": 14000.0,
    "M4": 14000.0,
    "M4 Pro": 14000.0,
    "M4 Max": 14000.0,
    "M5": 14000.0,
    "M5 Pro": 14000.0,
    "M5 Max": 14000.0,
}


def _get_chip_key(chip_desc: str) -> str:
    """Extract chip key like 'M5' or 'M4 Pro' from the full description."""
    match = re.search(r"Apple (M\d+(?:\s+(?:Pro|Max|Ultra))?)", chip_desc)
    if match:
        return match.group(1)
    return "M1"  # fallback


# ──────────────────────────────────────────────────────────────
# Mach timebase for tick→nanosecond conversion
# ──────────────────────────────────────────────────────────────

class _mach_timebase_info_data_t(ctypes.Structure):
    _fields_ = [
        ("numer", ctypes.c_uint32),
        ("denom", ctypes.c_uint32),
    ]

_libc = ctypes.CDLL("libc.dylib")
_timebase = _mach_timebase_info_data_t()
_libc.mach_timebase_info(ctypes.byref(_timebase))
TICKS_TO_NS = _timebase.numer / _timebase.denom  # 41.667 on Apple Silicon


# ──────────────────────────────────────────────────────────────
# Benchmark result
# ──────────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    chip: str
    chip_key: str
    ane_cores: int
    duration_s: float
    total_energy_mj: float
    peak_power_mw: float
    avg_power_mw: float
    peak_footprint_mb: float
    avg_ipc: float
    total_inferences: int
    thermal_state: str
    samples: list  # list of dicts per sample
    timestamp: str
    avg_temp: Optional[float] = None
    peak_temp: Optional[float] = None
    temp_key: Optional[str] = None

    @property
    def grade(self) -> str:
        budget = ANE_POWER_BUDGET_MW.get(self.chip_key, 14000.0)
        utilization = self.avg_power_mw / budget
        if utilization >= 0.70:
            return "A"
        elif utilization >= 0.50:
            return "B"
        elif utilization >= 0.30:
            return "C"
        else:
            return "D"

    @property
    def efficiency_pct(self) -> float:
        budget = ANE_POWER_BUDGET_MW.get(self.chip_key, 14000.0)
        return min(100.0, (self.avg_power_mw / budget) * 100.0)

    @property
    def estimated_tops(self) -> Optional[float]:
        peak_tops = ANE_TOPS.get(self.chip_key)
        if peak_tops is None:
            return None
        budget = ANE_POWER_BUDGET_MW.get(self.chip_key, 14000.0)
        utilization = self.avg_power_mw / budget
        return round(utilization * peak_tops, 1)

    def to_json(self) -> dict:
        return {
            "tool": "drift bench",
            "version": "1.0.0",
            "chip": self.chip,
            "chip_key": self.chip_key,
            "ane_cores": self.ane_cores,
            "duration_s": round(self.duration_s, 1),
            "total_energy_mj": round(self.total_energy_mj, 1),
            "peak_power_mw": round(self.peak_power_mw, 1),
            "avg_power_mw": round(self.avg_power_mw, 1),
            "peak_footprint_mb": round(self.peak_footprint_mb, 1),
            "avg_ipc": round(self.avg_ipc, 2),
            "estimated_tops": self.estimated_tops,
            "efficiency_pct": round(self.efficiency_pct, 1),
            "grade": self.grade,
            "thermal_state": self.thermal_state,
            "total_inferences": self.total_inferences,
            "timestamp": self.timestamp,
            "avg_temp": self.avg_temp,
            "peak_temp": self.peak_temp,
            "temp_key": self.temp_key,
        }

    def to_share_line(self) -> str:
        tops_str = f" · {self.estimated_tops} TOPS" if self.estimated_tops else ""
        return f"drift bench · {self.chip} · {self.avg_power_mw/1000:.1f}W avg{tops_str} · {self.grade}"


# ──────────────────────────────────────────────────────────────
# Benchmark runner
# ──────────────────────────────────────────────────────────────

def _compile_hammer(drift_dir: str) -> str:
    """Compile hammer_ane.swift if needed. Returns path to binary."""
    drift_dir_str = str(drift_dir)
    swift_src = os.path.join(drift_dir_str, "hammer_ane.swift")
    binary = os.path.join(drift_dir_str, "hammer_ane")

    if not os.path.exists(swift_src):
        raise FileNotFoundError(
            f"hammer_ane.swift not found in {drift_dir}. "
            "This file is required for benchmarking."
        )

    # Check if binary exists and is newer than source
    if os.path.exists(binary):
        src_mtime = os.path.getmtime(swift_src)
        bin_mtime = os.path.getmtime(binary)
        if bin_mtime >= src_mtime:
            return binary

    print("  Compiling hammer_ane.swift...", end="", flush=True)
    try:
        subprocess.run(
            ["swiftc", swift_src, "-o", binary, "-O"],
            check=True,
            capture_output=True,
            text=True,
        )
        print(" done")
    except subprocess.CalledProcessError as e:
        print(f" failed: {e.stderr}")
        raise RuntimeError(f"Failed to compile hammer_ane.swift: {e.stderr}")

    return binary


def _read_rusage(pid: int) -> Optional[rusage_info_v6]:
    """Read rusage for a PID, returns None on failure."""
    info = rusage_info_v6()
    ret = proc_pid_rusage(pid, RUSAGE_INFO_V6, ctypes.byref(info))
    if ret != 0:
        return None
    return info


def _detect_thermal_state() -> str:
    """Detect current thermal state via pmset."""
    try:
        output = subprocess.check_output(
            ["pmset", "-g", "therm"], text=True, timeout=2
        )
        if "CPU_Speed_Limit" in output:
            for line in output.splitlines():
                if "CPU_Speed_Limit" in line:
                    val = int(line.split("=")[-1].strip())
                    if val >= 100:
                        return "nominal"
                    elif val >= 75:
                        return "fair"
                    elif val >= 50:
                        return "serious"
                    else:
                        return "critical"
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    return "unknown"


def run_benchmark(
    duration: int = 30,
    threads: int = 8,
    sample_interval: float = 0.5,
    drift_dir: Optional[str] = None,
    quiet: bool = False,
    thermal: bool = False,
) -> BenchResult:
    """
    Run the ANE benchmark.

    1. Compiles and launches hammer_ane.swift as a subprocess
    2. Takes rusage samples every sample_interval seconds
    3. Computes energy, power, IPC metrics
    4. Returns a BenchResult with all data
    """
    if drift_dir is None:
        drift_dir = os.path.dirname(os.path.abspath(__file__))

    chip = get_chip_info()
    chip_key = _get_chip_key(chip.desc)
    ane_cores = int(chip.cores) if chip.cores.isdigit() else 16

    if not quiet:
        print(f"\n  drift bench · {chip.desc} · {ane_cores}-core ANE")
        print(f"  Duration: {duration}s · Threads: {threads}")
        print("  " + "─" * 50)

    # Step 1: Compile
    binary = _compile_hammer(drift_dir)

    # Step 2: Launch hammer subprocess
    if not quiet:
        print("  Starting ANE workload...", flush=True)

    proc = subprocess.Popen(
        [binary, str(threads)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the process to start and warm up
    time.sleep(1.0)

    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        raise RuntimeError(
            f"hammer_ane exited immediately: {stderr.decode('utf-8', errors='ignore')}"
        )

    hammer_pid = proc.pid
    if not quiet:
        print(f"  Workload PID: {hammer_pid}")

    # Step 3: Baseline reading
    baseline = _read_rusage(hammer_pid)
    if baseline is None:
        proc.terminate()
        raise RuntimeError("Failed to read initial rusage for hammer_ane")

    # Step 4: Sample loop
    samples = []
    power_values = []
    ipc_values = []
    footprint_values = []
    start_time = time.monotonic()
    prev_info = baseline
    prev_time = start_time
    total_inferences = 0
    
    temp_reader = None
    thermal_samples = []
    if thermal:
        try:
            from drift import SMCTempReader
            temp_reader = SMCTempReader()
            t_val = temp_reader.read_temp()
            if t_val is not None:
                thermal_samples.append(t_val)
        except Exception:
            pass

    if not quiet:
        print(f"  Benchmarking", end="", flush=True)

    try:
        while time.monotonic() - start_time < duration:
            time.sleep(sample_interval)

            if proc.poll() is not None:
                break

            if temp_reader:
                try:
                    t_val = temp_reader.read_temp()
                    if t_val is not None:
                        thermal_samples.append(t_val)
                except Exception:
                    pass

            info = _read_rusage(hammer_pid)
            if info is None:
                continue

            now = time.monotonic()
            wall_dt = now - prev_time

            # Energy delta
            de_nj = info.ri_energy_nj - prev_info.ri_energy_nj
            if de_nj > 0 and wall_dt > 0:
                power_mw = (de_nj / 1e6) / wall_dt
            else:
                power_mw = 0.0

            # IPC
            di = info.ri_instructions - prev_info.ri_instructions
            dc = info.ri_cycles - prev_info.ri_cycles
            ipc = di / dc if dc > 0 else 0.0

            # Footprint
            fp_mb = info.ri_neural_footprint / (1024 * 1024)

            sample = {
                "t": round(now - start_time, 1),
                "power_mw": round(power_mw, 1),
                "energy_mj": round(de_nj / 1e6, 1),
                "ipc": round(ipc, 2),
                "footprint_mb": round(fp_mb, 1),
            }
            samples.append(sample)
            power_values.append(power_mw)
            if ipc > 0:
                ipc_values.append(ipc)
            if fp_mb > 0:
                footprint_values.append(fp_mb)

            prev_info = info
            prev_time = now

            if not quiet:
                print(".", end="", flush=True)

    finally:
        if temp_reader:
            try:
                temp_reader.close()
            except Exception:
                pass
        # Step 5: Kill the workload
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
            # Try to parse total inferences from stdout
            stdout_text = stdout.decode("utf-8", errors="ignore")
            for line in stdout_text.splitlines():
                if "Total inferences" in line:
                    try:
                        total_inferences = int(re.search(r"(\d+)", line).group(1))
                    except (AttributeError, ValueError):
                        pass
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if not quiet:
        print(" done\n")

    # Step 6: Compute results
    actual_duration = time.monotonic() - start_time

    # Total energy from cumulative
    final = _read_rusage(hammer_pid) if proc.poll() is None else prev_info
    total_energy_nj = final.ri_energy_nj - baseline.ri_energy_nj
    total_energy_mj = total_energy_nj / 1e6

    peak_power = max(power_values) if power_values else 0.0
    avg_power = sum(power_values) / len(power_values) if power_values else 0.0
    peak_fp = max(footprint_values) if footprint_values else 0.0
    avg_ipc = sum(ipc_values) / len(ipc_values) if ipc_values else 0.0

    thermal_state_val = _detect_thermal_state()
    
    # Calculate avg and peak temp
    avg_temp_val = sum(thermal_samples) / len(thermal_samples) if thermal_samples else None
    peak_temp_val = max(thermal_samples) if thermal_samples else None
    temp_key_val = temp_reader.active_key if temp_reader else None

    return BenchResult(
        chip=chip.desc,
        chip_key=chip_key,
        ane_cores=ane_cores,
        duration_s=actual_duration,
        total_energy_mj=total_energy_mj,
        peak_power_mw=peak_power,
        avg_power_mw=avg_power,
        peak_footprint_mb=peak_fp,
        avg_ipc=avg_ipc,
        total_inferences=total_inferences,
        thermal_state=thermal_state_val,
        samples=samples,
        timestamp=datetime.now(timezone.utc).isoformat(),
        avg_temp=avg_temp_val,
        peak_temp=peak_temp_val,
        temp_key=temp_key_val,
    )


# ──────────────────────────────────────────────────────────────
# Terminal output
# ──────────────────────────────────────────────────────────────

def _grade_color(grade: str) -> str:
    """ANSI color for grade."""
    colors = {"A": "\033[92m", "B": "\033[93m", "C": "\033[33m", "D": "\033[91m"}
    return colors.get(grade, "\033[0m")


def print_result(result: BenchResult) -> None:
    """Print the benchmark result card to terminal."""
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[96m"
    green = "\033[92m"
    yellow = "\033[93m"
    white = "\033[97m"
    grade_c = _grade_color(result.grade)

    tops_line = ""
    if result.estimated_tops is not None:
        tops_line = f"  {bold}EST. TOPS{reset}        {white}{result.estimated_tops}{reset}"

    # Power sparkline from samples
    pw = [s["power_mw"] for s in result.samples]
    spark = render_sparkline(pw, 48) if pw else "▁" * 48

    # Thermal indicator
    if result.thermal_state == "nominal":
        thermal_str = f"{green}nominal (no throttle detected){reset}"
    elif result.thermal_state == "fair":
        thermal_str = f"{yellow}fair (minor throttling){reset}"
    else:
        thermal_str = f"{yellow}{result.thermal_state}{reset}"

    print(f"  {cyan}╔{'═' * 54}╗{reset}")
    print(f"  {cyan}║{reset}  {bold}drift bench{reset}  ·  {result.chip}  ·  {result.ane_cores}-core ANE{' ' * max(0, 13 - len(result.chip))} {cyan}║{reset}")
    print(f"  {cyan}╠{'═' * 54}╣{reset}")
    print(f"  {cyan}║{reset}{' ' * 54}{cyan}║{reset}")

    # Average power
    print(f"  {cyan}║{reset}  {bold}AVG POWER{reset}        {white}{result.avg_power_mw/1000:.2f} W{reset}{' ' * max(0, 37 - len(f'{result.avg_power_mw/1000:.2f} W'))}{cyan}║{reset}")

    # Peak power
    print(f"  {cyan}║{reset}  {bold}PEAK POWER{reset}       {white}{result.peak_power_mw/1000:.2f} W{reset}{' ' * max(0, 37 - len(f'{result.peak_power_mw/1000:.2f} W'))}{cyan}║{reset}")

    # Total energy
    energy_str = f"{result.total_energy_mj:,.1f} mJ"
    print(f"  {cyan}║{reset}  {bold}TOTAL ENERGY{reset}     {white}{energy_str}{reset}{' ' * max(0, 37 - len(energy_str))}{cyan}║{reset}")

    # Efficiency
    eff_str = f"{result.efficiency_pct:.1f}%"
    print(f"  {cyan}║{reset}  {bold}EFFICIENCY{reset}       {white}{eff_str}{reset}{' ' * max(0, 37 - len(eff_str))}{cyan}║{reset}")

    # Estimated TOPS
    if tops_line:
        tops_val = f"{result.estimated_tops}"
        print(f"  {cyan}║{reset}  {bold}EST. TOPS{reset}        {white}{tops_val}{reset}{' ' * max(0, 37 - len(tops_val))}{cyan}║{reset}")

    # Grade
    print(f"  {cyan}║{reset}  {bold}GRADE{reset}            {grade_c}{bold}{result.grade}{reset}{' ' * 35}{cyan}║{reset}")

    print(f"  {cyan}║{reset}{' ' * 54}{cyan}║{reset}")

    # Sparkline
    print(f"  {cyan}║{reset}  {cyan}{spark}{reset}      {cyan}║{reset}")

    # Time axis
    mid = result.duration_s / 2
    time_axis = f"  0s{' ' * 19}{mid:.0f}s{' ' * 18}{result.duration_s:.0f}s"
    print(f"  {cyan}║{reset}{dim}{time_axis}{reset}{' ' * max(0, 54 - len(time_axis))}{cyan}║{reset}")

    print(f"  {cyan}║{reset}{' ' * 54}{cyan}║{reset}")

    # Thermal
    print(f"  {cyan}║{reset}  {dim}THERMAL{reset}  {thermal_str}{' ' * max(0, 31 - len(result.thermal_state))}{cyan}║{reset}")

    # IPC
    ipc_str = f"{result.avg_ipc:.2f}"
    print(f"  {cyan}║{reset}  {dim}AVG IPC{reset}  {ipc_str}{' ' * max(0, 43 - len(ipc_str))}{cyan}║{reset}")

    # Footprint
    fp_str = f"{result.peak_footprint_mb:.1f} MB"
    print(f"  {cyan}║{reset}  {dim}PEAK FOOTPRINT{reset}  {fp_str}{' ' * max(0, 36 - len(fp_str))}{cyan}║{reset}")

    print(f"  {cyan}║{reset}{' ' * 54}{cyan}║{reset}")
    print(f"  {cyan}╚{'═' * 54}╝{reset}")
    print()
    
    if result.avg_temp is not None:
        print(f"  {bold}THERMAL{reset}")
        print(f"  {dim}───────────────────────────────{reset}")
        print(f"  avg   {result.avg_temp:.1f} °C")
        print(f"  peak  {result.peak_temp:.1f} °C")
        print(f"  key   {result.temp_key or 'unknown'}")
        print()
        
    print(f"  {dim}Share: {result.to_share_line()}{reset}")
    print()


# ──────────────────────────────────────────────────────────────
# CLI entry point (called from drift.py)
# ──────────────────────────────────────────────────────────────

def main(
    duration: int = 30,
    threads: int = 8,
    json_output: bool = False,
    output_file: Optional[str] = None,
    thermal: bool = False,
) -> None:
    """Run benchmark and display/export results."""

    result = run_benchmark(
        duration=duration,
        threads=threads,
        quiet=json_output,
        thermal=thermal,
    )

    if json_output:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print_result(result)

    # Save JSON results
    if output_file is None:
        chip_key = result.chip_key.replace(" ", "_").lower()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"drift_bench_{chip_key}_{ts}.json"

    with open(output_file, "w") as f:
        json.dump(result.to_json(), f, indent=2)

    if not json_output:
        print(f"  Results saved to: {output_file}")


if __name__ == "__main__":
    main()
