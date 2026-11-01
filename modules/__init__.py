"""
modules/__init__.py
Public package interface for the Music Metadata Handler modules.

Why this module exists:
Exposes the core domain models, pipeline stages, Selenium scraper client,
filename utilities, and search normalization helpers at the package root.
"""

from .core import (
    LocalTrackInfo,
    PendingTagUpdate,
    ResolvedMetadata,
    get_creation_time,
    iter_audio_files,
)
from .filename_utils import (
    infer_artist_title_from_filename,
    send_original_to_trash,
    unique_temp_copy,
)
from .processor import (
    commit_tags_to_file,
    extract_local_tags,
    fetch_spotify_data_async,
    map_spotify_match_to_metadata,
    match_track_via_browser,
)
from .search_utils import (
    _build_sanitized_query,
    _extract_remixer_tokens_from_title,
    _normalize_artist_for_search,
    _normalize_text_basic,
    _normalize_title_for_search,
    _strip_parentheses_with_feat,
    _tokens,
    _tokens_in_candidate,
    build_sanitized_query,
    extract_remixer_tokens_from_title,
    normalize_artist_for_search,
    normalize_text_basic,
    normalize_title_for_search,
    strip_parentheses_with_feat,
    tokens,
    tokens_in_candidate,
)
from .scanner import (
    KNOWN_CRITERIA,
    has_embedded_artwork,
    inspect_audio_file,
    matches_criteria,
    scan_audio_library,
)
from .spotify_client import (
    SpotifyClient,
    SpotifyWebScraper,
    select_primary_genre,
    upgrade_spotify_image_url,
)

__all__ = [
    "LocalTrackInfo",
    "PendingTagUpdate",
    "ResolvedMetadata",
    "SpotifyClient",
    "SpotifyWebScraper",
    "build_sanitized_query",
    "commit_tags_to_file",
    "extract_local_tags",
    "extract_remixer_tokens_from_title",
    "fetch_spotify_data_async",
    "get_creation_time",
    "infer_artist_title_from_filename",
    "iter_audio_files",
    "map_spotify_match_to_metadata",
    "match_track_via_browser",
    "normalize_artist_for_search",
    "normalize_text_basic",
    "normalize_title_for_search",
    "select_primary_genre",
    "send_original_to_trash",
    "strip_parentheses_with_feat",
    "tokens",
    "tokens_in_candidate",
    "unique_temp_copy",
    "upgrade_spotify_image_url",
]
