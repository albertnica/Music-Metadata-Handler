"""
config.py
Central configuration and flags for the metadata updater.
"""

from pathlib import Path
import logging

MAIN_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = MAIN_DIR / "credentials.json"

# File iteration / processing
RECURSIVE = False   # True = also apply to files in subdirectories
PROCESS_TOP_X = 50   # (int) number of files to process in this run

# Filename parsing mode:
# 0 = "Artist - Title"  (default, left=artist, right=title)
# 1 = "Title - Artist"  (left=title, right=artist)
# Applies to FLAC, MP3 and WAV filename fallback parsing.
FILENAME_PARSE_MODE = 0

# Spotify endpoints
SPOTIFY_TOKEN_URL =          "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL =         "https://api.spotify.com/v1/search"
SPOTIFY_ARTIST_URL =         "https://api.spotify.com/v1/artists/{}"
SPOTIFY_ARTIST_ALBUMS_URL =  "https://api.spotify.com/v1/artists/{}/albums"
SPOTIFY_ALBUM_TRACKS_URL =   "https://api.spotify.com/v1/albums/{}/tracks"

# Timeouts and limits
REQUEST_TIMEOUT = 12
SPOTIFY_MAX_LIMIT = 50

# Behavior flags
OVERWRITE_TITLE_ARTIST_OR_ALBUM = 1   # 0 = preserve title/artist/album, 1 = overwrite
UPDATE_ONLY_GENRE = 0                 # 1 = only update genre
PRINT_SEARCH_INFO = 0                 # 1 = extended logs
SEARCH_CANDIDATE_LIMIT = 5            # number of spotify tracks to search per music file
MARKET = None                         # set e.g. "US" or "ES" to restrict results

# Logging configuration (basic)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
