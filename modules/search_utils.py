"""
modules/search_utils.py
Text normalization, remix token extraction, and Spotify query construction utilities.

Why this module exists:
Local audio tags and filenames frequently contain noisy annotations (`(feat. X)`,
`[Official Video]`, diacritics, inconsistent punctuation) that degrade Spotify Search API
recall. Centralizing pre-compiled regex normalization here keeps `spotify_client.py`
focused purely on HTTP transport and candidate scoring.
"""

from __future__ import annotations

import re
import unicodedata

# Pre-compiled regular expressions to avoid re-compilation inside candidate scoring loops.
_FEAT_TOKEN_RE: re.Pattern[str] = re.compile(r"\b(feat\.?|ft\.?)\b", flags=re.IGNORECASE)
_PAREN_GROUP_RE: re.Pattern[str] = re.compile(r"\(([^)]*)\)")
_BRACKET_GROUP_RE: re.Pattern[str] = re.compile(r"\[([^\]]*)\]")
_PAREN_REMIX_RE: re.Pattern[str] = re.compile(r"\(([^)]*remix[^)]*)\)", flags=re.IGNORECASE)
_BRACKET_REMIX_RE: re.Pattern[str] = re.compile(r"\[([^\]]*remix[^\]]*)\]", flags=re.IGNORECASE)
_REMIX_WORD_RE: re.Pattern[str] = re.compile(r"\bremix\b", flags=re.IGNORECASE)
_PARENS_RE: re.Pattern[str] = re.compile(r"[()]+")
_NON_ALNUM_RE: re.Pattern[str] = re.compile(r"[^0-9a-zA-Z\s]")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")
_ARTIST_SEP_RE: re.Pattern[str] = re.compile(r"[,\\/]+")


