# `drift` Test Suite

This directory contains the complete, production-grade test suite for `drift` — a zero-entitlement, htop-style Apple Neural Engine (ANE) monitor.

The test suite is designed to be fully runnable on **non-macOS platforms** (such as Linux CI/CD pipelines) via sophisticated mock layers, while providing robust property-based validation using `hypothesis`.

---

## Running the Tests

To run the test suite, ensure the virtual environment is activated and dependencies are installed:

```bash
# Activate virtual environment
source .venv/bin/activate

# Install requirements (pytest, pytest-cov, pytest-asyncio, hypothesis, psutil)
pip install -r requirements.txt

# Run all tests with verbose output
pytest -v

# Run with coverage report
pytest --cov=. --cov-report=term-missing
```

---

## Test Coverage Configurations

Code coverage is monitored with a strict threshold of **85%**. Exclusions for non-testable components (e.g. system terminal calls, native interactive Textual TUI widgets, OS-level shutdowns, and Apple Silicon hardware-dependent loops) are defined in `.coveragerc`.

Exclusion rules cover:
- Textual Screen subclasses (`FingerprintScreen`, `MainScreen`, `WatchScreen`, etc.)
- The interactive `DriftApp` execution context
- Direct shell utilities (`sys.exit()`, platform architecture guards)
- Ollama local filesystem configuration parsing

---

## Test Architecture and Mock Layers

Mock behaviors are defined globally in [conftest.py](file:///Users/raj/Desktop/drift/tests/conftest.py):

| Component Mock | Strategy & Method | Purpose |
| :--- | :--- | :--- |
| **`libproc` Scanner** | Overrides `_proc_pidinfo` and `_proc_pidfdinfo` using custom `FakeLibproc` hooks. Supports `CArgObject` pointer unpacking (`buffer._obj`). | Simulates file descriptors, paths, and vnodes to inspect Safetensors, GGUF, and CoreML model framework identifiers. |
| **`IOKit` SMC Telemetry** | Implements a simulated AppleSMC client method matching the `IOConnectCallStructMethod` selector. | Exercises temperature reading fallbacks (`TC0P`, `TC0D`, `TCMb`, `TCHP`, `TH0T`, `Tp00`, `Tp01`, `Ts0P`) and SP78 parsing. |
| **WAL Database Writer** | Mocks SQLite WAL writing with a temp directory provider and background worker queue. | Evaluates WAL speed, session pruning, event logs, and database locking states on system wake/sleep. |
| **Task / Bench Clock** | Stubs `time.monotonic` and `time.sleep` in unison. | Simulates passage of time accurately for benchmark durations, TTL cache expirations, and clock drift/negative jumps. |

---

## Directory Layout

- `conftest.py` - Fixtures, global monkeypatch systems, and ctypes array mock helpers.
- `test_bench.py` - TOPS benchmark scorecard parser, compilation fallbacks, and execution durations.
- `test_cli_subcommands.py` - Argument routing, subcommands (`ps`, `history`, `version`), and JSON printing.
- `test_database.py` - WAL transactions, multi-instance logging concurrency, and DB session retention pruning.
- `test_edge_cases.py` - Space and Unicode paths, negative/positive uint64 neural footprint, and full-disk exceptions.
- `test_model_detect.py` - Cache TTL intervals, model framework detection patterns (MLX, CoreML, llama.cpp), and thread safety.
- `test_performance.py` - RSS memory ceilings (<50MB), SQL insertion durations, and timer standard deviations.
- `test_power_math.py` - Odometer overflows, zero delta time divisions, and chip performance maps.
- `test_regression.py` - NTP negative time jump cache safety, DB locking, and SMC loop retries.
- `test_rusage_struct.py` - Struct layouts (464 bytes), member field offset alignment checks.
- `test_smc_temperature.py` - Fixed point sp78/flt decoders, fallback sensor selections, and heat color bands.
- `test_tui_screens.py` - Textual CSS transitions, datatable column configurations, and idle screens.
