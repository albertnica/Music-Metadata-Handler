"""
modules/search_utils.py
Text normalization utilities, remix token extraction, and query construction.
"""

from typing import Optional, List
import re
import unicodedata

def _strip_parentheses_with_feat(s: Optional[str]) -> str:
    """
    Remove parenthesized or bracketed substrings that contain "feat" (or "ft").
    """
    if not s:
        return ""

    def repl(m):
        inner = m.group(1)
        if re.search(r"\b(feat\.?|ft\.?)\b", inner, flags=re.IGNORECASE):
            return " "
        return m.group(0)

    s = re.sub(r"\(([^)]*)\)", repl, s)
    s = re.sub(r"\[([^]]*)\]", repl, s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_remixer_tokens_from_title(s: Optional[str]) -> List[str]:
    """
    Extract normalized tokens from remix annotations in a title string.
    For example "Song Title (Some DJ Remix)" or "Song Title [Another Remix]". This
    helper extracts meaningful tokens from those remix annotations so they can
    be considered when matching search candidates (e.g., 'some', 'dj').
    """
    if not s:
        return []
    res: List[str] = []

    def _extract_from_matches(pattern):
        for m in re.finditer(pattern, s, flags=re.IGNORECASE):
            inner = m.group(1)
            name = re.sub(r"\bremix\b", " ", inner, flags=re.IGNORECASE)
            name = re.sub(r"[^0-9a-zA-Z\s]", " ", name)
            name = unicodedata.normalize("NFKD", name)
            name = "".join(ch for ch in name if not unicodedata.combining(ch))
            name = re.sub(r"\s+", " ", name).strip().lower()
            if name:
                res.extend([t for t in name.split() if t])

    _extract_from_matches(r"\(([^)]*remix[^)]*)\)")
    _extract_from_matches(r"\[([^]]*remix[^]]*)\]")
    return res


def _normalize_text_basic(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[()]+", " ", s)
    s = re.sub(r"\b(feat\.?|ft\.?)\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[^0-9a-zA-Z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _normalize_artist_for_search(s: Optional[str]) -> str:
    if not s:
        return ""
    s2 = s.replace(",", " ").replace("\\", " ").replace("/", " ")
    s2 = _strip_parentheses_with_feat(s2)
    return _normalize_text_basic(s2)


def _normalize_title_for_search(s: Optional[str]) -> str:
    if not s:
        return ""
    s2 = _strip_parentheses_with_feat(s)
    return _normalize_text_basic(s2)


def _tokens(n: str) -> List[str]:
    if not n:
        return []
    return [t for t in n.split() if t]


def _tokens_in_candidate(tokens: List[str], candidate_norm: str) -> bool:
    if not tokens:
        return True
    cand_set = set(candidate_norm.split())
    return all(tok in cand_set for tok in tokens)


def _build_sanitized_query(n_artist: str, n_title: str, n_album: str, fielded: bool = True) -> str:
    def quote_and_escape(s: str) -> str:
        s2 = s.replace('"', ' ')
        s2 = re.sub(r'\s+', ' ', s2).strip()
        return f'"{s2}"' if s2 else ''
    if fielded and (n_artist or n_title or n_album):
        parts = []
        if n_title:
            parts.append(f'track:{quote_and_escape(n_title)}')
        if n_artist:
            parts.append(f'artist:{quote_and_escape(n_artist)}')
        if n_album:
            parts.append(f'album:{quote_and_escape(n_album)}')
        return " ".join([p for p in parts if p])
    parts = []
    if n_artist:
        parts.append(n_artist)
    if n_title:
        parts.append(n_title)
    if n_album:
        parts.append(n_album)
    return " ".join(parts) if parts else '""'
