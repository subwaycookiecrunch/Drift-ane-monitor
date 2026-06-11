"""
model_detect.py — AI model file detection via macOS proc_pidinfo

Uses proc_pidinfo(PROC_PIDLISTFDS) + proc_pidfdinfo(PROC_PIDFDVNODEPATHINFO)
to enumerate every open file descriptor of a process and detect AI model files.

This is the same syscall path that lsof uses. No sudo, no entitlements required.

Supported frameworks:
  - Ollama / llama.cpp (.gguf)
  - MLX (.safetensors)
  - CoreML (.mlmodelc, .mlpackage, .mlmodel)
  - whisper.cpp (.gguf, .bin with known whisper patterns)
  - PyTorch (.pt, .pth — if open as vnode)
  - ONNX (.onnx)
"""

import ctypes
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────
# Constants from <sys/proc_info.h>
# ──────────────────────────────────────────────────────────────

PROC_PIDLISTFDS = 1
PROC_PIDFDVNODEPATHINFO = 2
PROX_FDTYPE_VNODE = 1
MAXPATHLEN = 1024

# ──────────────────────────────────────────────────────────────
# ctypes struct definitions (mirroring XNU bsd/sys/proc_info.h)
# ──────────────────────────────────────────────────────────────

class proc_fdinfo(ctypes.Structure):
    _fields_ = [
        ("proc_fd", ctypes.c_int32),
        ("proc_fdtype", ctypes.c_uint32),
    ]

class vinfo_stat(ctypes.Structure):
    _fields_ = [
        ("vst_dev", ctypes.c_uint32),
        ("vst_mode", ctypes.c_uint16),
        ("vst_nlink", ctypes.c_uint16),
        ("vst_ino", ctypes.c_uint64),
        ("vst_uid", ctypes.c_uint32),
        ("vst_gid", ctypes.c_uint32),
        ("vst_atime", ctypes.c_uint64),
        ("vst_atimensec", ctypes.c_uint64),
        ("vst_mtime", ctypes.c_uint64),
        ("vst_mtimensec", ctypes.c_uint64),
        ("vst_ctime", ctypes.c_uint64),
        ("vst_ctimensec", ctypes.c_uint64),
        ("vst_birthtime", ctypes.c_uint64),
        ("vst_birthtimensec", ctypes.c_uint64),
        ("vst_size", ctypes.c_uint64),
        ("vst_blocks", ctypes.c_uint64),
        ("vst_blksize", ctypes.c_uint32),
        ("vst_flags", ctypes.c_uint32),
        ("vst_gen", ctypes.c_uint32),
        ("vst_rdev", ctypes.c_uint32),
        ("vst_qspare", ctypes.c_uint64 * 2),
    ]

class fsid_t(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]

class vnode_info(ctypes.Structure):
    _fields_ = [
        ("vi_stat", vinfo_stat),
        ("vi_type", ctypes.c_int),
        ("vi_pad", ctypes.c_int),
        ("vi_fsid", fsid_t),
    ]

class vnode_info_path(ctypes.Structure):
    _fields_ = [
        ("vip_vi", vnode_info),
        ("vip_path", ctypes.c_char * MAXPATHLEN),
    ]

class proc_fileinfo(ctypes.Structure):
    _fields_ = [
        ("fi_openflags", ctypes.c_uint32),
        ("fi_status", ctypes.c_uint32),
        ("fi_offset", ctypes.c_int64),
        ("fi_type", ctypes.c_int32),
        ("fi_guardflags", ctypes.c_uint32),
    ]

class vnode_fdinfowithpath(ctypes.Structure):
    _fields_ = [
        ("pfi", proc_fileinfo),
        ("pvip", vnode_info_path),
    ]

# ──────────────────────────────────────────────────────────────
# Load libproc
# ──────────────────────────────────────────────────────────────

try:
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _proc_pidinfo = _libproc.proc_pidinfo
    _proc_pidfdinfo = _libproc.proc_pidfdinfo
except OSError as e:
    logging.warning(f"Failed to load libproc: {e}")
    _libproc = None
    _proc_pidinfo = None
    _proc_pidfdinfo = None

# ──────────────────────────────────────────────────────────────
# Model file extensions and framework classification
# ──────────────────────────────────────────────────────────────

# Extensions that definitively indicate an AI model
_MODEL_EXTENSIONS = {
    ".gguf": "llama.cpp",
    ".safetensors": "mlx",
    ".mlmodelc": "coreml",
    ".mlpackage": "coreml",
    ".mlmodel": "coreml",
    ".onnx": "onnx",
}

