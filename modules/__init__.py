"""
modules.__init__.py
Package initialization for modules. Export commonly used helpers to simplify imports.
"""

# Expose the most used modules/functions to simplify imports
from .filename_utils import infer_artist_title_from_filename, unique_temp_copy, send_original_to_trash
from .search_utils import (
    _strip_parentheses_with_feat,
    _extract_remixer_tokens_from_title,
    _normalize_text_basic,
    _normalize_artist_for_search,
    _normalize_title_for_search,
    _tokens,
    _tokens_in_candidate,
    _build_sanitized_query
)
# Expose processor entrypoint and wav utils if available
try:
    from .processor import process_single_file
except Exception:
    pass

try:
    from .wav_utils import finalize_wav_with_metadata
except Exception:
    pass

# Keep __all__ minimal (optional)
__all__ = [
    "infer_artist_title_from_filename",
    "unique_temp_copy",
    "send_original_to_trash",
    "process_single_file",
    "finalize_wav_with_metadata"
]
