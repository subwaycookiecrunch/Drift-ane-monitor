import model_detect
import drift
import bench
from typing import List, Optional, Tuple
import psutil
import sqlite3
import time

_scanner = model_detect.ModelScanner(scan_interval=2.0)

def scan_process(pid: int) -> Optional[List[model_detect.ModelFile]]:
    """Scan process open files for models."""
    return _scanner.scan(pid)

def scan_all_processes() -> List[model_detect.ModelFile]:
    """Scan all processes for models, excluding kernel pid 0."""
    res = []
    for p in psutil.process_iter(['pid']):
        pid = p.info['pid']
        if pid == 0:
            continue
        try:
            m = _scanner.scan(pid)
            if m:
                res.extend(m)
        except Exception:
            pass
    return res

def prune_old_logs(db_path: str) -> None:
    """Prune sessions older than 30 days and cascading events/samples."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cutoff = time.time() - 2592000
        session_ids = [r[0] for r in cursor.execute("SELECT id FROM sessions WHERE started_at < ?;", (cutoff,)).fetchall()]
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            cursor.execute(f"DELETE FROM events WHERE session_id IN ({placeholders});", session_ids)
            cursor.execute(f"DELETE FROM samples WHERE session_id IN ({placeholders});", session_ids)
            cursor.execute(f"DELETE FROM sessions WHERE id IN ({placeholders});", session_ids)
        conn.commit()
    finally:
        conn.close()

def calculate_ipc(instructions: int, cycles: int) -> float:
    """Calculate IPC safely."""
    if cycles <= 0:
        return 0.0
    return instructions / cycles

def get_tops_for_chip(chip: str) -> float:
    """Get peak ANE TOPS for a chip (case-insensitive)."""
    for k, v in bench.ANE_TOPS.items():
        if k.lower() == chip.lower():
            return v
    raise ValueError(f"Unknown chip {chip}")