# Extensions that MIGHT be models but need path-based disambiguation
_AMBIGUOUS_EXTENSIONS = {
    ".bin": None,
    ".pt": "pytorch",
    ".pth": "pytorch",
}

# Patterns to EXCLUDE from .bin matches (not AI models)
_BIN_EXCLUDE_PATTERNS = (
    "v8_context_snapshot",
    "icudtl",
    "snapshot_blob",
    "chrome",
    "electron",
    "metallib",
    ".app/",
)

# Patterns that confirm a .bin file is a whisper/AI model
_BIN_INCLUDE_PATTERNS = (
    "ggml",
    "whisper",
    "model",
    "weights",
    "encoder",
    "decoder",
)

# ──────────────────────────────────────────────────────────────
# Ollama manifest resolution
# ──────────────────────────────────────────────────────────────

_OLLAMA_MODELS_DIR = os.path.expanduser("~/.ollama/models")
_ollama_hash_cache: Dict[str, str] = {}
_ollama_cache_time: float = 0.0
_OLLAMA_CACHE_TTL = 10.0  # refresh every 10 seconds


def _refresh_ollama_cache() -> None:
    """Build a mapping from blob SHA-256 hash → model name by reading Ollama manifests."""
    global _ollama_hash_cache, _ollama_cache_time

    manifests_dir = os.path.join(_OLLAMA_MODELS_DIR, "manifests", "registry.ollama.ai")
    if not os.path.isdir(manifests_dir):
        return

    new_cache: Dict[str, str] = {}
    try:
        # Walk: manifests/registry.ollama.ai/<namespace>/<model>/<tag>
        for namespace in os.listdir(manifests_dir):
            ns_path = os.path.join(manifests_dir, namespace)
            if not os.path.isdir(ns_path):
                continue
            for model_name in os.listdir(ns_path):
                model_path = os.path.join(ns_path, model_name)
                if not os.path.isdir(model_path):
                    continue
                for tag in os.listdir(model_path):
                    tag_path = os.path.join(model_path, tag)
                    if not os.path.isfile(tag_path):
                        continue
                    try:
                        with open(tag_path, "r") as f:
                            manifest = json.load(f)
                        for layer in manifest.get("layers", []):
                            digest = layer.get("digest", "")
                            media_type = layer.get("mediaType", "")
                            if "model" in media_type and digest.startswith("sha256:"):
                                blob_hash = digest.replace("sha256:", "sha256-")
                                display = f"{model_name}:{tag}"
                                if namespace != "library":
                                    display = f"{namespace}/{display}"
                                new_cache[blob_hash] = display
                    except (json.JSONDecodeError, KeyError, IOError):
                        continue
    except OSError:
        pass

    _ollama_hash_cache = new_cache
    _ollama_cache_time = time.monotonic()


def _resolve_ollama_blob(path: str) -> Optional[str]:
    """Given a path like ~/.ollama/models/blobs/sha256-abc123, return the model name."""
    global _ollama_cache_time

    if time.monotonic() - _ollama_cache_time > _OLLAMA_CACHE_TTL:
        _refresh_ollama_cache()

    basename = os.path.basename(path)
    return _ollama_hash_cache.get(basename)


# ──────────────────────────────────────────────────────────────
# ModelFile dataclass
# ──────────────────────────────────────────────────────────────

@dataclass
class ModelFile:
    """Represents a detected AI model file open by a process."""
    path: str                         # Full filesystem path
    name: str                         # Human-readable model name
    size_bytes: int = 0               # File size from vnode stat
    framework: str = "unknown"        # llama.cpp, mlx, coreml, pytorch, onnx, whisper
    quantization: str = ""            # Extracted quant info (e.g., "Q4_K_M")

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def display(self) -> str:
        """Short display string for the TUI."""
        size_str = f"{self.size_gb:.1f}GB" if self.size_bytes > 0 else ""
        parts = [self.name]
        if self.quantization:
            parts.append(self.quantization)
        if size_str:
            parts.append(size_str)
        return " · ".join(parts)


# ──────────────────────────────────────────────────────────────
# Model name extraction from file paths
# ──────────────────────────────────────────────────────────────

