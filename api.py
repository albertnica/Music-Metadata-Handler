"""
api.py
Unified Python programmatic API and JSON CLI interface for Music Metadata Handler.

Why this module exists:
Provides a clean, typed programmatic entrypoint and headless subprocess interface for
inspecting audio tags, querying Spotify Web Player candidates, and persisting metadata
changes without spawning GUI servers or interacting directly with internal stage orchestrators.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import sys
from typing import Any, Sequence

import aiohttp

import config
from modules.core import LocalTrackInfo, PendingTagUpdate, ResolvedMetadata
from modules.processor import (
    commit_tags_to_file,
    extract_local_tags,
    map_spotify_match_to_metadata,
    match_track_via_browser,
)
from modules.scanner import KNOWN_CRITERIA, inspect_audio_file, scan_audio_library
from modules.spotify_client import SpotifyWebScraper, download_image_bytes


@dataclass(slots=True)
class TrackDiff:
    """Represents a proposed metadata transformation for pre-commit review."""
    file_path: str
    filename: str
    ext: str
    original_title: str
    original_artist: str
    original_album: str
    proposed_title: str
    proposed_artist: str
    proposed_album: str
    proposed_date: str
    proposed_genre: str
    image_url: str
    has_cover: bool
    staged_update: PendingTagUpdate | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializes the diff into a JSON-safe dictionary."""
        return {
            "file_path": self.file_path,
            "filename": self.filename,
            "ext": self.ext,
            "original": {
                "title": self.original_title,
                "artist": self.original_artist,
                "album": self.original_album,
            },
            "proposed": {
                "title": self.proposed_title,
                "artist": self.proposed_artist,
                "album": self.proposed_album,
                "date": self.proposed_date,
                "genre": self.proposed_genre,
                "image_url": self.image_url,
                "has_cover": self.has_cover,
            },
        }


class MusicMetadataStudio:
    """
    High-level API client for headless inspection and synchronization.

    Example usage:
    ```python
    from api import MusicMetadataStudio

    studio = MusicMetadataStudio()

    # 1. Audit library
    report = studio.scan("C:/Music", recursive=True, criteria=["lack_cover"])

    # 2. Preview changes without writing to disk
    diff = studio.resolve_file("C:/Music/track.flac")
    print(diff.to_dict())

    # 3. Apply changes
    studio.commit_diff(diff)
    ```
    """

    def __init__(self) -> None:
        self._scraper: SpotifyWebScraper | None = None

    def _get_scraper(self) -> SpotifyWebScraper:
        """Lazily instantiates the Selenium Web Player scraper."""
        if self._scraper is None:
            self._scraper = SpotifyWebScraper()
        return self._scraper

    def close(self) -> None:
        """Terminates active Selenium WebDriver instances."""
        if self._scraper is not None:
            self._scraper.close()
            self._scraper = None

    def __enter__(self) -> MusicMetadataStudio:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def scan(
        self,
        folder_path: str | Path,
        recursive: bool = False,
        criteria: list[str] | None = None,
        filename_parse_mode: int = 0,
    ) -> dict[str, Any]:
        """
        Audits an audio directory and returns deficiency reports.
        """
        config.FILENAME_PARSE_MODE = filename_parse_mode
        p = Path(folder_path).resolve()
        return scan_audio_library(p, recursive=recursive, criteria=criteria)

    def inspect_file(self, file_path: str | Path) -> dict[str, Any] | None:
        """Reads embedded Vorbis/ID3 tags and evaluates health for a single audio file."""
        p = Path(file_path).resolve()
        return inspect_audio_file(p)

    def resolve_file(
        self,
        file_path: str | Path,
        scrape_album: bool = True,
        overwrite_taa: bool = True,
        update_only_genre: bool = False,
        filename_parse_mode: int = 0,
    ) -> TrackDiff | None:
        """
        Searches Spotify Web Player for matching metadata and returns a pre-commit diff.
        Does NOT modify the file on disk.
        """
        config.FILENAME_PARSE_MODE = filename_parse_mode
        config.SCRAPE_ALBUM_DETAILS = 1 if scrape_album else 0
        config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = 1 if overwrite_taa else 0
        config.UPDATE_ONLY_GENRE = 1 if update_only_genre else 0

        p = Path(file_path).resolve()
        local_info = extract_local_tags(p)
        if local_info is None:
            return None

        scraper = self._get_scraper()
        scraper.ensure_logged_in()
        match_res = match_track_via_browser(scraper, local_info)
        if match_res is None:
            return None

        resolved_meta, image_url = match_res

        # Fetch artwork bytes into memory
        image_bytes: bytes | None = None
        mime: str | None = None
        if image_url and not update_only_genre:
            async def _fetch() -> tuple[bytes, str] | None:
                async with aiohttp.ClientSession() as session:
                    return await download_image_bytes(session, image_url)
            artwork = asyncio.run(_fetch())
            if artwork:
                image_bytes, mime = artwork

        pending = PendingTagUpdate(
            file_path=p,
            ext=local_info.ext,
            metadata_map=resolved_meta,
            image_bytes=image_bytes,
            mime=mime,
        )

        return TrackDiff(
            file_path=str(p),
            filename=p.name,
            ext=local_info.ext,
            original_title=local_info.id3_title or local_info.fn_title or "",
            original_artist=local_info.id3_artist or local_info.fn_artist or "",
            original_album=local_info.id3_album or "",
            proposed_title=resolved_meta.title,
            proposed_artist=resolved_meta.artist,
            proposed_album=resolved_meta.album,
            proposed_date=resolved_meta.date,
            proposed_genre=resolved_meta.genre,
            image_url=image_url or "",
            has_cover=bool(image_bytes),
            staged_update=pending,
        )

    def commit_diff(self, diff: TrackDiff) -> bool:
        """Atomically persists a resolved `TrackDiff` to disk."""
        if diff.staged_update is None:
            raise ValueError("TrackDiff contains no staged update payload to commit.")
        return commit_tags_to_file(diff.staged_update)

    def sync_file(
        self,
        file_path: str | Path,
        scrape_album: bool = True,
        overwrite_taa: bool = True,
        update_only_genre: bool = False,
        filename_parse_mode: int = 0,
    ) -> bool:
        """
        End-to-end atomic synchronization for a single audio file.
        """
        diff = self.resolve_file(
            file_path,
            scrape_album=scrape_album,
            overwrite_taa=overwrite_taa,
            update_only_genre=update_only_genre,
            filename_parse_mode=filename_parse_mode,
        )
        if diff is None:
            return False
        return self.commit_diff(diff)


