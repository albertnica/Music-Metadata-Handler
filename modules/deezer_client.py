"""
modules/deezer_client.py
Functions to query the Deezer API. (No OAuth required for basic search)
"""

from typing import Optional, List
import requests
import logging
import re

from config import DEEZER_SEARCH_URL, REQUEST_TIMEOUT, SEARCH_CANDIDATE_LIMIT, PRINT_SEARCH_INFO

# Reuse normalization helpers from search_utils
from modules.search_utils import (
    _normalize_artist_for_search,
    _normalize_title_for_search,
    _tokens,
    _tokens_in_candidate,
    _normalize_text_basic
)

def deezer_search(q: str, limit: int = 20, offset: int = 0) -> Optional[dict]:
    """
    Sends a GET request to the Deezer search endpoint.
    """
    params = {"q": q, "limit": limit, "index": offset}
    try:
        r = requests.get(DEEZER_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        logging.error("Deezer request failed: %s", e)
        return None
    if not r.ok:
        logging.error("Deezer request error: %s %s", r.status_code, r.text)
        return None
    try:
        return r.json()
    except Exception:
        return None

def deezer_find_best_match(artist: Optional[str], album: Optional[str], title: Optional[str],
                           combined_limit: int = None) -> Optional[dict]:
    """
    Find the best matching Deezer item for the given artist/album/title.
    Tries multiple query strategies (fielded, plain) and artist variants.
    """
    if combined_limit is None:
        combined_limit = SEARCH_CANDIDATE_LIMIT

    n_artist = _normalize_artist_for_search(artist) if artist else ""
    n_title = _normalize_title_for_search(title) if title else ""
    n_album = _normalize_title_for_search(album) if album else ""

    artist_tokens = _tokens(n_artist)
    title_tokens = _tokens(n_title)
    album_tokens = _tokens(n_album)

    # Build Deezer query variants
    # 1. Full fielded
    # 2. Artist + Title fielded
    # 3. Title fielded
    # 4. Plain search
    
    query_variants = []
    
    # Try with full artist string
    if n_artist and n_title:
        query_variants.append(f'artist:"{n_artist}" track:"{n_title}"')
    
    # Try with first artist only if multiple artists present
    first_artist = ""
    if artist:
        # Split on common separators: , ; &
        parts = re.split(r'[,;&]', artist)
        if len(parts) > 1:
            first_artist = _normalize_artist_for_search(parts[0])
            if first_artist and n_title:
                query_variants.append(f'artist:"{first_artist}" track:"{n_title}"')

    if n_title:
        query_variants.append(f'track:"{n_title}"')
        
    if n_artist and n_title:
        query_variants.append(f"{n_artist} {n_title}")
    elif n_title:
        query_variants.append(n_title)

    seen_ids = set()
    overall_idx = 0

    for q in query_variants:
        if PRINT_SEARCH_INFO:
            logging.info("Deezer Query: '%s' | target=%d", q, combined_limit)
            
        offset = 0
        while True:
            current_limit = min(50, combined_limit - overall_idx)
            if current_limit <= 0 or overall_idx >= combined_limit:
                break
            
            res = deezer_search(q, limit=current_limit, offset=offset)
            if not res or "data" not in res:
                break
                
            items = res["data"]
            if not items:
                break
                
            for it in items:
                it_id = it.get("id")
                if it_id in seen_ids:
                    continue
                seen_ids.add(it_id)
                overall_idx += 1
                
                # Check match
                cand_title = _normalize_text_basic(it.get("title"))
                cand_artist = _normalize_text_basic(it.get("artist", {}).get("name"))
                cand_album = _normalize_text_basic(it.get("album", {}).get("title"))
                
                if PRINT_SEARCH_INFO:
                    logging.info("Candidate #%d: title='%s' | artist='%s' | album='%s'", overall_idx, cand_title, cand_artist, cand_album)

                title_ok = _tokens_in_candidate(title_tokens, cand_title)
                # Artist check:                title_ok = _tokens_in_candidate(title_tokens, cand_title)
                artist_ok = True
                if artist_tokens:
                    artist_ok = _tokens_in_candidate(artist_tokens, cand_artist) or \
                                (first_artist and _tokens_in_candidate(_tokens(first_artist), cand_artist))
                
                album_ok = True
                if album_tokens:
                    # Lenient album check: if most tokens match, or if it's a soundtrack and keywords match
                    album_ok = _tokens_in_candidate(album_tokens, cand_album)
                    if not album_ok:
                        # Fallback: if artist and title are solid, we allow album mismatch
                        # especially if they share major keywords like 'concrete' and 'jungle'
                        common = set(album_tokens) & set(cand_album.split())
                        if len(common) >= 2 or (len(album_tokens) <= 2 and len(common) >= 1):
                            album_ok = True

                accepted = bool(title_ok and artist_ok and album_ok)
                if PRINT_SEARCH_INFO:
                    logging.info("ACCEPTED" if accepted else "REJECTED")
                    
                if accepted:
                    return it
                
                if overall_idx >= combined_limit:
                    break
            
            if len(items) < current_limit:
                break
            offset += current_limit
            
    return None
