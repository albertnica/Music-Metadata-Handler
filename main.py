"""
main.py
Entrypoint for the metadata updater. Loads credentials, obtains Spotify token,
iterates audio files and updates metadata using modules.processor.
If an option is not provided, the default from config.py is used.
"""

import argparse
import json
import time
import logging
from pathlib import Path
import sys

# Import config module early so we can mutate it based on CLI args BEFORE importing other modules
import config

# Ensure config has expected defaults so main can rely on them as baseline values.
# If a key is missing from config.py, set a sane default here (CLI can still override).
_DEFAULTS = {
    "RECURSIVE": False,
    "PROCESS_TOP_X": 1,
    "OVERWRITE_TITLE_ARTIST_OR_ALBUM": 0,
    "UPDATE_ONLY_GENRE": 0,
    "PRINT_SEARCH_INFO": 0,
    "SEARCH_CANDIDATE_LIMIT": 5,
    "MARKET": None,
}
for _k, _v in _DEFAULTS.items():
    if not hasattr(config, _k):
        setattr(config, _k, _v)

def parse_args():
    parser = argparse.ArgumentParser(
        description="Update audio file metadata using Spotify. CLI overrides config.py defaults."
    )
    # Boolean flags use default=None so we can detect "not provided" and keep config defaults.
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=None,
                        help="--recursive: Process files in subfolders (overrides config.RECURSIVE)")

    parser.add_argument("--process-top-x", dest="process_top_x", type=int, default=None,
                        help="--process-top-x int: Number of files to process (overrides config.PROCESS_TOP_X)")

    parser.add_argument("--overwrite-taa", dest="overwrite_title_artist_or_album", action="store_true", default=None,
                        help="--overwrite-taa: Overwrite title/artist/album fields (overrides config.OVERWRITE_TITLE_ARTIST_OR_ALBUM)")

    parser.add_argument("--update-only-genre", dest="update_only_genre", action="store_true", default=None,
                        help="--update-only-genre: Only update genre fields (overrides config.UPDATE_ONLY_GENRE)")

    parser.add_argument("--music-path", dest="music_path", type=str, default=None,
                        help="--music-path path: Override music_path from credentials.json with this path")

    # Pass-through extra args to be tolerant
    return parser.parse_args()


def apply_cli_overrides_to_config(args):
    """
    Mutate config module attributes based on parsed CLI args.
    """
    if getattr(args, "recursive", None) is not None:
        config.RECURSIVE = bool(args.recursive)

    if args.process_top_x is not None:
        try:
            config.PROCESS_TOP_X = int(args.process_top_x)
        except Exception:
            logging.warning("Invalid process_top_x value passed; keeping config.PROCESS_TOP_X = %r", config.PROCESS_TOP_X)

    if getattr(args, "overwrite_title_artist_or_album", False) is not None:
        config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = int(bool(args.overwrite_title_artist_or_album))

    if getattr(args, "update_only_genre", False) is not None:
        config.UPDATE_ONLY_GENRE = int(bool(args.update_only_genre))

    # Note: music_path is handled separately when reading credentials.json


def main():
    # Parse CLI args first
    args = parse_args()

    # Apply overrides to config BEFORE importing modules that read them at import-time
    apply_cli_overrides_to_config(args)

    # Now import modules that depend on config values
    try:
        # Importing these after config overrides ensures modules read the updated config values
        from modules.processor import process_single_file
        from modules.core import iter_audio_files, get_creation_time
        from modules.spotify_client import get_spotify_token
    except Exception as e:
        logging.error("Failed importing modules after applying CLI overrides: %s", e)
        raise

    # Load credentials and music path from credentials.json
    try:
        with config.CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        client_id = str(data.get("client_id", "")).strip()
        client_secret = str(data.get("client_secret", "")).strip()

        # Music_path: CLI override preferred; else credentials.json
        if args.music_path:
            music_path = args.music_path
        else:
            music_path = data.get("music_path") or data.get("source_dir") or data.get("music_dir")

        if not client_id or not client_secret:
            logging.error("Missing client_id or client_secret in credentials.json")
            return
        if not music_path:
            logging.error("Missing music_path in credentials.json and no --music-path provided")
            return
        SOURCE_DIR = Path(music_path)
        if not SOURCE_DIR.exists() or not SOURCE_DIR.is_dir():
            logging.error("music_path from credentials.json / CLI is not a valid directory: %s", SOURCE_DIR)
            return
        logging.info("Loaded music path: %s", SOURCE_DIR)

        token, expires_at = get_spotify_token(client_id, client_secret)
        logging.info("Spotify token obtained")
    except FileNotFoundError:
        logging.error("Credentials file not found: %s", config.CREDENTIALS_PATH)
        return
    except json.JSONDecodeError as e:
        logging.error("Error parsing credentials.json: %s", e)
        return
    except Exception as e:
        logging.error("Failed to load credentials or obtain token: %s", e)
        return

    # Use possibly overridden PROCESS_TOP_X and RECURSIVE from config
    paths = [p for p in iter_audio_files(SOURCE_DIR, config.RECURSIVE)]
    paths.sort(key=lambda p: get_creation_time(p) if p.exists() else 0, reverse=True)
    total = len(paths)
    logging.info("Found %d audio files (FLAC/MP3/WAV) in %s", total, SOURCE_DIR)

    if config.PROCESS_TOP_X and isinstance(config.PROCESS_TOP_X, int) and config.PROCESS_TOP_X > 0:
        limit = min(config.PROCESS_TOP_X, total)
        paths = paths[:limit]

    updated = skipped = failed = 0
    for i, path in enumerate(paths, 1):
        try:
            if int(time.time()) >= expires_at:
                try:
                    token, expires_at = get_spotify_token(client_id, client_secret)
                except Exception:
                    logging.error("Failed to refresh Spotify token")
                    break

            logging.info("Processing (%d/%d): %s", i, len(paths), path.name)
            ok = process_single_file(path, token)
            if ok:
                logging.info("Updated metadata for: %s (original moved to trash)", path.name)
                updated += 1
            else:
                skipped += 1

        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error("Unexpected error processing %s: %s", path.name, e)
            failed += 1

    logging.info("Completed. Updated: %d, Skipped: %d, Failed: %d, Total found: %d", updated, skipped, failed, total)


if __name__ == "__main__":
    # Ensure logging configured
    try:
        logging.getLogger().handlers  # just access to avoid lint warnings
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
