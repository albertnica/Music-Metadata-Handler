"""
main.py
CLI entrypoint and batch pipeline runner for the Music Metadata Handler.

Why this module exists:
Handles command-line argument parsing, directory scanning, persistent Chrome WebDriver
session lifecycle (`SpotifyWebScraper`), asynchronous CDN cover-art retrieval, and
atomic copy-on-write tag persistence.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import concurrent.futures
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

import config
from modules.core import (
    LocalTrackInfo,
    PendingTagUpdate,
    ResolvedMetadata,
    get_creation_time,
    iter_audio_files,
)
from modules.processor import (
    commit_tags_to_file,
    extract_local_tags,
    match_track_via_browser,
)
from modules.spotify_client import SpotifyWebScraper, download_image_bytes

# Baseline fallback defaults if any attribute was removed from config.py manually.
_DEFAULTS: dict[str, Any] = {
    "RECURSIVE": False,
    "PROCESS_TOP_X": 1,
    "OVERWRITE_TITLE_ARTIST_OR_ALBUM": 1,
    "UPDATE_ONLY_GENRE": 0,
    "PRINT_SEARCH_INFO": 0,
    "SEARCH_CANDIDATE_LIMIT": 5,
    "SCRAPE_ALBUM_DETAILS": 1,
    "MARKET": None,
}

for _key, _val in _DEFAULTS.items():
    if not hasattr(config, _key):
        setattr(config, _key, _val)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """
    Parses CLI flags that override `config.py` defaults for the current execution run.

    Why: Using `BooleanOptionalAction` (`--flag` / `--no-flag`) with `default=None`
    allows users to both enable *and* disable flags that default to `True` in `config.py`
    (such as `OVERWRITE_TITLE_ARTIST_OR_ALBUM` and `SCRAPE_ALBUM_DETAILS`).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize local audio metadata (FLAC/MP3) with the Spotify Web Player "
            "using Chrome/ChromeDriver and local genre caching."
        )
    )
    parser.add_argument(
        "--recursive",
        dest="recursive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Scan subdirectories recursively.",
    )
    parser.add_argument(
        "--process-top-x",
        dest="process_top_x",
        type=int,
        default=None,
        help="Maximum number of most-recently created files to process (0 = all).",
    )
    parser.add_argument(
        "--overwrite-taa",
        dest="overwrite_taa",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Overwrite existing Title, Artist, and Album tags.",
    )
    parser.add_argument(
        "--update-only-genre",
        dest="update_only_genre",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Only update the Genre tag and leave all other fields/artwork untouched.",
    )
    parser.add_argument(
        "--only-missing-album",
        dest="only_missing_album",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Only process files that are missing the Album tag.",
    )
    parser.add_argument(
        "--scrape-album-details",
        dest="scrape_album_details",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Visit the matched album page in Chrome to extract release date and track number.",
    )
    parser.add_argument(
        "--confirm",
        dest="confirm",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Prompt for interactive confirmation in console before overwriting tags on disk.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help="Automatically answer yes to confirmation prompt.",
    )
    parser.add_argument(
        "--print-search-info",
        dest="print_search_info",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Append candidate scoring traces to spotify_search_debug.log.",
    )
    parser.add_argument(
        "--input-path",
        dest="input_path",
        type=str,
        default=None,
        help="Override music_path from credentials.json.",
    )
    parser.add_argument(
        "--output-path",
        dest="output_path",
        type=str,
        default=None,
        help="Directory to write updated audio files (defaults to in-place update).",
    )
    return parser.parse_args(argv)


def apply_cli_overrides_to_config(args: argparse.Namespace) -> None:
    """
    Applies non-null CLI arguments onto `config` module attributes.

    Why: Mutating `config` prior to pipeline execution ensures both `RuntimeConfig.from_module()`
    and any legacy direct `config.<ATTR>` lookups observe the CLI overrides.
    """
    if getattr(args, "recursive", None) is not None:
        config.RECURSIVE = bool(args.recursive)

    if getattr(args, "process_top_x", None) is not None:
        try:
            config.PROCESS_TOP_X = max(0, int(args.process_top_x))
        except (TypeError, ValueError):
            logging.warning("Invalid --process-top-x value; keeping default.")

    if getattr(args, "overwrite_taa", None) is not None:
        config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = int(bool(args.overwrite_taa))

    if getattr(args, "update_only_genre", None) is not None:
        config.UPDATE_ONLY_GENRE = int(bool(args.update_only_genre))

    if getattr(args, "only_missing_album", None) is not None:
        config.ONLY_MISSING_ALBUM = int(bool(args.only_missing_album))

    if getattr(args, "scrape_album_details", None) is not None:
        config.SCRAPE_ALBUM_DETAILS = int(bool(args.scrape_album_details))

    if getattr(args, "print_search_info", None) is not None:
        config.PRINT_SEARCH_INFO = int(bool(args.print_search_info))


