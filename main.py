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
# Using config.something is preferred in this case
import config

# Ensure config has expected defaults so main can rely on them as baseline values.
# If a key is missing from config.py, set a sane default here (CLI can still override).
_DEFAULTS = {
    "RECURSIVE": False,
    "PROCESS_TOP_X": 1,
    "OVERWRITE_TITLE_ARTIST_OR_ALBUM": 1,
    "UPDATE_ONLY_GENRE": 0,
    "PRINT_SEARCH_INFO": 0,
    "SEARCH_CANDIDATE_LIMIT": 5,
    "MARKET": None
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

    parser.add_argument("--overwrite-taa", dest="overwrite_taa", action="store_true", default=None,
                        help="--overwrite-taa: Overwrite title/artist/album fields (overrides config.OVERWRITE_TITLE_ARTIST_OR_ALBUM)")

    parser.add_argument("--update-only-genre", dest="update_only_genre", action="store_true", default=None,
                        help="--update-only-genre: Only update genre fields (overrides config.UPDATE_ONLY_GENRE)")

    parser.add_argument("--input-path", dest="input_path", type=str, default=None,
                        help="--input-path path: Override music_path from credentials.json with this path")
    parser.add_argument("--output-path", dest="output_path", type=str, default=None,
                        help="--output-path path: Directory to write outputs.")
    parser.add_argument("--token", dest="token", type=str, default=None,
                        help="--token string: Manual Spotify access token to use")

    parser.add_argument("--print-search-info", dest="print_search_info", action="store_true", default=None,
                        help="--print-search-info: Print extended search logs (overrides config.PRINT_SEARCH_INFO)")

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

    if getattr(args, "overwrite_taa", False) is not None:
        config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = int(bool(args.overwrite_taa))

    if getattr(args, "update_only_genre", False) is not None:
        config.UPDATE_ONLY_GENRE = int(bool(args.update_only_genre))

    if getattr(args, "print_search_info", None) is not None:
        config.PRINT_SEARCH_INFO = int(bool(args.print_search_info))


def main():
    # Parse CLI args first
    args = parse_args()

    # Apply overrides to config BEFORE importing modules that read them at import-time
    apply_cli_overrides_to_config(args)

    # Now import modules that depend on config values
    try:
        from modules.core import iter_audio_files, get_creation_time
        from modules.processor import process_single_file
    except Exception as e:
        logging.error("Failed importing modules: %s", e)
        sys.exit()

    # Load credentials and music path from credentials.json
    try:
        with config.CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Token: CLI override preferred; then credentials.json
        spotify_token = args.token or data.get("spotify_access_token")
        if not spotify_token:
            logging.error("No Spotify access token found. Provide it in credentials.json or via --token.")
            return

        # Input path: CLI override preferred; else credentials.json
        if args.input_path:
            input_path = args.input_path
        else:
            input_path = data.get("music_path") or data.get("source_dir") or data.get("music_dir")

        if not input_path:
            logging.error("Missing music_path in credentials.json and no --input-path provided")
            return
        SOURCE_DIR = Path(input_path)
        if not SOURCE_DIR.exists() or not SOURCE_DIR.is_dir():
            logging.error("input_path is not a valid directory: %s", SOURCE_DIR)
            return
        logging.info("Loaded input path: %s", SOURCE_DIR)

        # Output path: default to input path unless overridden
        if args.output_path:
            OUTPUT_DIR = Path(args.output_path)
        else:
            OUTPUT_DIR = SOURCE_DIR
        if not OUTPUT_DIR.exists() or not OUTPUT_DIR.is_dir():
            logging.error("output_path is not a valid directory: %s", OUTPUT_DIR)
            return
        logging.info("Using output path: %s", OUTPUT_DIR)
        
    except FileNotFoundError:
        logging.error("Credentials file not found: %s", config.CREDENTIALS_PATH)
        return
    except json.JSONDecodeError as e:
        logging.error("Error parsing credentials.json: %s", e)
        return
    except Exception as e:
        logging.error("Failed to load credentials: %s", e)
        return

    # Use possibly overridden PROCESS_TOP_X and RECURSIVE from config
    paths = [p for p in iter_audio_files(SOURCE_DIR, config.RECURSIVE)]
    paths.sort(key=lambda p: get_creation_time(p) if p.exists() else 0, reverse=True)
    total = len(paths)
    logging.info("Found %d audio files (FLAC/MP3/WAV) in %s", total, SOURCE_DIR)

    if isinstance(config.PROCESS_TOP_X, int) and config.PROCESS_TOP_X > 0:
        limit = min(config.PROCESS_TOP_X, total)
        paths = paths[:limit]

    updated = skipped = failed = 0
    for i, path in enumerate(paths, 1):
        try:
            logging.info("Processing (%d/%d): %s", i, len(paths), path.name)
            # Pass the token to process_single_file
            ok = process_single_file(path, token=spotify_token, output_dir=OUTPUT_DIR)
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
