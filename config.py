"""
config.py
Centralized configuration repository for the Music Metadata Handler.
Centralizes environment flags, API endpoints, and runtime constants 
to ensure consistent behavior across all modules.
"""

from pathlib import Path
import logging

# Path resolution for local resources
MAIN_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = MAIN_DIR / "credentials.json"

# File iteration / processing
RECURSIVE = False   # Scans subdirectories if search depth is required
PROCESS_TOP_X = 1   # Limit to avoid hitting API rate limits or processing huge libraries at once

# Filename parsing mode:
# 0 = "Artist - Title"  (default, left=artist, right=title)
# 1 = "Title - Artist"  (left=title, right=artist)
# Reason: Provides a fallback mechanism when audio tags are missing but 
# filename structure is consistent.
FILENAME_PARSE_MODE = 0

# Behavior flags
OVERWRITE_TITLE_ARTIST_OR_ALBUM = 1   # Set to 1 to replace existing core metadata with Spotify data
UPDATE_ONLY_GENRE = 0                 # Set to 1 for a 'safe' run that only modifies genre tags
PRINT_SEARCH_INFO = 0                 # Enable for verbose debugging of the matching heuristic
SEARCH_CANDIDATE_LIMIT = 5            # Number of Spotify items to analyze per local file
MARKET = None                         # Geographic market code (e.g. "US") to filter results

# Spotify API endpoints
SPOTIFY_TOKEN_URL =          "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL =         "https://api.spotify.com/v1/search"
SPOTIFY_ARTIST_URL =         "https://api.spotify.com/v1/artists/{}"
SPOTIFY_ARTIST_ALBUMS_URL =  "https://api.spotify.com/v1/artists/{}/albums"
SPOTIFY_ALBUM_TRACKS_URL =   "https://api.spotify.com/v1/albums/{}/tracks"

# Network performance and limits
REQUEST_TIMEOUT = 12
SPOTIFY_MAX_LIMIT = 50
