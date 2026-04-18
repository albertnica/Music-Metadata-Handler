"""
modules/core.py
Abstraction layer for file system operations and legacy compatibility. 
We centralize file iteration and metadata extraction here to maintain a 
consistent API if we swap underlying libraries or processing logic.
"""

from pathlib import Path

# File iteration helpers
def iter_audio_files(root: Path, recursive: bool):
    """
    Scans for supported audio formats. WAV is excluded as per the latest 
    streamlining requirements to focus on higher-fidelity/compressed formats.
    Reason: WAV support was deprecated to simplify the codebase and avoid 
    low-level RIFF chunk manipulation issues.
    """
    patterns = ("*.flac", "*.mp3")
    if recursive:
        for pat in patterns:
            yield from root.rglob(pat)
    else:
        for pat in patterns:
            yield from root.glob(pat)


def get_creation_time(path: Path) -> float:
    """
    Retrieves the most accurate file creation timestamp available. 
    Sorted processing relies on this to handle incoming files in the order 
    they were added to the library.
    Reason: Different OSes report creation time via different attributes; 
    we need a cross-platform fallback to st_mtime for consistency.
    """
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
