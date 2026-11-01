"""
modules/tag_utils.py
Format-specific audio metadata reading and writing utilities for FLAC and MP3 (ID3).

Why this module exists:
Isolates all low-level `mutagen` frame and Vorbis Comment manipulation in a single
adapter layer so `processor.py` remains agnostic to container-format specifics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    ID3NoHeaderError,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPOS,
    TRCK,
    TSRC,
)

# Re-exported from spotify_client to preserve backward compatibility for external callers.
from modules.spotify_client import download_image_bytes, get_artist_genres

# Canonical mapping from logical tag names to ID3v2.4 frame identifiers.
_ID3_FRAME_MAP: dict[str, str] = {
    "artist": "TPE1",
    "albumartist": "TPE2",
    "album": "TALB",
    "title": "TIT2",
    "date": "TDRC",
    "tracknumber": "TRCK",
    "discnumber": "TPOS",
    "isrc": "TSRC",
    "genre": "TCON",
}


def read_audio_object(file_path: Path) -> tuple[FLAC | ID3 | None, Any, str]:
    """
    Loads the Mutagen container object (`FLAC` or `ID3`) for `file_path`.

    Why: Centralizes container instantiation and `ID3NoHeaderError` recovery so
    untagged MP3 files can still be inspected and tagged without throwing errors.
    """
    ext = file_path.suffix.lower()
    audio: FLAC | ID3 | None = None

    try:
        if ext == ".flac":
            audio = FLAC(str(file_path))
        elif ext == ".mp3":
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()
        else:
            return None, None, ext
    except Exception as exc:
        logging.debug("Failed reading audio header for %s: %s", file_path.name, exc)
        return None, None, ext

    if isinstance(audio, FLAC):
        tags = audio.tags if audio.tags is not None else {}
    elif isinstance(audio, ID3):
        tags = audio
    else:
        tags = {}

    return audio, tags, ext


def first_tag_generic(
    audio_obj: FLAC | ID3 | None,
    tags: Any,
    key: str,
) -> str | None:
    """
    Extracts the first non-empty string value for a logical tag `key` across FLAC and ID3.

    Why: Vorbis Comments store values as lists of strings keyed by lowercase names,
    whereas ID3 stores frame objects (`TIT2`, `TPE1`, etc.) whose `.text` attribute
    may contain `ID3TimeStamp` or list values. This helper provides a uniform read API.
    """
    if audio_obj is None or tags is None:
        return None

    try:
        if isinstance(audio_obj, FLAC):
            raw_val = tags.get(key) or tags.get(key.upper())
            if not raw_val:
                return None
            first = raw_val[0] if isinstance(raw_val, (list, tuple)) else raw_val
            text = str(first).strip()
            return text or None

        if isinstance(audio_obj, ID3):
            frame_id = _ID3_FRAME_MAP.get(key.lower())
            if not frame_id or frame_id not in tags:
                return None
            frames = tags.getall(frame_id)
            if not frames:
                return None
            frame_val = getattr(frames[0], "text", None)
            if isinstance(frame_val, (list, tuple)):
                if not frame_val:
                    return None
                text = str(frame_val[0]).strip()
                return text or None
            if frame_val is not None:
                text = str(frame_val).strip()
                return text or None
            text = str(frames[0]).strip()
            return text or None
    except Exception as exc:
        logging.debug("Error reading tag '%s': %s", key, exc)

    return None


def remove_existing_pictures_generic(path: Path, audio_obj: FLAC | ID3) -> None:
    """
    Removes all existing embedded cover art blocks/frames from `audio_obj`.

    Why: Prevents accumulating duplicate or low-resolution artwork frames when
    embedding newly fetched high-resolution album art from Spotify.
    """
    ext = path.suffix.lower()
    if isinstance(audio_obj, ID3):
        audio_obj.delall("APIC")
        return

    if ext == ".flac" and isinstance(audio_obj, FLAC):
        audio_obj.clear_pictures()


def set_genre_on_audio(
    path: Path,
    audio_obj: FLAC | ID3,
    genres_list: Sequence[str],
) -> None:
    """
    Sets or clears the genre tag on `audio_obj` using a standardized `; ` delimiter.

    Why: Ensures multi-genre lists from Spotify are serialized identically across
    Vorbis Comments (`genre`) and ID3v2.4 (`TCON`) frames.
    """
    ext = path.suffix.lower()
    cleaned_genres = [g.strip() for g in genres_list if g and g.strip()]
    genre_value = "; ".join(cleaned_genres) if cleaned_genres else None

    if isinstance(audio_obj, ID3):
        audio_obj.delall("TCON")
        if genre_value:
            audio_obj.add(TCON(encoding=3, text=genre_value))
        return

    if ext == ".flac" and isinstance(audio_obj, FLAC):
        if audio_obj.tags is None:
            audio_obj.add_tags()
        if genre_value:
            audio_obj.tags["genre"] = [genre_value]
        else:
            for key in ("genre", "genres"):
                if key in audio_obj.tags:
                    del audio_obj.tags[key]


def write_tags_to_temp_file(
    source_path: Path,
    temp_path: Path,
    ext: str,
    metadata: Mapping[str, Any],
    image_bytes: bytes | None,
    mime: str | None,
    *,
    overwrite_taa: bool,
    update_only_genre: bool,
) -> None:
    """
    Applies resolved metadata and optional cover art to `temp_path` and persists changes.

    Why: Centralizes FLAC and ID3 mutation rules (including `overwrite_taa` and
    `update_only_genre` guards) and propagates save errors to the caller so a failed
    write never overwrites the original source file.
    """
    genres_list = (
        [g for g in str(metadata.get("genre", "")).split("; ") if g]
        if metadata.get("genre")
        else []
    )

    if ext == ".flac":
        _write_flac_tags(
            source_path=source_path,
            temp_path=temp_path,
            metadata=metadata,
            genres_list=genres_list,
            image_bytes=image_bytes,
            mime=mime,
            overwrite_taa=overwrite_taa,
            update_only_genre=update_only_genre,
        )
    elif ext == ".mp3":
        _write_mp3_tags(
            source_path=source_path,
            temp_path=temp_path,
            metadata=metadata,
            genres_list=genres_list,
            image_bytes=image_bytes,
            mime=mime,
            overwrite_taa=overwrite_taa,
            update_only_genre=update_only_genre,
        )
    else:
        raise ValueError(f"Unsupported audio extension: {ext}")


def _write_flac_tags(
    *,
    source_path: Path,
    temp_path: Path,
    metadata: Mapping[str, Any],
    genres_list: list[str],
    image_bytes: bytes | None,
    mime: str | None,
    overwrite_taa: bool,
    update_only_genre: bool,
) -> None:
    """Writes Vorbis Comments and FLAC picture blocks to `temp_path`."""
    audio_tmp = FLAC(str(temp_path))
    if audio_tmp.tags is None:
        audio_tmp.add_tags()

    if update_only_genre:
        if genres_list:
            set_genre_on_audio(source_path, audio_tmp, genres_list)
        audio_tmp.save(str(temp_path))
        return

    title_val = str(metadata.get("title") or "")
    artist_val = str(metadata.get("artist") or "")
    album_val = str(metadata.get("album") or "")

    if overwrite_taa:
        if title_val:
            audio_tmp.tags["title"] = [title_val]
        if artist_val:
            audio_tmp.tags["artist"] = [artist_val]
        if album_val:
            audio_tmp.tags["album"] = [album_val]
    else:
        if title_val and not first_tag_generic(audio_tmp, audio_tmp.tags, "title"):
            audio_tmp.tags["title"] = [title_val]
        if artist_val and not first_tag_generic(audio_tmp, audio_tmp.tags, "artist"):
            audio_tmp.tags["artist"] = [artist_val]
        if album_val and not first_tag_generic(audio_tmp, audio_tmp.tags, "album"):
            audio_tmp.tags["album"] = [album_val]

    if metadata.get("date"):
        audio_tmp.tags["date"] = [str(metadata["date"])]
    if metadata.get("track"):
        audio_tmp.tags["tracknumber"] = [str(metadata["track"])]
    if metadata.get("disc"):
        audio_tmp.tags["discnumber"] = [str(metadata["disc"])]
    if metadata.get("isrc"):
        audio_tmp.tags["isrc"] = [str(metadata["isrc"])]
    if genres_list:
        set_genre_on_audio(source_path, audio_tmp, genres_list)

    if image_bytes:
        pic = Picture()
        pic.data = image_bytes
        pic.type = 3  # Front cover
        pic.mime = mime or "image/jpeg"
        remove_existing_pictures_generic(source_path, audio_tmp)
        audio_tmp.add_picture(pic)

    audio_tmp.save(str(temp_path))


def _write_mp3_tags(
    *,
    source_path: Path,
    temp_path: Path,
    metadata: Mapping[str, Any],
    genres_list: list[str],
    image_bytes: bytes | None,
    mime: str | None,
    overwrite_taa: bool,
    update_only_genre: bool,
) -> None:
    """Writes ID3v2.4 frames and APIC artwork to `temp_path`."""
    try:
        audio_tmp = ID3(str(temp_path))
    except ID3NoHeaderError:
        audio_tmp = ID3()

    if update_only_genre:
        if genres_list:
            set_genre_on_audio(source_path, audio_tmp, genres_list)
        audio_tmp.save(str(temp_path))
        return

    title_val = str(metadata.get("title") or "")
    artist_val = str(metadata.get("artist") or "")
    album_val = str(metadata.get("album") or "")

    if overwrite_taa:
        if title_val:
            audio_tmp.delall("TIT2")
            audio_tmp.add(TIT2(encoding=3, text=title_val))
        if artist_val:
            audio_tmp.delall("TPE1")
            audio_tmp.add(TPE1(encoding=3, text=artist_val))
        if album_val:
            audio_tmp.delall("TALB")
            audio_tmp.add(TALB(encoding=3, text=album_val))
    else:
        if title_val and not first_tag_generic(audio_tmp, audio_tmp, "title"):
            audio_tmp.delall("TIT2")
            audio_tmp.add(TIT2(encoding=3, text=title_val))
        if artist_val and not first_tag_generic(audio_tmp, audio_tmp, "artist"):
            audio_tmp.delall("TPE1")
            audio_tmp.add(TPE1(encoding=3, text=artist_val))
        if album_val and not first_tag_generic(audio_tmp, audio_tmp, "album"):
            audio_tmp.delall("TALB")
            audio_tmp.add(TALB(encoding=3, text=album_val))

    if metadata.get("date"):
        audio_tmp.delall("TDRC")
        audio_tmp.add(TDRC(encoding=3, text=str(metadata["date"])))
    if metadata.get("track"):
        audio_tmp.delall("TRCK")
        audio_tmp.add(TRCK(encoding=3, text=str(metadata["track"])))
    if metadata.get("disc"):
        audio_tmp.delall("TPOS")
        audio_tmp.add(TPOS(encoding=3, text=str(metadata["disc"])))
    if genres_list:
        set_genre_on_audio(source_path, audio_tmp, genres_list)
    if metadata.get("isrc"):
        audio_tmp.delall("TSRC")
        audio_tmp.add(TSRC(encoding=3, text=str(metadata["isrc"])))

    if image_bytes:
        remove_existing_pictures_generic(source_path, audio_tmp)
        audio_tmp.add(
            APIC(
                encoding=3,
                mime=mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=image_bytes,
            )
        )

    audio_tmp.save(str(temp_path))


__all__ = [
    "download_image_bytes",
    "first_tag_generic",
    "get_artist_genres",
    "read_audio_object",
    "remove_existing_pictures_generic",
    "set_genre_on_audio",
    "write_tags_to_temp_file",
]
