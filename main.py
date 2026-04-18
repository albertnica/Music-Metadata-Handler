"""
main.py
Entrypoint for the Music Metadata Handler. 
Orchestrates the metadata update process by managing Spotify authentication tokens, 
scanning for local audio files (FLAC/MP3), and triggering the asynchronous processing pipeline.
"""

import argparse
import json
import logging
from pathlib import Path
import sys
import asyncio
import concurrent.futures

# Import config module early to allow CLI overrides before other modules are initialized
import config

# Standard baseline configuration values for the execution environment
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
    """
    Parses command-line arguments to allow runtime customization.
    """
    parser = argparse.ArgumentParser(
        description="Update audio file metadata using Spotify. CLI overrides config.py defaults."
    )
    parser.add_argument("--recursive", dest="recursive", action="store_true", default=None,
                        help="Process files in subfolders.")
    parser.add_argument("--process-top-x", dest="process_top_x", type=int, default=None,
                        help="Number of files to process.")
    parser.add_argument("--overwrite-taa", dest="overwrite_taa", action="store_true", default=None,
                        help="Overwrite title/artist/album fields.")
    parser.add_argument("--update-only-genre", dest="update_only_genre", action="store_true", default=None,
                        help="Only update genre fields.")
    parser.add_argument("--input-path", dest="input_path", type=str, default=None,
                        help="Override music_path from credentials.json.")
    parser.add_argument("--output-path", dest="output_path", type=str, default=None,
                        help="Directory to write outputs.")
    return parser.parse_args()

def apply_cli_overrides_to_config(args):
    """
    Mutates the config module attributes based on user-provided CLI arguments.
    """
    if getattr(args, "recursive", None) is not None:
        config.RECURSIVE = bool(args.recursive)

    if args.process_top_x is not None:
        try:
            config.PROCESS_TOP_X = int(args.process_top_x)
        except Exception:
            logging.warning("Invalid process_top_x value; keeping default.")

    if getattr(args, "overwrite_taa", False) is not None:
        config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = int(bool(args.overwrite_taa))

    if getattr(args, "update_only_genre", False) is not None:
        config.UPDATE_ONLY_GENRE = int(bool(args.update_only_genre))


def save_token_to_credentials(token: str, original_data: dict):
    """
    Persists a valid Bearer token back to credentials.json.
    """
    try:
        data = original_data.copy()
        data["token"] = token
        data.pop("client_id", None)
        data.pop("client_secret", None)
        
        with config.CREDENTIALS_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logging.info("Token successfully saved to credentials.json")
    except Exception as e:
        logging.error("Failed to save token to credentials file: %s", e)


def get_valid_token(current_token: str, original_data: dict):
    """
    Validates the current token and prompts for a new one if unauthorized (401) or malformed (400).
    Automatically strips "Bearer " prefix if user accidentally pastes it.
    """
    from modules.spotify_client import spotifysearch
    
    token = current_token
    attempts = 0
    while attempts < 3:
        if not token:
            logging.warning("No Spotify token found.")
            token = input("Please enter a new Spotify Bearer Token: ").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if not token:
                logging.error("No token provided. Exiting.")
                sys.exit(1)
            save_token_to_credentials(token, original_data)
            continue
            
        async def _test_token():
            import aiohttp
            sem = asyncio.Semaphore(1)
            async with aiohttp.ClientSession() as session:
                return await spotifysearch(session, sem, token, "test", limit=1)
                
        res = asyncio.run(_test_token())
        
        if res and "error" in res and res["error"].get("status") in (401, 400):
            logging.warning("Current Spotify token is invalid or expired.")
            token = input("Please enter a new Spotify Bearer Token: ").strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if not token:
                logging.error("No token provided. Exiting.")
                sys.exit(1)
            save_token_to_credentials(token, original_data)
            attempts += 1
        elif res is None:
            logging.error("Unable to reach Spotify API. Please check your connection.")
            sys.exit(1)
        else:
            return token
            
    logging.error("Multiple invalid token attempts. Please check your source.")
    sys.exit(1)


