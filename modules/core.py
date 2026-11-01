"""
modules/core.py
Core domain data structures and filesystem discovery primitives.

Why this module exists:
1. Defines memory-efficient, strongly-typed pipeline payloads (`LocalTrackInfo`,
   `ResolvedMetadata`, `PendingTagUpdate`) so stages communicate through explicit
   contracts rather than loosely-typed dictionaries.
2. Centralizes single-pass filesystem traversal and OS-aware creation timestamp
   resolution to keep I/O predictable across Windows and Linux/WSL2.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
import platform
from typing import Any

import config

# Cached at import time to avoid repeated syscall/platform checks inside sort key loops.
_IS_WINDOWS: bool = platform.system() == "Windows"


class _DictCompatibleMixin:
    """
    Provides read-only mapping subscript compatibility (`obj["key"]`, `obj.get("key")`)
    on top of slotted dataclasses.

    Why: Allows internal modules to benefit from static typing and `__slots__` memory
    savings without breaking external callers that access fields via dict syntax.
    """

    __slots__ = ()

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the dataclass instance into a standard dictionary."""
        return asdict(self)  # type: ignore[arg-type]


@dataclass(slots=True, frozen=True)
class LocalTrackInfo(_DictCompatibleMixin):
    """
    Stage 1 output payload representing metadata extracted from a local audio file
    prior to querying Spotify.

    Why: Separates raw tag values (`id3_*`) from filename-inferred fallbacks (`fn_*`)
    so Stage 2 can execute a deterministic cascading search (ISRC -> Tags -> Filename).
    """

    file_path: Path
    ext: str
    id3_artist: str | None
    id3_title: str | None
    id3_album: str | None
    fn_artist: str | None
    fn_title: str | None
    isrc_tag: str | None

    @property
    def artist(self) -> str | None:
        """Primary artist fallback chain preferring embedded tags over filename inference."""
        return self.id3_artist or self.fn_artist

    @property
    def title(self) -> str | None:
        """Primary title fallback chain preferring embedded tags over filename inference."""
        return self.id3_title or self.fn_title

    @property
    def album(self) -> str | None:
        """Embedded album name if present."""
        return self.id3_album


@dataclass(slots=True, frozen=True)
class ResolvedMetadata(_DictCompatibleMixin):
    """
    Normalized metadata fields resolved from a Spotify match merged with local fallbacks.

    Why: Standardizes all tag fields into non-nullable strings before Stage 3 writes
    format-specific Vorbis Comments (FLAC) or ID3v2.4 frames (MP3).
    """

    title: str = ""
    artist: str = ""
    album: str = ""
    date: str = ""
    track: str = ""
    disc: str = ""
    genre: str = ""
    isrc: str = ""


@dataclass(slots=True, frozen=True)
class PendingTagUpdate(_DictCompatibleMixin):
    """
    Stage 2 output payload containing resolved metadata and downloaded cover art
    ready for atomic disk persistence in Stage 3.

    Why: Decouples network I/O from disk I/O so Mutagen never holds open file handles
    while waiting on HTTP responses.
    """

    file_path: Path
    ext: str
    metadata_map: ResolvedMetadata
    image_bytes: bytes | None = None
    mime: str | None = None


def iter_audio_files(root: Path, recursive: bool) -> Iterator[Path]:
    """
    Yields supported audio files (`.flac`, `.mp3`) from `root` in a single traversal pass.

    Why: Using a single directory walk (`rglob("*")` / `iterdir()`) with case-insensitive
    suffix filtering halves filesystem I/O compared to running separate globs per extension,
    and ensures `.FLAC`/`.MP3` files are discovered on case-sensitive Linux/WSL2 mounts.
    """
    supported = {ext.lower() for ext in config.SUPPORTED_EXTENSIONS}
    entries = root.rglob("*") if recursive else root.iterdir()
    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() in supported:
                yield entry
        except OSError:
            # Skip broken symlinks or permission-restricted entries without aborting the scan.
            continue


def get_creation_time(path: Path) -> float:
    """
    Retrieves the most accurate creation timestamp available for `path`.

    Why: Sorting by creation time allows users to process only the `N` most recently
    added tracks (`PROCESS_TOP_X`). Because POSIX, macOS, and Windows expose creation
    time differently (`st_birthtime` vs. `st_ctime` vs. `st_mtime`), this helper
    normalizes the lookup without re-querying OS platform state on every call.
    """
    try:
        stat_result = path.stat()
        birthtime = getattr(stat_result, "st_birthtime", None)
        if birthtime is not None:
            return float(birthtime)
        if _IS_WINDOWS:
            return float(stat_result.st_ctime)
        return float(stat_result.st_mtime)
    except OSError:
        return 0.0
