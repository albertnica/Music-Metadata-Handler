"""
modules/filename_utils.py
Filename metadata inference and atomic temporary file staging utilities.

Why this module exists:
Isolates filesystem path parsing and safe copy-on-write file staging from
metadata tag manipulation so interrupted writes never corrupt user audio files.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shutil
import tempfile

from send2trash import send2trash

import config

# Requires whitespace flanking the dash (hyphen, en-dash, or em-dash) so hyphenated
# artist names (e.g., "Jay-Z", "blink-182") are not falsely split mid-token.
_FILENAME_SPLIT_RE: re.Pattern[str] = re.compile(r"\s[-–—]\s")


def infer_artist_title_from_filename(
    path: Path,
    parse_mode: int | None = None,
) -> tuple[str | None, str | None]:
    """
    Infers `(artist, title)` from an audio filename stem using `FILENAME_PARSE_MODE`.

    Why: Provides a structured fallback when embedded ID3/Vorbis tags are missing
    or incomplete, while requiring whitespace-bounded dash delimiters to avoid
    corrupting hyphenated artist or track names.

    Args:
        path: Path to the audio file.
        parse_mode: Optional override (`0` for `"Artist - Title"`, `1` for `"Title - Artist"`).
            Defaults to `config.FILENAME_PARSE_MODE` evaluated at call time.

    Returns:
        A `(artist, title)` tuple, or `(None, None)` if the filename does not match
        the delimiter pattern.
    """
    stem = path.stem.strip()
    if not stem:
        return None, None

    parts = _FILENAME_SPLIT_RE.split(stem, maxsplit=1)
    if len(parts) != 2:
        return None, None

    left = parts[0].strip() or None
    right = parts[1].strip() or None
    mode = config.FILENAME_PARSE_MODE if parse_mode is None else parse_mode

    if mode == 0:
        return left, right
    return right, left


def unique_temp_copy(src: Path) -> Path:
    """
    Creates an atomic, thread-safe temporary copy of `src` in the same directory.

    Why: Mutating audio tags directly in-place risks corrupting the original file if
    an exception or power loss occurs mid-write. Creating the temporary copy in
    `src.parent` via `tempfile.mkstemp` avoids TOCTOU filename race conditions across
    `ThreadPoolExecutor` workers and guarantees the temp file resides on the same
    filesystem volume for atomic replacement.
    """
    fd, raw_tmp_path = tempfile.mkstemp(
        prefix=f".{src.name}.",
        suffix=".tmp",
        dir=str(src.parent),
    )
    os.close(fd)
    temp_path = Path(raw_tmp_path)
    try:
        shutil.copy2(src, temp_path)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def send_original_to_trash(original: Path) -> None:
    """
    Moves `original` to the OS Recycle Bin / Trash before replacing it with the updated file.

    Why: Gives the user a non-destructive recovery mechanism if a heuristic Spotify
    match overwrites a file with undesired metadata. Falls back to `unlink()` only
    when the underlying filesystem (e.g., network share or headless mount) does not
    support a recycle bin.
    """
    if not original.exists():
        return
    try:
        send2trash(str(original))
    except Exception as trash_err:
        logging.debug(
            "Recycle bin unavailable for %s (%s); falling back to direct unlink.",
            original.name,
            trash_err,
        )
        try:
            original.unlink(missing_ok=True)
        except OSError as unlink_err:
            logging.error("Failed to remove original file %s: %s", original.name, unlink_err)
            raise