# Common GGUF naming patterns:
#   llama-3-8b-instruct-q4_K_M.gguf
#   mistral-7b-v0.3.Q5_K_M.gguf
#   phi-3-mini-4k-instruct.Q4_0.gguf
_GGUF_NAME_RE = re.compile(
    r"^(.+?)(?:[.-]((?:Q|q|IQ|iq|F|f)\d[^\s.]*))?\.(gguf)$",
    re.IGNORECASE
)

# Safetensors: model-00001-of-00004.safetensors, model.safetensors
_SAFETENSOR_SHARD_RE = re.compile(
    r"^(.+?)-\d{5}-of-\d{5}\.safetensors$",
    re.IGNORECASE
)


def _extract_model_name(path: str, framework: str) -> Tuple[str, str]:
    """
    Extract a human-readable model name and quantization from a model file path.
    Returns (name, quantization).
    """
    basename = os.path.basename(path)
    parent_dir = os.path.basename(os.path.dirname(path))

    # Ollama blob resolution
    if "/.ollama/models/blobs/" in path:
        resolved = _resolve_ollama_blob(path)
        if resolved:
            return resolved, ""
        return f"ollama-blob", ""

    # GGUF files
    if framework == "llama.cpp":
        m = _GGUF_NAME_RE.match(basename)
        if m:
            name = m.group(1).replace("-", " ").replace("_", " ")
            quant = m.group(2) or ""
            return name.strip(), quant.upper()
        # Fallback: strip extension
        return basename.rsplit(".", 1)[0], ""

    # Safetensors (MLX, HuggingFace)
    if framework == "mlx":
        m = _SAFETENSOR_SHARD_RE.match(basename)
        if m:
            # For sharded models, the parent directory usually has the model name
            return parent_dir, ""
        if basename == "model.safetensors" or basename == "weights.safetensors":
            # Generic name — use parent directory
            return parent_dir, ""
        return basename.rsplit(".", 1)[0], ""

    # CoreML
    if framework == "coreml":
        # .mlmodelc is a directory — its name IS the model name
        name = basename.replace(".mlmodelc", "").replace(".mlpackage", "").replace(".mlmodel", "")
        return name, ""

    # Fallback
    name = basename.rsplit(".", 1)[0] if "." in basename else basename
    return name, ""


# ──────────────────────────────────────────────────────────────
# FD enumeration and model detection
# ──────────────────────────────────────────────────────────────

def _get_open_vnode_paths(pid: int) -> List[Tuple[str, int]]:
    """
    Get all open vnode file paths for a process.
    Returns list of (path, file_size) tuples.
    """
    if _proc_pidinfo is None:
        return []

    results: List[Tuple[str, int]] = []

    try:
        # Step 1: Get buffer size for FD list
        bufsize = _proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
        if bufsize <= 0:
            return []

        # Step 2: Allocate and fill FD list
        num_fds = bufsize // ctypes.sizeof(proc_fdinfo)
        if num_fds <= 0 or num_fds > 10000:  # sanity limit
            return []

        fd_array = (proc_fdinfo * num_fds)()
        ret = _proc_pidinfo(pid, PROC_PIDLISTFDS, 0, ctypes.byref(fd_array), bufsize)
        if ret <= 0:
            return []

        actual_fds = ret // ctypes.sizeof(proc_fdinfo)

        # Step 3: For each vnode FD, get the path
        vinfo = vnode_fdinfowithpath()
        vinfo_size = ctypes.sizeof(vinfo)

        for i in range(actual_fds):
            if fd_array[i].proc_fdtype != PROX_FDTYPE_VNODE:
                continue

            ret2 = _proc_pidfdinfo(
                pid, fd_array[i].proc_fd,
                PROC_PIDFDVNODEPATHINFO,
                ctypes.byref(vinfo), vinfo_size
            )
            if ret2 <= 0:
                continue

            try:
                path = vinfo.pvip.vip_path.decode("utf-8", errors="ignore").rstrip("\x00")
                if path and path.startswith("/"):
                    file_size = vinfo.pvip.vip_vi.vi_stat.vst_size
                    results.append((path, file_size))
            except (UnicodeDecodeError, ValueError):
                continue

    except (ctypes.ArgumentError, OSError) as e:
        logging.debug(f"FD enumeration failed for PID {pid}: {e}")

    return results