def _load_default_input_path() -> str | None:
    """
    Reads `music_path` or `source_dir` from `credentials.json` if the file exists.

    Why: Allows users to run `python main.py` without passing `--input-path` every time,
    while no longer requiring API tokens in `credentials.json`.
    """
    if not config.CREDENTIALS_PATH.exists():
        return config.DEFAULT_BASE_PATH
    try:
        creds_data = json.loads(config.CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if isinstance(creds_data, dict):
            path_val = creds_data.get("music_path") or creds_data.get("source_dir")
            return str(path_val).strip() if path_val else config.DEFAULT_BASE_PATH
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Could not read %s: %s", config.CREDENTIALS_PATH.name, exc)
    return config.DEFAULT_BASE_PATH


async def _download_artwork_batch(
    matched_items: Sequence[tuple[LocalTrackInfo, ResolvedMetadata, str | None]],
) -> list[PendingTagUpdate]:
    """
    Downloads `640x640` album cover art concurrently via `aiohttp` after Chrome closes.

    Why: Decoupling CDN image downloads from Selenium allows the browser instance to be
    terminated immediately after DOM scraping while fetching images in parallel.
    """
    runtime_cfg = config.RuntimeConfig.from_module()
    semaphore = asyncio.Semaphore(runtime_cfg.max_concurrent_requests)

    async with aiohttp.ClientSession() as session:

        async def _build_update(
            track: LocalTrackInfo,
            meta: ResolvedMetadata,
            image_url: str | None,
        ) -> PendingTagUpdate:
            img_bytes: bytes | None = None
            mime: str | None = None
            if image_url and not runtime_cfg.update_only_genre:
                art = await download_image_bytes(session, image_url, semaphore=semaphore)
                if art:
                    img_bytes, mime = art
            return PendingTagUpdate(
                file_path=track.file_path,
                ext=track.ext,
                metadata_map=meta,
                image_bytes=img_bytes,
                mime=mime,
            )

        tasks = [
            _build_update(track, meta, img_url)
            for track, meta, img_url in matched_items
        ]
        return list(await asyncio.gather(*tasks))


def main(argv: Sequence[str] | None = None) -> None:
    """
    Executes the end-to-end metadata synchronization workflow using Chrome WebDriver.

    Why: Coordinates CLI overrides, file discovery, Stage 1 local tag extraction,
    Stage 2 Selenium DOM scraping + async CDN cover art downloads, and Stage 3 atomic
    tag persistence.
    """
    args = parse_args(argv)
    apply_cli_overrides_to_config(args)
    runtime_cfg = config.RuntimeConfig.from_module()

    input_path = args.input_path or _load_default_input_path()
    if not input_path:
        logging.error("No music library path defined in credentials.json or --input-path.")
        return

    source_dir = Path(input_path)
    if not source_dir.exists() or not source_dir.is_dir():
        logging.error("Invalid source directory: %s", source_dir)
        return

    output_dir = Path(args.output_path) if args.output_path else source_dir
    if not output_dir.exists() or not output_dir.is_dir():
        logging.error("Invalid output directory: %s", output_dir)
        return

    # Discover and prioritize newest audio files first.
    paths = list(iter_audio_files(source_dir, runtime_cfg.recursive))
    paths.sort(key=get_creation_time, reverse=True)
    total_found = len(paths)
    logging.info("Discovered %d audio files (FLAC/MP3) in %s", total_found, source_dir)

    if runtime_cfg.process_top_x > 0 and not runtime_cfg.only_missing_album:
        paths = paths[: min(runtime_cfg.process_top_x, total_found)]

    total_to_process = len(paths)
    if total_to_process == 0:
        logging.info("No files to process. Exiting.")
        return

    updated = 0
    skipped = 0
    failed = 0

    # ---------------------------------------------------------
    # STAGE 1: Extract Local Metadata (ThreadPoolExecutor)
    # ---------------------------------------------------------
    logging.info("Stage 1/3: Extracting local metadata tags (%d files)...", total_to_process)
    local_data_list: list[LocalTrackInfo] = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        for res in executor.map(extract_local_tags, paths):
            if res is not None:
                local_data_list.append(res)
            elif not runtime_cfg.only_missing_album:
                skipped += 1

    if runtime_cfg.only_missing_album and runtime_cfg.process_top_x > 0:
        local_data_list = local_data_list[: runtime_cfg.process_top_x]

    if not local_data_list:
        logging.info(
            "Summary: Updated: 0 | Skipped: %d | Failed: 0 | Total: %d",
            skipped,
            total_found,
        )
        return

    # ---------------------------------------------------------
    # STAGE 2: Scrape Spotify Web Player DOM + Download Cover Art
    # ---------------------------------------------------------
    logging.info(
        "Stage 2/3: Scraping Spotify Web Player via Chrome (%d files)...",
        len(local_data_list),
    )
    matched_items: list[tuple[LocalTrackInfo, ResolvedMetadata, str | None]] = []
    skipped_filenames: list[str] = []

    scraper = SpotifyWebScraper()
    try:
        scraper.ensure_logged_in()
        for idx, track_info in enumerate(local_data_list, start=1):
            logging.info(
                "  [%d/%d] Searching Spotify Web Player for: %s",
                idx,
                len(local_data_list),
                track_info.file_path.name,
            )
            match_res = match_track_via_browser(scraper, track_info)
            if match_res is not None:
                resolved_meta, image_url = match_res
                matched_items.append((track_info, resolved_meta, image_url))
            else:
                skipped_filenames.append(track_info.file_path.name)
    finally:
        # Always release the Chrome WebDriver before downloading images and writing tags.
        scraper.close()

    skipped += len(skipped_filenames)

    valid_updates: list[PendingTagUpdate] = []
    if matched_items:
        logging.info("Downloading high-resolution cover art for %d matched tracks...", len(matched_items))
        valid_updates = asyncio.run(_download_artwork_batch(matched_items))

    # ---------------------------------------------------------
    # STAGE 3: Commit Metadata to Disk (ThreadPoolExecutor)
    # ---------------------------------------------------------
    if valid_updates:
        if args.confirm and not args.yes:
            print("\n" + "=" * 65)
            print("  PROPOSED METADATA OVERWRITES FOR REVIEW")
            print("=" * 65)
            for idx, upd in enumerate(valid_updates, start=1):
                m = upd.metadata_map
                print(f"[{idx}/{len(valid_updates)}] {upd.file_path.name}")
                print(f"   -> Title:  {m.title}")
                print(f"   -> Artist: {m.artist}")
                print(f"   -> Album:  {m.album}")
                print(f"   -> Date:   {m.date}")
                print(f"   -> Genre:  {m.genre}")
                print(f"   -> Cover:  {'Yes (640x640)' if upd.image_bytes else 'No'}")
            print("=" * 65)
            proceed = input("\nProceed to overwrite tags on disk? [Y/n]: ").strip().lower()
            if proceed and proceed not in ("y", "yes"):
                logging.info("Operation cancelled by user. No files were modified.")
                return

        logging.info("Stage 3/3: Committing %d metadata updates to disk...", len(valid_updates))

        def _commit_worker(update_item: PendingTagUpdate) -> bool:
            return commit_tags_to_file(
                update_item,
                output_dir=output_dir,
                source_root=source_dir,
            )

        with concurrent.futures.ThreadPoolExecutor() as executor:
            for success in executor.map(_commit_worker, valid_updates):
                if success:
                    updated += 1
                else:
                    failed += 1

    logging.info(
        "Summary: Updated: %d | Skipped: %d | Failed: %d | Total processed: %d",
        updated,
        skipped,
        failed,
        total_to_process,
    )

    if skipped_filenames:
        logging.info("\n--- SKIPPED FILES (No Spotify Match) ---")
        for filename in sorted(skipped_filenames):
            logging.info(" - %s", filename)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