# ---------------------------------------------------------------------------
# CLI & Subprocess Interface
# ---------------------------------------------------------------------------

def _cli_scan(args: argparse.Namespace) -> None:
    p = Path(args.path).resolve()
    criteria = [c.strip() for c in args.criteria.split(",")] if args.criteria else ["all"]
    with MusicMetadataStudio() as studio:
        report = studio.scan(p, recursive=args.recursive, criteria=criteria, filename_parse_mode=args.mode)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Scanned: {report['total_scanned']} | Matched: {report['total_matched']} | FLAC: {report['flac_count']} | MP3: {report['mp3_count']}")
        for t in report.get("tracks", []):
            print(f" - [{t['ext'].upper()}] {t['filename']} -> Issues: {', '.join(t['issues']) or 'None'}")


def _cli_inspect(args: argparse.Namespace) -> None:
    p = Path(args.path).resolve()
    with MusicMetadataStudio() as studio:
        data = studio.inspect_file(p)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        if not data:
            print("File not found or unreadable.")
            return
        print(f"File: {data['filename']}")
        print(f"Title:  {data['title'] or '(empty)'}")
        print(f"Artist: {data['artist'] or '(empty)'}")
        print(f"Album:  {data['album'] or '(empty)'}")
        print(f"Date:   {data['date'] or '(empty)'}")
        print(f"Genre:  {data['genre'] or '(empty)'}")
        print(f"Cover:  {'Embedded' if data['has_cover'] else 'Missing'}")


def _cli_sync(args: argparse.Namespace) -> None:
    target = Path(args.path).resolve()
    with MusicMetadataStudio() as studio:
        if target.is_file():
            files = [target]
        elif target.is_dir():
            report = studio.scan(target, recursive=args.recursive, criteria=["all"], filename_parse_mode=args.mode)
            files = [Path(t["file_path"]) for t in report["tracks"]]
        else:
            print(f"Error: Target path does not exist: {target}", file=sys.stderr)
            sys.exit(1)

        diffs: list[dict[str, Any]] = []
        for f in files:
            diff = studio.resolve_file(
                f,
                scrape_album=not args.no_album_details,
                overwrite_taa=not args.no_overwrite_taa,
                update_only_genre=args.only_genre,
                filename_parse_mode=args.mode,
            )
            if diff:
                if not args.dry_run:
                    studio.commit_diff(diff)
                diffs.append(diff.to_dict())

        if args.json:
            print(json.dumps({"dry_run": args.dry_run, "total": len(diffs), "results": diffs}, indent=2, ensure_ascii=False))
        else:
            action = "PREVIEWED (Dry Run)" if args.dry_run else "COMMITTED"
            print(f"{action}: {len(diffs)} file(s).")
            for d in diffs:
                print(f" * {d['filename']} -> '{d['proposed']['title']}' by '{d['proposed']['artist']}'")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="api.py",
        description="Music Metadata Studio Subprocess & Scripting API",
    )
    parser.add_argument("--json", action="store_true", help="Format stdout output strictly as JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scan command
    p_scan = subparsers.add_parser("scan", help="Scan directory and report metadata deficiencies")
    p_scan.add_argument("path", help="Directory path to scan")
    p_scan.add_argument("--recursive", "-r", action="store_true", help="Scan subfolders recursively")
    p_scan.add_argument("--criteria", "-c", help="Comma-separated criteria (lack_cover, lack_title, lack_artist, lack_album, lack_date, lack_genre, all)")
    p_scan.add_argument("--mode", "-m", type=int, default=0, choices=[0, 1], help="Filename format: 0=Artist-Title, 1=Title-Artist")
    p_scan.set_defaults(func=_cli_scan)

    # Inspect command
    p_inspect = subparsers.add_parser("inspect", help="Inspect embedded tags for a single file")
    p_inspect.add_argument("path", help="Audio file path to inspect")
    p_inspect.set_defaults(func=_cli_inspect)

    # Sync command
    p_sync = subparsers.add_parser("sync", help="Synchronize audio file(s) with Spotify Web Player")
    p_sync.add_argument("path", help="Audio file or folder to synchronize")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview proposed metadata changes without writing to disk")
    p_sync.add_argument("--recursive", "-r", action="store_true", help="Scan subfolders recursively if path is a directory")
    p_sync.add_argument("--no-overwrite-taa", action="store_true", help="Keep existing Title/Artist/Album tags")
    p_sync.add_argument("--only-genre", action="store_true", help="Only synchronize the Genre tag")
    p_sync.add_argument("--no-album-details", action="store_true", help="Skip /album/{id} scraping")
    p_sync.add_argument("--mode", "-m", type=int, default=0, choices=[0, 1], help="Filename format: 0=Artist-Title, 1=Title-Artist")
    p_sync.set_defaults(func=_cli_sync)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