def main():
    args = parse_args()
    apply_cli_overrides_to_config(args)

    try:
        from modules.core import iter_audio_files, get_creation_time
        from modules.processor import extract_local_tags, fetch_spotify_data_async, commit_tags_to_file
    except Exception as e:
        logging.error("Resource initialization failed: %s", e)
        sys.exit(1)

    # Configuration loading and path validation
    try:
        with config.CREDENTIALS_PATH.open("r", encoding="utf-8") as f:
            creds_data = json.load(f)
        
        token = str(creds_data.get("token", "")).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
            
        input_path = args.input_path or creds_data.get("music_path") or creds_data.get("source_dir")
        
        if not input_path:
            logging.error("No music library path defined in credentials.json or CLI.")
            return

        SOURCE_DIR = Path(input_path)
        if not SOURCE_DIR.exists() or not SOURCE_DIR.is_dir():
            logging.error("Invalid source directory: %s", SOURCE_DIR)
            return

        OUTPUT_DIR = Path(args.output_path) if args.output_path else SOURCE_DIR
        if not OUTPUT_DIR.exists() or not OUTPUT_DIR.is_dir():
            logging.error("Invalid output directory: %s", OUTPUT_DIR)
            return

        # Pre-execution token validation
        token = get_valid_token(token, creds_data)
        logging.info("Spotify authentication successful.")

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error("Credentials configuration error: %s", e)
        return

    # File discovery and prioritization
    paths = list(iter_audio_files(SOURCE_DIR, config.RECURSIVE))
    paths.sort(key=lambda p: get_creation_time(p), reverse=True)
    total_found = len(paths)
    logging.info("Discovered %d audio files (FLAC/MP3) in %s", total_found, SOURCE_DIR)

    if config.PROCESS_TOP_X > 0:
        paths = paths[:min(config.PROCESS_TOP_X, total_found)]

    total_to_process = len(paths)
    if total_to_process == 0:
        logging.info("No files to process. Exiting.")
        return

    updated = skipped = failed = 0

    # ---------------------------------------------------------
    # STAGE 1: Extract Local Metadata (ThreadPoolExecutor)
    # ---------------------------------------------------------
    logging.info("Stage 1/3: Extracting local metadata tags (%d files)...", total_to_process)
    local_data_list = []
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = executor.map(extract_local_tags, paths)
        for res in results:
            if res is not None:
                local_data_list.append(res)
            else:
                skipped += 1

    if not local_data_list:
        logging.info("Summary: Updated: 0 | Skipped: %d | Failed: 0 | Total: %d", skipped, total_found)
        return

    # ---------------------------------------------------------
    # STAGE 2: Fetch Spotify Metadata (Asyncio Network I/O)
    # ---------------------------------------------------------
    logging.info("Stage 2/3: Fetching Spotify metadata asynchronously...")
    
    async def fetch_all(loop_data, access_token):
        import aiohttp
        # Semaphore limits concurrent requests to prevent HTTP 429 Too Many Requests
        semaphore = asyncio.Semaphore(3)
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_spotify_data_async(session, semaphore, access_token, param) for param in loop_data]
            return await asyncio.gather(*tasks)

    fetch_results = asyncio.run(fetch_all(local_data_list, token))
    
    valid_updates = []
    skipped_filenames = []
    
    for i, res in enumerate(fetch_results):
        if res is not None:
            valid_updates.append(res)
        else:
            skipped_filenames.append(local_data_list[i]["file_path"].name)
            
    skipped += len(skipped_filenames)

    # ---------------------------------------------------------
    # STAGE 3: Commit Metadata to Disk (ThreadPoolExecutor)
    # ---------------------------------------------------------
    if valid_updates:
        logging.info("Stage 3/3: Committing %d metadata updates to disk...", len(valid_updates))
        
        def commit_wrapper(update_data):
            return commit_tags_to_file(update_data, OUTPUT_DIR)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = executor.map(commit_wrapper, valid_updates)
            for ok in results:
                if ok:
                    updated += 1
                else:
                    failed += 1

    logging.info("Summary: Updated: %d | Skipped: %d | Failed: %d | Total processed: %d", updated, skipped, failed, total_to_process)
    
    if skipped_filenames:
        logging.info("\n--- SKIPPED FILES (No Spotify Match) ---")
        for fn in sorted(skipped_filenames):
            logging.info(f" - {fn}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
