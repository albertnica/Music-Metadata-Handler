"""
modules/processor.py
Three-stage audio metadata processing pipeline orchestrator.

Why this module exists:
Coordinates the three decoupled execution phases—(1) synchronous local tag extraction,
(2) Selenium Chrome DOM metadata scraping + asynchronous CDN cover art retrieval, and
(3) atomic copy-on-write disk persistence—without coupling directly to WebDriver
internals or Mutagen frame schemas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from pathlib import Path
import shutil
from typing import Any

import aiohttp

import config
from modules.core import LocalTrackInfo, PendingTagUpdate, ResolvedMetadata
from modules.filename_utils import (
    infer_artist_title_from_filename,
    send_original_to_trash,
    unique_temp_copy,
)
from modules.search_utils import strip_parentheses_with_feat
from modules.spotify_client import (
    SpotifyWebScraper,
    download_image_bytes,
)
from modules.tag_utils import (
    first_tag_generic,
    read_audio_object,
    write_tags_to_temp_file,
)


def extract_local_tags(file_path: Path) -> LocalTrackInfo | None:
    """
    Stage 1 (Synchronous / ThreadPool): Reads existing metadata tags and filename
    fallbacks from `file_path` without holding open Mutagen objects across stages.

    Why: Releasing the Mutagen object immediately after reading keeps memory footprint
    flat (`O(1)` per file handle) when scanning thousands of tracks prior to Stage 2.
    """
    audio_obj, tags, ext = read_audio_object(file_path)
    if audio_obj is None:
        logging.info("Unsupported format or unreadable file: %s", file_path.name)
        return None

    isrc_tag = first_tag_generic(audio_obj, tags, "isrc")
    id3_artist = first_tag_generic(audio_obj, tags, "artist") or first_tag_generic(
        audio_obj, tags, "albumartist"
    )
    id3_album = first_tag_generic(audio_obj, tags, "album")
    if config.RuntimeConfig.from_module().only_missing_album and id3_album and id3_album.strip():
        return None
    id3_title = first_tag_generic(audio_obj, tags, "title")

    fn_artist, fn_title = infer_artist_title_from_filename(file_path)

    if not id3_artist and not fn_artist and not id3_title and not fn_title:
        logging.info("Insufficient metadata to search for: %s", file_path.name)
        return None

    return LocalTrackInfo(
        file_path=file_path,
        ext=ext,
        id3_artist=id3_artist,
        id3_title=id3_title,
        id3_album=id3_album,
        fn_artist=fn_artist,
        fn_title=fn_title,
        isrc_tag=isrc_tag,
    )


def map_spotify_match_to_metadata(
    match: Mapping[str, Any],
    artist: str | None,
    album: str | None,
    title: str | None,
    isrc_tag: str | None,
) -> tuple[ResolvedMetadata, str | None, str | None, str | None]:
    """
    Maps a raw Spotify Track or Album dictionary into a normalized `ResolvedMetadata`
    object alongside its high-resolution cover art URL and primary artist ID.

    Why: Isolates Spotify's nested dictionary structure (`album.images`, `artists[0].id`,
    `genres`) from Stage 3 tag persistence.
    """
    meta_artist: str | None = None
    meta_title: str | None = None
    meta_album: str | None = None
    meta_date: str | None = None
    meta_track: str | None = None
    meta_disc: str | None = None
    image_url: str | None = None
    artist_id: str | None = None
    genres_list: list[str] = []

    if "album" in match and "name" in match:
        meta_title = match.get("name")
        album_info = match.get("album") or {}
        meta_album = album_info.get("name")
        artists = match.get("artists") or []
        if artists:
            names = [a.get("name") for a in artists if a.get("name")]
            meta_artist = ", ".join(names) if names else None
            artist_id = artists[0].get("id")
        meta_track = str(match["track_number"]) if match.get("track_number") else None
        meta_disc = str(match["disc_number"]) if match.get("disc_number") else None
        images = album_info.get("images") or []
        if images:
            image_url = images[0].get("url")
        meta_date = album_info.get("release_date")
        raw_genres = album_info.get("genres") or match.get("genres")
        if isinstance(raw_genres, list) and raw_genres:
            genres_list = [str(g) for g in raw_genres if g]
    else:
        meta_album = match.get("name")
        artists = match.get("artists") or []
        if artists:
            names = [a.get("name") for a in artists if a.get("name")]
            meta_artist = ", ".join(names) if names else None
            artist_id = artists[0].get("id")
        images = match.get("images") or []
        if images:
            image_url = images[0].get("url")
        meta_date = match.get("release_date")
        raw_genres = match.get("genres")
        if isinstance(raw_genres, list) and raw_genres:
            genres_list = [str(g) for g in raw_genres if g]

    external_ids = match.get("external_ids")
    spotify_isrc = external_ids.get("isrc") if isinstance(external_ids, Mapping) else None

    resolved = ResolvedMetadata(
        title=meta_title or title or "",
        artist=meta_artist or artist or "",
        album=meta_album or album or "",
        date=meta_date or "",
        track=meta_track or "",
        disc=meta_disc or "",
        genre="; ".join(genres_list) if genres_list else "",
        isrc=isrc_tag or spotify_isrc or "",
    )
    return resolved, image_url, artist_id, meta_artist


def match_track_via_browser(
    scraper: SpotifyWebScraper,
    local_data: LocalTrackInfo | Mapping[str, Any],
) -> tuple[ResolvedMetadata, str | None] | None:
    """
    Stage 2a (Selenium Browser): Searches the Spotify Web Player DOM for `local_data`
    using a cascading fallback (Embedded Tags -> Filename Inference) and resolves genres
    from `data/genre_cache.json`.

    Why: Separating synchronous WebDriver navigation from async CDN cover-art downloads
    allows the Chrome browser to close as soon as DOM scraping finishes.
    """
    runtime_cfg = config.RuntimeConfig.from_module()
    file_path: Path = local_data["file_path"]
    isrc_tag: str | None = local_data.get("isrc_tag")
    id3_artist: str | None = local_data.get("id3_artist")
    id3_title: str | None = local_data.get("id3_title")
    id3_album: str | None = local_data.get("id3_album")
    fn_artist: str | None = local_data.get("fn_artist")
    fn_title: str | None = local_data.get("fn_title")

    match: dict[str, Any] | None = None

    # Tier 1: Embedded ID3 / Vorbis tag search in Web Player DOM.
    if id3_artist and id3_title:
        artist_q = strip_parentheses_with_feat(id3_artist)
        title_q = strip_parentheses_with_feat(id3_title)
        album_q = strip_parentheses_with_feat(id3_album) if id3_album else None
        match = scraper.find_best_match(
            artist_q,
            album_q,
            title_q,
            combined_limit=runtime_cfg.search_candidate_limit,
        )

    # Tier 2: Filename-inferred artist/title fallback search in Web Player DOM.
    if not match and fn_artist and fn_title:
        artist_q = strip_parentheses_with_feat(fn_artist)
        title_q = strip_parentheses_with_feat(fn_title)
        match = scraper.find_best_match(
            artist_q,
            None,
            title_q,
            combined_limit=runtime_cfg.search_candidate_limit,
        )

    if not match:
        logging.info("No Spotify match for: %s", file_path.name)
        return None

    fallback_artist = id3_artist or fn_artist
    fallback_title = id3_title or fn_title
    resolved_meta, image_url, _, _ = map_spotify_match_to_metadata(
        match,
        fallback_artist,
        id3_album,
        fallback_title,
        isrc_tag,
    )
    return resolved_meta, image_url


async def fetch_spotify_data_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    scraper: SpotifyWebScraper,
    local_data: LocalTrackInfo | Mapping[str, Any],
) -> PendingTagUpdate | None:
    """
    Stage 2 (Combined Browser Match + Async Cover Art Fetch): Matches `local_data`
    via `scraper` and downloads high-resolution `640x640` cover art from Spotify's CDN.
    """
    runtime_cfg = config.RuntimeConfig.from_module()
    matched = match_track_via_browser(scraper, local_data)
    if matched is None:
        return None

    resolved_meta, image_url = matched
    image_bytes: bytes | None = None
    mime: str | None = None

    if image_url and not runtime_cfg.update_only_genre:
        artwork = await download_image_bytes(session, image_url, semaphore=semaphore)
        if artwork:
            image_bytes, mime = artwork

    return PendingTagUpdate(
        file_path=local_data["file_path"],
        ext=str(local_data["ext"]),
        metadata_map=resolved_meta,
        image_bytes=image_bytes,
        mime=mime,
    )


def commit_tags_to_file(
    update_data: PendingTagUpdate | Mapping[str, Any],
    output_dir: Path | None = None,
    source_root: Path | None = None,
) -> bool:
    """
    Stage 3 (Synchronous / ThreadPool): Atomically writes updated tags to a temporary copy
    and replaces the target file on disk.

    Why: Copy-on-write staging guarantees that any Mutagen serialization error leaves the
    original audio file untouched. Preserves relative subdirectory structure when exporting
    to a custom `output_dir`.
    """
    runtime_cfg = config.RuntimeConfig.from_module()
    file_path: Path = update_data["file_path"]
    metadata_map = update_data["metadata_map"]
    image_bytes: bytes | None = update_data.get("image_bytes")
    mime: str | None = update_data.get("mime")
    ext: str = str(update_data["ext"]).lower()

    if ext not in config.SUPPORTED_EXTENSIONS:
        logging.warning("Unsupported extension '%s' for %s", ext, file_path.name)
        return False

    temp_path = unique_temp_copy(file_path)

    try:
        write_tags_to_temp_file(
            source_path=file_path,
            temp_path=temp_path,
            ext=ext,
            metadata=metadata_map,
            image_bytes=image_bytes,
            mime=mime,
            overwrite_taa=runtime_cfg.overwrite_title_artist_or_album,
            update_only_genre=runtime_cfg.update_only_genre,
        )

        if output_dir is not None:
            if source_root is not None:
                try:
                    rel_path = file_path.relative_to(source_root)
                except ValueError:
                    rel_path = Path(file_path.name)
            else:
                rel_path = Path(file_path.name)
            dest_path = Path(output_dir) / rel_path
        else:
            dest_path = file_path

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Send the file being replaced to the recycle bin before moving the staged file into place.
        if dest_path.resolve() == file_path.resolve():
            send_original_to_trash(file_path)
        elif dest_path.exists():
            send_original_to_trash(dest_path)

        shutil.move(str(temp_path), str(dest_path))
        logging.info("Successfully updated tags for: %s", dest_path.name)
        return True

    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        logging.error("Failed updating %s on disk: %s", file_path.name, exc)
        return False


__all__ = [
    "commit_tags_to_file",
    "extract_local_tags",
    "fetch_spotify_data_async",
    "first_tag_generic",
    "map_spotify_match_to_metadata",
    "match_track_via_browser",
    "read_audio_object",
]
