"""
modules/scanner.py
Multithreaded audio library scanner and issue classification engine.

Why this module exists:
Decouples filesystem traversal, Mutagen header parsing, and metadata health evaluation
from the UI presentation layer, allowing the web dashboard and CLI to audit audio files
for missing tags and cover art without retaining open file descriptors in memory.
"""

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC
from mutagen.id3 import ID3

import config
from modules.core import iter_audio_files
from modules.filename_utils import infer_artist_title_from_filename
from modules.tag_utils import first_tag_generic, read_audio_object

# Standard recognized metadata issue keys
KNOWN_CRITERIA: tuple[str, ...] = (
    "all",
    "lack_cover",
    "lack_title",
    "lack_artist",
    "lack_album",
    "lack_date",
    "lack_genre",
)


def has_embedded_artwork(audio_obj: FLAC | ID3 | None, ext: str) -> bool:
    """
    Determines whether the audio container contains at least one embedded front cover image.

    Why: FLAC stores pictures in dedicated metadata picture blocks, whereas ID3
    embeds them as APIC frames. Inspecting the parsed container directly avoids
    decoding image binary payloads into RAM during batch library audits.
    """
    if audio_obj is None:
        return False

    if ext == ".flac" and isinstance(audio_obj, FLAC):
        return bool(audio_obj.pictures)

    if ext == ".mp3" and isinstance(audio_obj, ID3):
        return bool(audio_obj.getall("APIC"))

    return False


def inspect_audio_file(
    file_path: Path,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """
    Reads embedded tags and evaluates metadata completeness for a single audio file.

    Why: Immediate destruction of the Mutagen object upon return guarantees flat
    memory usage (O(1) per worker thread) across massive directory scans.
    """
    audio_obj, tags, ext = read_audio_object(file_path)
    if audio_obj is None:
        return None

    title = first_tag_generic(audio_obj, tags, "title") or ""
    artist = (
        first_tag_generic(audio_obj, tags, "artist")
        or first_tag_generic(audio_obj, tags, "albumartist")
        or ""
    )
    album = first_tag_generic(audio_obj, tags, "album") or ""
    date = first_tag_generic(audio_obj, tags, "date") or ""
    genre = first_tag_generic(audio_obj, tags, "genre") or ""
    has_cover = has_embedded_artwork(audio_obj, ext)

    fn_artist, fn_title = infer_artist_title_from_filename(file_path)

    issues: list[str] = []
    if not has_cover:
        issues.append("lack_cover")
    if not title.strip():
        issues.append("lack_title")
    if not artist.strip():
        issues.append("lack_artist")
    if not album.strip():
        issues.append("lack_album")
    if not date.strip():
        issues.append("lack_date")
    if not genre.strip():
        issues.append("lack_genre")

    if base_dir is not None:
        try:
            rel_path = str(file_path.relative_to(base_dir))
        except ValueError:
            rel_path = file_path.name
    else:
        rel_path = file_path.name

    return {
        "file_path": str(file_path.resolve()),
        "filename": file_path.name,
        "rel_path": rel_path,
        "ext": ext,
        "title": title,
        "artist": artist,
        "album": album,
        "date": date,
        "genre": genre,
        "has_cover": has_cover,
        "fn_artist": fn_artist or "",
        "fn_title": fn_title or "",
        "issues": issues,
    }


def matches_criteria(issues: list[str], criteria: list[str] | None) -> bool:
    """
    Evaluates whether a track's identified metadata deficiencies satisfy active filter criteria.

    Why: Allows users to isolate specific maintenance tasks (e.g., only tracks needing artwork)
    without forcing all-or-nothing pipeline executions.
    """
    if not criteria or "all" in criteria:
        return True

    return any(crit in issues for crit in criteria)


def scan_audio_library(
    folder_path: Path,
    recursive: bool = False,
    criteria: list[str] | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    """
    Performs a concurrent library scan and returns categorized track inventory with health stats.

    Why: Leveraging ThreadPoolExecutor speeds up disk metadata reads across multi-thousand track
    libraries by an order of magnitude compared to sequential filesystem loops.
    """
    resolved_folder = folder_path.resolve()
    if not resolved_folder.exists() or not resolved_folder.is_dir():
        raise FileNotFoundError(f"Directory not found: {folder_path}")

    all_paths = list(iter_audio_files(resolved_folder, recursive=recursive))
    total_scanned = len(all_paths)

    results: list[dict[str, Any]] = []
    flac_count = 0
    mp3_count = 0

    def _worker(path: Path) -> dict[str, Any] | None:
        try:
            return inspect_audio_file(path, base_dir=resolved_folder)
        except Exception as exc:
            logging.debug("Error inspecting %s: %s", path.name, exc)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for item in executor.map(_worker, all_paths):
            if item is None:
                continue

            if item["ext"] == ".flac":
                flac_count += 1
            elif item["ext"] == ".mp3":
                mp3_count += 1

            if matches_criteria(item["issues"], criteria):
                results.append(item)

    # Sort tracks deterministically by relative path
    results.sort(key=lambda x: x["rel_path"].lower())

    return {
        "folder_path": str(resolved_folder),
        "total_scanned": total_scanned,
        "total_matched": len(results),
        "flac_count": flac_count,
        "mp3_count": mp3_count,
        "tracks": results,
    }


__all__ = [
    "KNOWN_CRITERIA",
    "has_embedded_artwork",
    "inspect_audio_file",
    "matches_criteria",
    "scan_audio_library",
]