def _classify_path(path: str) -> Optional[Tuple[str, str]]:
    """
    Classify a file path as a model file.
    Returns (framework, extension) or None if not a model.
    """
    path_lower = path.lower()

    # Check for CoreML directory subfiles
    if ".mlmodelc/" in path_lower or path_lower.endswith(".mlmodelc"):
        return "coreml", ".mlmodelc"
    if ".mlpackage/" in path_lower or path_lower.endswith(".mlpackage"):
        return "coreml", ".mlpackage"

    # Check definitive model extensions
    for ext, framework in _MODEL_EXTENSIONS.items():
        if path_lower.endswith(ext):
            return framework, ext

    # Check ambiguous extensions with filtering
    for ext, framework in _AMBIGUOUS_EXTENSIONS.items():
        if not path_lower.endswith(ext):
            continue

        # Exclude known non-model .bin files
        if ext == ".bin":
            if any(pattern in path_lower for pattern in _BIN_EXCLUDE_PATTERNS):
                return None
            # Only include .bin files that look like AI models
            if any(pattern in path_lower for pattern in _BIN_INCLUDE_PATTERNS):
                return "whisper", ext
            # Skip unrecognized .bin files
            return None

        # .pt / .pth files — always classify as pytorch
        return framework, ext

    # Check for Ollama blobs (no extension but in the blobs directory)
    if "/.ollama/models/blobs/" in path:
        return "llama.cpp", ""

    return None


def get_open_model_files(pid: int) -> List[ModelFile]:
    """
    Detect all AI model files currently open by the given process.

    Uses proc_pidinfo to enumerate file descriptors — same as lsof.
    No sudo required for same-user processes.
    """
    vnode_paths = _get_open_vnode_paths(pid)
    models: List[ModelFile] = []
    seen_paths: set = set()

    for path, file_size in vnode_paths:
        if path in seen_paths:
            continue

        classification = _classify_path(path)
        if classification is None:
            continue

        framework, ext = classification
        seen_paths.add(path)

        name, quant = _extract_model_name(path, framework)

        models.append(ModelFile(
            path=path,
            name=name,
            size_bytes=file_size,
            framework=framework,
            quantization=quant,
        ))

    return models


# ──────────────────────────────────────────────────────────────
# Cached scanner (for integration with DataCollector)
# ──────────────────────────────────────────────────────────────

class ModelScanner:
    """
    Caches model detection results per PID.
    Only re-scans FDs every `scan_interval` seconds to avoid
    excessive syscall overhead at 250ms refresh rates.
    """

    def __init__(self, scan_interval: float = 2.0):
        self.scan_interval = scan_interval
        self._cache: Dict[int, Tuple[float, List[ModelFile]]] = {}

    def scan(self, pid: int) -> List[ModelFile]:
        """Get model files for a PID, using cache if fresh enough."""
        now = time.monotonic()

        cached = self._cache.get(pid)
        if cached is not None:
            last_scan, models = cached
            if 0 <= now - last_scan < self.scan_interval:
                return models

        models = get_open_model_files(pid)
        self._cache[pid] = (now, models)
        return models

    def get_primary_model(self, pid: int) -> Optional[ModelFile]:
        """Get the most significant model file for a PID (largest by size)."""
        models = self.scan(pid)
        if not models:
            return None
        return max(models, key=lambda m: m.size_bytes)

    def clear_pid(self, pid: int) -> None:
        """Remove a PID from the cache."""
        self._cache.pop(pid, None)

    def clear_all(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()

    def prune_dead(self, alive_pids: set) -> None:
        """Remove cached entries for PIDs that no longer exist."""
        dead = [pid for pid in self._cache if pid not in alive_pids]
        for pid in dead:
            del self._cache[pid]


# ──────────────────────────────────────────────────────────────
# CLI test
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import psutil

    print("Scanning all processes for AI model files...")
    print("=" * 72)

    found_any = False
    scanner = ModelScanner(scan_interval=0)

    for p in psutil.process_iter(["pid", "name"]):
        try:
            pid = p.info["pid"]
            name = p.info["name"]
            models = scanner.scan(pid)
            if models:
                found_any = True
                print(f"\n🧠 {name} (PID {pid}):")
                for m in models:
                    size_str = f" [{m.size_gb:.1f} GB]" if m.size_bytes > 0 else ""
                    quant_str = f" ({m.quantization})" if m.quantization else ""
                    print(f"   → {m.name}{quant_str}{size_str}  [{m.framework}]")
                    print(f"     {m.path}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not found_any:
        print("\nNo AI model files detected.")
        print("Try running: ollama run llama3  (or any local model)")