def _strip_accents(text: str) -> str:
    """
    Decomposes Unicode characters via NFKD and strips combining diacritical marks.
    Why: Ensures accent-insensitive token comparison between local files and Spotify catalog entries.
    """
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def strip_parentheses_with_feat(text: str | None) -> str:
    """
    Removes parenthesized or bracketed clauses containing `feat`/`ft` credits.

    Why: Spotify stores featured artists in the track's `artists` array rather than
    inside the track title string; leaving `(feat. X)` in a fielded `track:` query
    causes Spotify's search index to miss canonical track entries.
    """
    if not text:
        return ""

    def _replace_if_feat(match: re.Match[str]) -> str:
        inner = match.group(1)
        if _FEAT_TOKEN_RE.search(inner):
            return " "
        return match.group(0)

    cleaned = _PAREN_GROUP_RE.sub(_replace_if_feat, text)
    cleaned = _BRACKET_GROUP_RE.sub(_replace_if_feat, cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def extract_remixer_tokens_from_title(title: str | None) -> list[str]:
    """
    Extracts normalized artist/remixer tokens from parenthesized or bracketed remix clauses.

    Why: On Spotify, remixer names often appear in the candidate's `artists` array in
    addition to (or instead of) the title. Extracting remixer tokens allows the scoring
    heuristic to reward candidates whose artist list matches the remixer.
    """
    if not title:
        return []

    extracted: list[str] = []
    for pattern in (_PAREN_REMIX_RE, _BRACKET_REMIX_RE):
        for match in pattern.finditer(title):
            inner = match.group(1)
            without_remix_keyword = _REMIX_WORD_RE.sub(" ", inner)
            alnum_only = _NON_ALNUM_RE.sub(" ", _strip_accents(without_remix_keyword))
            cleaned = _WHITESPACE_RE.sub(" ", alnum_only).strip().lower()
            if cleaned:
                extracted.extend(tok for tok in cleaned.split() if tok)

    return extracted


def normalize_text_basic(text: str | None) -> str:
    """
    Normalizes an arbitrary string to lowercase ASCII alphanumeric space-separated tokens,
    stripping `feat`/`ft` markers and punctuation.

    Why: Produces a canonical representation so lexical token set intersections are
    resilient to punctuation, casing, and Unicode diacritic variations.
    """
    if not text:
        return ""
    cleaned = _strip_accents(text)
    cleaned = _PARENS_RE.sub(" ", cleaned)
    cleaned = _FEAT_TOKEN_RE.sub(" ", cleaned)
    cleaned = _NON_ALNUM_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip().lower()


def normalize_artist_for_search(artist: str | None) -> str:
    """
    Normalizes an artist string by splitting multi-artist delimiters (`,`, `/`, `\\`)
    and stripping featured credits before applying basic lexical normalization.

    Why: Local tags often concatenate collaborating artists with slashes or commas
    (e.g., `"Artist A / Artist B"`), which must be converted to whitespace before tokenization.
    """
    if not artist:
        return ""
    separated = _ARTIST_SEP_RE.sub(" ", artist)
    stripped = strip_parentheses_with_feat(separated)
    return normalize_text_basic(stripped)


def normalize_title_for_search(title: str | None) -> str:
    """
    Normalizes a track or album title by removing featured-artist clauses first,
    then applying ASCII alphanumeric normalization.

    Why: Preserves remix or version modifiers inside parentheses while discarding
    `(feat. ...)` noise prior to tokenization.
    """
    if not title:
        return ""
    stripped = strip_parentheses_with_feat(title)
    return normalize_text_basic(stripped)


def tokens(normalized_text: str) -> list[str]:
    """
    Splits a normalized string into non-empty lexical tokens.

    Why: Provides a consistent list of tokens for candidate intersection scoring.
    """
    if not normalized_text:
        return []
    return [tok for tok in normalized_text.split() if tok]


def tokens_in_candidate(query_tokens: list[str], candidate_norm: str) -> bool:
    """
    Checks whether every token in `query_tokens` is present in `candidate_norm`.

    Why: Used for strict subset verification when comparing query tokens against
    normalized candidate metadata.
    """
    if not query_tokens:
        return True
    candidate_set = set(candidate_norm.split())
    return all(tok in candidate_set for tok in query_tokens)


def build_sanitized_query(
    n_artist: str,
    n_title: str,
    n_album: str,
    fielded: bool = True,
) -> str:
    """
    Constructs a Spotify Search API query string (`q` parameter), either using field
    filters (`track:... artist:... album:...`) or plain free-text concatenation.

    Why: Fielded queries maximize precision on clean tags, whereas plain free-text
    queries act as a high-recall fallback when tokens span across fields (e.g., remixes
    or collaborations embedded in titles).
    """

    def _sanitize(value: str) -> str:
        return _WHITESPACE_RE.sub(" ", value.replace('"', " ")).strip()

    clean_artist = _sanitize(n_artist) if n_artist else ""
    clean_title = _sanitize(n_title) if n_title else ""
    clean_album = _sanitize(n_album) if n_album else ""

    if fielded and (clean_artist or clean_title or clean_album):
        parts: list[str] = []
        if clean_title:
            parts.append(f"track:{clean_title}")
        if clean_artist:
            parts.append(f"artist:{clean_artist}")
        if clean_album:
            parts.append(f"album:{clean_album}")
        return " ".join(parts)

    plain_parts = [p for p in (clean_artist, clean_title, clean_album) if p]
    return " ".join(plain_parts)


# Backward-compatible aliases for legacy callers importing underscore-prefixed names.
_strip_parentheses_with_feat = strip_parentheses_with_feat
_extract_remixer_tokens_from_title = extract_remixer_tokens_from_title
_normalize_text_basic = normalize_text_basic
_normalize_artist_for_search = normalize_artist_for_search
_normalize_title_for_search = normalize_title_for_search
_tokens = tokens
_tokens_in_candidate = tokens_in_candidate
_build_sanitized_query = build_sanitized_query
