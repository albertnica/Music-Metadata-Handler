"""
modules/core.py
Compatibility wrapper. Provides overwrite_metadata_with_spotify and keeps
file iteration helpers, allowing backwards compatibility with older imports.
"""

from pathlib import Path
import logging

# Delegate to the new processor implementation
try:
    from modules.processor import process_single_file
except Exception:
    # Fallback if import fails (prevents immediate crash on import)
    def process_single_file(file_path: Path, token: str):
        logging.error("modules.processor not available; process_single_file missing.")
        return False

# Re-export under the old name expected by older code
def overwrite_metadata_with_spotify(file_path: Path, token: str) -> bool:
    """
    Backwards-compatible wrapper that calls the new processor.process_single_file.
    """
    return process_single_file(file_path, token)


# File iteration helpers (kept from the original for compatibility)
def iter_audio_files(root: Path, recursive: bool):
    patterns = ("*.flac", "*.mp3", "*.wav")
    if recursive:
        for pat in patterns:
            yield from root.rglob(pat)
    else:
        for pat in patterns:
            yield from root.glob(pat)


def get_creation_time(path: Path) -> float:
    try:
        s = path.stat()
        if hasattr(s, "st_birthtime"):
            return float(s.st_birthtime)
        # Windows uses st_ctime as creation time
        import platform
        if platform.system() == "Windows":
            return float(s.st_ctime)
    # Otherwise use mtime
        return float(s.st_mtime)
    except Exception:
        return 0.0
