"""
config.py
Centralized configuration repository for the Music Metadata Handler.

Why this module exists:
Decouples environment paths, persistent Chrome profile locations, genre priority
mappings, heuristic thresholds, and runtime flags from business logic so behavior
can be tuned globally or overridden via CLI arguments without modifying pipeline modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Path resolution for local resources.
# Anchoring to __file__ guarantees deterministic path resolution regardless of
# the current working directory from which the CLI is invoked.
MAIN_DIR: Path = Path(__file__).resolve().parent
CREDENTIALS_PATH: Path = MAIN_DIR / "credentials.json"
DEBUG_LOG_PATH: Path = MAIN_DIR / "spotify_search_debug.log"

# Persistent browser & genre cache storage (isolated inside data/).
DATA_DIR: Path = MAIN_DIR / "data"
CHROME_PROFILE_DIR: Path = DATA_DIR / "spotify_chrome_profile"
COOKIE_FILE: Path = DATA_DIR / "spotify_cookies.json"
GENRE_CACHE_FILE: Path = DATA_DIR / "genre_cache.json"

# Supported audio extensions (WAV is intentionally excluded to avoid fragile RIFF chunk mutations).
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".flac", ".mp3")

# Default base music library directory
DEFAULT_BASE_PATH: str = "C:/"

# File iteration / processing defaults
RECURSIVE: bool = False        # Prevents accidental deep scans of massive root directories unless requested
PROCESS_TOP_X: int = 1         # Caps batch size by default to prevent accidental long scraping runs

# Filename parsing mode:
# 0 = "Artist - Title" (default: left=artist, right=title)
# 1 = "Title - Artist" (reversed: left=title, right=artist)
# Why: Provides a deterministic fallback when embedded audio tags are missing or corrupted.
FILENAME_PARSE_MODE: int = 0

# Metadata mutation and diagnostics flags (kept as int/bool compatible values)
OVERWRITE_TITLE_ARTIST_OR_ALBUM: int = 1  # 1 = overwrite existing core tags with canonical Spotify metadata
UPDATE_ONLY_GENRE: int = 0                # 1 = preserve all existing tags/artwork and only populate genre
ONLY_MISSING_ALBUM: int = 0               # 1 = only process audio files whose Album tag is empty/missing
PRINT_SEARCH_INFO: int = 0                # 1 = write detailed candidate scoring traces to DEBUG_LOG_PATH
SEARCH_CANDIDATE_LIMIT: int = 5           # Maximum DOM track rows evaluated per search query
SCRAPE_ALBUM_DETAILS: int = 1             # 1 = visit /album/{id} page (cached) to extract release_date and track_number
MARKET: str | None = None                 # Optional ISO 3166-1 alpha-2 country code

# Spotify Web Player & API URLs
SPOTIFY_WEB_BASE_URL: str = "https://open.spotify.com"
SPOTIFY_WEB_SEARCH_TRACKS_URL: str = "https://open.spotify.com/search/{}/tracks"
SPOTIFY_WEB_ALBUM_URL: str = "https://open.spotify.com/album/{}"
SPOTIFY_TOKEN_URL: str = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL: str = "https://api.spotify.com/v1/search"
SPOTIFY_ARTIST_URL: str = "https://api.spotify.com/v1/artists/{}"
SPOTIFY_ARTIST_ALBUMS_URL: str = "https://api.spotify.com/v1/artists/{}/albums"
SPOTIFY_ALBUM_TRACKS_URL: str = "https://api.spotify.com/v1/albums/{}/tracks"

# Network and browser performance limits
REQUEST_TIMEOUT: int = 12                 # Seconds before aborting a stalled HTTP/Wait operation
SPOTIFY_MAX_LIMIT: int = 50               # Maximum items per batch
MAX_CONCURRENT_REQUESTS: int = 3          # Semaphore ceiling for concurrent CDN cover-art downloads
MAX_HTTP_RETRIES: int = 4                 # Maximum retry attempts on transient network failures

# Ordered subgenre priority list (ported from Telegram-Music-Downloader).
# Why: Spotify returns artist genres in arbitrary/alphabetical order where generic tags
# (e.g., "pop", "edm") often shadow specific subgenres (e.g., "Melodic Bass", "Dubstep").
GENRE_PRIORITY: tuple[tuple[str, str], ...] = (
    ("melodic bass", "Melodic Bass"),
    ("melodic dubstep", "Melodic Bass"),
    ("chillstep", "Chillstep"),
    ("future bass", "Future Bass"),
    ("deathstep", "Deathstep"),
    ("riddim", "Dubstep"),
    ("brostep", "Dubstep"),
    ("dubstep", "Dubstep"),
    ("drum and bass", "Drum And Bass"),
    ("dnb", "Drum And Bass"),
    ("liquid funk", "Drum And Bass"),
    ("drift phonk", "Drift Phonk"),
    ("phonk", "Phonk"),
    ("witch house", "Witch House"),
    ("synthwave", "Synthwave"),
    ("darksynth", "Synthwave"),
    ("chillwave", "Chillwave"),
    ("metalcore", "Metalcore"),
    ("post-hardcore", "Post-Hardcore"),
    ("pop punk", "Pop Punk"),
    ("nu metal", "Nu Metal"),
    ("hypertechno", "Hypertechno"),
    ("bass house", "Bass House"),
    ("future house", "Future House"),
    ("slap house", "Slap House"),
    ("deep house", "Deep House"),
    ("electro house", "Electro House"),
    ("big room", "Big Room"),
    ("moombahton", "Moombahton"),
    ("edm trap", "Edm Trap"),
    ("trap", "Edm Trap"),
    ("trance", "Trance"),
    ("progressive house", "Edm"),
    ("brazilian bass", "Brazilian Bass"),
    ("house", "House"),
    ("edm", "Edm"),
    ("electropop", "Edm"),
    ("dance pop", "Edm"),
    ("pop", "Pop"),
)


@dataclass(slots=True, frozen=True)
class RuntimeConfig:
    """
    Immutable snapshot of active runtime configuration settings.

    Why: Accessing module-level globals via `from config import X` binds values at
    import time, causing subtle bugs when CLI flags mutate `config` afterward.
    Snapshotting via `RuntimeConfig.from_module()` guarantees consistent reads.
    """

    recursive: bool
    process_top_x: int
    filename_parse_mode: int
    overwrite_title_artist_or_album: bool
    update_only_genre: bool
    only_missing_album: bool
    print_search_info: bool
    search_candidate_limit: int
    scrape_album_details: bool
    market: str | None
    request_timeout: int
    spotify_max_limit: int
    max_concurrent_requests: int
    max_http_retries: int

    @classmethod
    def from_module(cls) -> RuntimeConfig:
        """
        Builds a strongly-typed configuration snapshot from current module attributes.
        Why: Preserves backward compatibility with callers that mutate `config.<ATTR>`
        at runtime while enforcing strict boolean/integer types downstream.
        """
        return cls(
            recursive=bool(RECURSIVE),
            process_top_x=max(0, int(PROCESS_TOP_X)),
            filename_parse_mode=int(FILENAME_PARSE_MODE),
            overwrite_title_artist_or_album=bool(OVERWRITE_TITLE_ARTIST_OR_ALBUM),
            update_only_genre=bool(UPDATE_ONLY_GENRE),
            only_missing_album=bool(ONLY_MISSING_ALBUM),
            print_search_info=bool(PRINT_SEARCH_INFO),
            search_candidate_limit=max(1, int(SEARCH_CANDIDATE_LIMIT)),
            scrape_album_details=bool(SCRAPE_ALBUM_DETAILS),
            market=str(MARKET).strip() if MARKET else None,
            request_timeout=max(1, int(REQUEST_TIMEOUT)),
            spotify_max_limit=max(1, min(50, int(SPOTIFY_MAX_LIMIT))),
            max_concurrent_requests=max(1, int(MAX_CONCURRENT_REQUESTS)),
            max_http_retries=max(1, int(MAX_HTTP_RETRIES)),
        )
