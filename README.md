# drift

drift is a zero-entitlement, htop-style Apple Neural Engine monitor for Apple Silicon Macs built with Python and Textual.

## Install

```bash
brew install subwaycookiecrunch/tap/drift
```

## Quick Start

```bash
drift ps
drift finger
drift bench
drift history
drift watch --thermal
drift top
```

## Features

| | |
| --- | --- |
| Per-process ANE energy and footprint tracking | System-wide ANE utilization live sparkline |
| Active model detection via open file descriptors | Relational session logging and sqlite database |
| Multi-threaded hardware TOPS benchmark scorecard | Side-by-side model comparison views |
| Focused process watch and threshold warnings | Structured history weekly performance cards |

## How It Works

XNU kernel process resource usage statistics are retrieved using `rusage_info_v6` structure via `proc_pidinfo` with flavor `PROC_PIDRUSAGE`. This enables retrieval of `ri_neural_footprint` and `ri_energy_nj` per process without requiring root privileges. Power values are derived using consecutive energy differentials divided by the wall time delta.

Model detection uses process file descriptor scanning to match paths of memory-mapped model files against known extensions. The ctypes bindings for `proc_pidinfo` with flavor `PROC_PIDLISTFDS` enumerate process open file descriptors. FDs identifying active vnodes are subsequently resolved to paths via flavor `PROC_PIDFDVNODEPATHINFO`.

Temperature readings are retrieved via ctypes calls to `IOKit.framework` using the `AppleSMC` service client. The sensor keys are dynamically matched across Apple Silicon hardware generations using a fallback chain starting at `TC0P`. All metrics are persisted to local SQLite databases using background worker threads to keep interface updates free of blocking operations.
