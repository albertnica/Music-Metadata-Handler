"""
modules/spotify_client.py
Gateway for interaction with the Spotify Web API.
We centralize search and metadata retrieval logic here to decouple 
the primary processing loop from HTTP request complexities.
Now re-architected to use async I/O.
"""

from typing import Optional, Tuple
import asyncio
import aiohttp
import logging

from config import *

# Reuse normalization helpers from search_utils
from modules.search_utils import (
    _normalize_artist_for_search,
    _normalize_title_for_search,
    _extract_remixer_tokens_from_title,
    _tokens,
    _tokens_in_candidate,
    _build_sanitized_query,
    _normalize_text_basic
)


async def spotifysearch(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, token: str, q: str, type_: str = "track", limit: int = 20, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    Executes a search query against Spotify's catalog asynchronously.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": q, "type": type_, "limit": limit, "offset": offset}
    if market:
        params["market"] = market
        
    for attempt in range(4):
        try:
            async with semaphore:
                async with session.get(SPOTIFY_SEARCH_URL, headers=headers, params=params) as r:
                    if r.status in (401, 400):
                        logging.debug("Spotify API rejected the request (%d).", r.status)
                        return {"error": {"status": r.status}}
                    if r.status == 429:
                        wait_sec = int(r.headers.get("Retry-After", 2))
                        logging.warning("Spotify Rate Limit 429! Waiting %d seconds...", wait_sec)
                        await asyncio.sleep(wait_sec + 0.5)
                        continue
                    if not r.ok:
                        return {"error": {"status": r.status}}
                    return await r.json()
        except Exception:
            await asyncio.sleep(1)
            
    return None


async def spotify_get_artist_albums(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, token: str, artist_id: str, limit: int = SPOTIFY_MAX_LIMIT, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    Retrieves all albums associated with a specific artist asynchronously.
    Reason: Used in fallback search scenarios where direct track search 
    failed but we have a confirmed artist identity.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": limit, "offset": offset}
    if market:
        params["market"] = market
        
    try:
        async with semaphore:
            async with session.get(SPOTIFY_ARTIST_ALBUMS_URL.format(artist_id), headers=headers, params=params) as r:
                if r.status == 200:
                    return await r.json()
                return None
    except Exception:
        return None


async def spotify_get_album_tracks(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, token: str, album_id: str, limit: int = SPOTIFY_MAX_LIMIT, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    Enumerates tracks within a specific album asynchronously.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": limit, "offset": offset}
    if market:
        params["market"] = market
        
    try:
        async with semaphore:
            async with session.get(SPOTIFY_ALBUM_TRACKS_URL.format(album_id), headers=headers, params=params) as r:
                if r.status == 200:
                    return await r.json()
                return None
    except Exception:
        return None



def _debug_log(messages: list[str]):
    if not messages:
        return
    from config import MAIN_DIR
    log_path = MAIN_DIR / "spotify_search_debug.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(messages) + "\n" + "-"*50 + "\n")
    except:
        pass


def _score_candidate(artist_tokens, title_tokens, cand_artist_norm, cand_title, remixer_tokens=None) -> float:
    cand_art_toks = set(cand_artist_norm.split())
    cand_tit_toks = set(cand_title.split())
    
    local_art_toks = set(artist_tokens)
    local_tit_toks = set(title_tokens)
    
    title_matches = len(local_tit_toks.intersection(cand_tit_toks))
    artist_matches = len(local_art_toks.intersection(cand_art_toks))
    
    remixer_matches = 0
    if remixer_tokens:
        remixer_matches = len(set(remixer_tokens).intersection(cand_art_toks))
        artist_matches += remixer_matches * 1.5

    artist_penalty = len(cand_art_toks - local_art_toks) * 0.2
    title_penalty = len(cand_tit_toks - local_tit_toks) * 0.1
    missed_title = len(local_tit_toks - cand_tit_toks) * 0.5
    missed_artist = len(local_art_toks - cand_art_toks) * 0.5

    score = (title_matches * 2.0) + (artist_matches * 1.5) - artist_penalty - title_penalty - missed_title - missed_artist
    
    valid = (title_matches > 0)
    if local_art_toks and artist_matches == 0 and remixer_matches == 0:
        valid = False

    return score if valid else -999.0


async def spotify_find_best_match(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, token: str, artist: Optional[str], album: Optional[str], title: Optional[str], combined_limit: int = None) -> Optional[dict]:
    """
    Implements a robust scoring heuristic to find the most accurate 
    Spotify metadata match, penalizing anomalies and accepting slight differences.
    """
    if combined_limit is None:
        combined_limit = SEARCH_CANDIDATE_LIMIT

    n_artist = _normalize_artist_for_search(artist) if artist else ""
    n_title = _normalize_title_for_search(title) if title else ""
    n_album = _normalize_title_for_search(album) if album else ""

    artist_tokens = _tokens(n_artist)
    title_tokens = _tokens(n_title)
    remixer_tokens = _extract_remixer_tokens_from_title(title or "")

    queries = []
    primary_q_fielded = _build_sanitized_query(n_artist, n_title, n_album, fielded=True)
    if primary_q_fielded:
        queries.append(("track", primary_q_fielded))
    at_q_fielded = _build_sanitized_query(n_artist, n_title, "", fielded=True)
    if at_q_fielded and at_q_fielded != primary_q_fielded:
        queries.append(("track", at_q_fielded))
    t_q_fielded = _build_sanitized_query("", n_title, "", fielded=True)
    if t_q_fielded and t_q_fielded not in (primary_q_fielded, at_q_fielded):
        queries.append(("track", t_q_fielded))
    primary_q_plain = _build_sanitized_query(n_artist, n_title, n_album, fielded=False)
    if primary_q_plain and primary_q_plain not in (q for _, q in queries):
        queries.append(("track", primary_q_plain))

    seen_keys = set()
    overall_idx = 0
    best_match = None
    best_score = -999.0
    
    log_buffer = [f"[{title} | {artist}]", f"  Tokens -> Title: {title_tokens}, Artist: {artist_tokens}"]

    for (kind, q) in queries:
        log_buffer.append(f"  -> QUERY: '{q}' ({kind})")
        offset = 0
        while True:
            per_request = min(SPOTIFY_MAX_LIMIT, combined_limit - overall_idx)
            if per_request <= 0:
                break
            
            j = await spotifysearch(session, semaphore, token, q, type_=kind, limit=per_request, offset=offset, market=MARKET)
            if not j or "error" in j:
                log_buffer.append(f"     [!] API Error/Expired: {j.get('error') if j else 'None'}")
                break
                
            items = j.get("tracks", {}).get("items", [])
            if not isinstance(items, list) or not items:
                log_buffer.append(f"     [?] 0 items returned")
                break
                
            for it in items:
                it_id = it.get("id")
                if it_id:
                    key = f"id:{it_id}"
                else:
                    cand_title = _normalize_text_basic(it.get("name"))
                    cand_artists = " ".join(a.get("name", "") for a in it.get("artists", []))
                    cand_artist_norm = _normalize_artist_for_search(cand_artists)
                    key = f"key:{cand_title}|{cand_artist_norm}"
                    
                if key in seen_keys:
                    continue
                    
                overall_idx += 1
                seen_keys.add(key)
                
                cand_title = _normalize_text_basic(it.get("name"))
                cand_artists = " ".join(a.get("name", "") for a in it.get("artists", []))
                cand_artist_norm = _normalize_artist_for_search(cand_artists)
                
                score = _score_candidate(artist_tokens, title_tokens, cand_artist_norm, cand_title, remixer_tokens)
                log_buffer.append(f"     Candidate {overall_idx}: '{it.get('name')}' by '{cand_artists}' -> SCORE: {score:.2f}")
                
                if score > best_score and score > 0:
                    best_score = score
                    best_match = it
                    
                if overall_idx >= combined_limit:
                    break
                    
            if overall_idx >= combined_limit:
                break
            offset += per_request
            if len(items) < per_request:
                break
        
        if best_match and best_score >= 2.0:
            log_buffer.append(f"  MATCH FOUND: '{best_match.get('name')}' Score: {best_score:.2f}")
            _debug_log(log_buffer)
            return best_match
            
        if overall_idx >= combined_limit:
            break

    if best_match:
        log_buffer.append(f"  MATCH FOUND (Fallback): '{best_match.get('name')}' Score: {best_score:.2f}")
    else:
        log_buffer.append("  NO MATCH FOUND.")
        
    _debug_log(log_buffer)
    return best_match
