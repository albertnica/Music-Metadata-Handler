"""
"modules/filename_utils.py"
Utilities related to file names and temporary operations.
"""

from pathlib import Path
import shutil
import re
from typing import Optional, Tuple
from send2trash import send2trash

# Preserve the original filename-splitting regex and behavior
_FILENAME_SPLIT_RE = re.compile(r"\s[-–—]\s")


def infer_artist_title_from_filename(p: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Infer artist and title from filename, respecting FILENAME_PARSE_MODE:
      - If filename contains "Left - Right" (separator - or long dashes), return according to mode:
          mode 0: (artist=Left, title=Right)
          mode 1: (artist=Right, title=Left)
      - If filename contains a single hyphen WITHOUT spaces, split on first hyphen and apply same mode.
      - If filename has no separator, treat the entire stem as TITLE (artist unknown).
    """
    stem = p.stem.strip()
    if not stem:
        return None, None

    # Prefer explicit " space - space " separators
    m = _FILENAME_SPLIT_RE.split(stem, maxsplit=1)
    if len(m) == 2:
        left = m[0].strip()
        right = m[1].strip()
        # FILENAME_PARSE_MODE is provided by config.py
        from config import FILENAME_PARSE_MODE
        if FILENAME_PARSE_MODE == 0:
            artist = left or None
            title = right or None
        else:
            artist = right or None
            title = left or None
        return artist, title

    return None, None


def unique_temp_copy(src: Path) -> Path:
    base_tmp = src.name + ".tmp"
    temp_path = src.with_name(base_tmp)
    i = 0
    while temp_path.exists():
        i += 1
        temp_path = src.with_name(f"{src.name}.tmp{i}")
    shutil.copy2(str(src), str(temp_path))
    return temp_path


def send_original_to_trash(original: Path) -> None:
    try:
        send2trash(str(original))
    except Exception:
        try:
            original.unlink()
        except Exception:
            pass
