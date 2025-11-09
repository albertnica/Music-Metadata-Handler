"""
modules/spotify_client.py
Functions to obtain tokens and query the Spotify Web API.
"""

from typing import Optional, Tuple
import base64
import time
import requests
import logging

from config import (
    SPOTIFY_TOKEN_URL,
    SPOTIFY_SEARCH_URL,
    SPOTIFY_ARTIST_URL,
    SPOTIFY_ARTIST_ALBUMS_URL,
    SPOTIFY_ALBUM_TRACKS_URL,
    REQUEST_TIMEOUT,
    SPOTIFY_MAX_LIMIT,
    MARKET,
    SEARCH_CANDIDATE_LIMIT,
    PRINT_SEARCH_INFO,
)

# Reuse normalization helpers from search_utils
try:
    from modules.search_utils import (
        _normalize_artist_for_search,
        _normalize_title_for_search,
        _extract_remixer_tokens_from_title,
        _tokens,
        _tokens_in_candidate,
        _build_sanitized_query,
        _normalize_text_basic
    )
except Exception:
    # If the modules package is not set up, try direct import for local testing
    try:
        from search_utils import (
            _normalize_artist_for_search,
            _normalize_title_for_search,
            _extract_remixer_tokens_from_title,
            _tokens,
            _tokens_in_candidate,
            _build_sanitized_query,
            _normalize_text_basic
        )
    except Exception:
        # Provide minimal fallbacks to avoid import-time crashes; real behavior requires search_utils
        def _normalize_artist_for_search(s): return s or ""
        def _normalize_title_for_search(s): return s or ""
        def _extract_remixer_tokens_from_title(s): return []
        def _tokens(n): return []
        def _tokens_in_candidate(tokens, candidate_norm): return True
        def _build_sanitized_query(a, t, al, fielded=True): return '""'
        def _normalize_text_basic(s): return s or ""


def get_spotify_token(client_id: str, client_secret: str, ttl_margin: int = 5) -> Tuple[str, int]:
    """
    Obtain an OAuth access token from Spotify using the Client Credentials flow.
    """
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {auth}"}
    data = {"grant_type": "client_credentials"}
    resp = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    j = resp.json()
    token = j["access_token"]
    expires_in = int(j.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in - ttl_margin
    return token, expires_at


def spotifysearch(token: str, q: str, type_: str = "track", limit: int = 20, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    This wrapper sends a GET request to the configured search endpoint and
    returns the parsed JSON response on success or `None` on error. It handles
    network errors and basic 401/unauthorized checks.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": q, "type": type_, "limit": limit, "offset": offset}
    if market:
        params["market"] = market
    try:
        r = requests.get(SPOTIFY_SEARCH_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None
    if r.status_code == 401 or not r.ok:
        return None
    try:
        return r.json()
    except Exception:
        return None


def spotify_get_artist_albums(token: str, artist_id: str, limit: int = SPOTIFY_MAX_LIMIT, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    Retrieve albums for a given artist using Spotify's artist albums endpoint.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": limit, "offset": offset}
    if market:
        params["market"] = market
    try:
        r = requests.get(SPOTIFY_ARTIST_ALBUMS_URL.format(artist_id), headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        return r.json()
    except Exception:
        return None


def spotify_get_album_tracks(token: str, album_id: str, limit: int = SPOTIFY_MAX_LIMIT, offset: int = 0, market: Optional[str] = None) -> Optional[dict]:
    """
    Retrieve tracks for a given album using Spotify's album tracks endpoint.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": limit, "offset": offset}
    if market:
        params["market"] = market
    try:
        r = requests.get(SPOTIFY_ALBUM_TRACKS_URL.format(album_id), headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None
    if not r.ok:
        return None
    try:
        return r.json()
    except Exception:
        return None


def spotify_find_best_match(token: str, artist: Optional[str], album: Optional[str], title: Optional[str],
                            combined_limit: int = None) -> Optional[dict]:
    """
    Find the best matching Spotify item for the given artist/album/title inputs.

    The algorithm:
    - Build a set of search queries (fielded queries first, then plain text).
    - For each query, page through Spotify results up to `combined_limit`.
    - Normalize candidate titles/artists/albums and apply token containment checks.
    - Return the first accepted candidate (track or album depending on the query).
    """
    if combined_limit is None:
        combined_limit = SEARCH_CANDIDATE_LIMIT

    n_artist = _normalize_artist_for_search(artist) if artist else ""
    n_title = _normalize_title_for_search(title) if title else ""
    n_album = _normalize_title_for_search(album) if album else ""

    artist_tokens = _tokens(n_artist)
    title_tokens = _tokens(n_title)
    album_tokens = _tokens(n_album)
    remixer_tokens = _extract_remixer_tokens_from_title(title or "")

    if PRINT_SEARCH_INFO:
        logging.info("Sanitized search input: artist='%s' | title='%s' | album='%s'", n_artist, n_title, n_album)

    # Build queries: fielded first, then plain
    queries = []
    primary_q_fielded = _build_sanitized_query(n_artist, n_title, n_album, fielded=True)
    if primary_q_fielded:
        queries.append(("track", primary_q_fielded))
    at_q_fielded = _build_sanitized_query(n_artist, n_title, "", fielded=True)
    if at_q_fielded and at_q_fielded != primary_q_fielded:
        queries.append(("track", at_q_fielded))
    aa_q_fielded = _build_sanitized_query(n_artist, "", n_album, fielded=True)
    if aa_q_fielded and aa_q_fielded not in (primary_q_fielded, at_q_fielded):
        queries.append(("album", aa_q_fielded))
    t_q_fielded = _build_sanitized_query("", n_title, "", fielded=True)
    if t_q_fielded and t_q_fielded not in (primary_q_fielded, at_q_fielded, aa_q_fielded):
        queries.append(("track", t_q_fielded))
    a_q_fielded = _build_sanitized_query("", "", n_album, fielded=True)
    if a_q_fielded and a_q_fielded not in (primary_q_fielded, at_q_fielded, aa_q_fielded, t_q_fielded):
        queries.append(("album", a_q_fielded))

    primary_q_plain = _build_sanitized_query(n_artist, n_title, n_album, fielded=False)
    if primary_q_plain and primary_q_plain not in (q for _, q in queries):
        queries.append(("track", primary_q_plain))

    seen_keys = set()
    overall_idx = 0

    for (kind, q) in queries:
        if PRINT_SEARCH_INFO:
            logging.info("Query base: '%s' | type=%s | target=%d", q, kind, combined_limit)
        offset = 0
        while True:
            per_request = min(SPOTIFY_MAX_LIMIT, combined_limit - overall_idx)
            if per_request <= 0:
                break
            if PRINT_SEARCH_INFO:
                logging.info("Searching Spotify: q='%s' type=%s limit=%d offset=%d market=%s", q, kind, per_request, offset, MARKET)
            j = spotifysearch(token, q, type_=kind, limit=per_request, offset=offset, market=MARKET)
            if not j:
                break
            items = j.get((kind + "s") if kind in ("album", "track") else "tracks", {}).get("items", [])
            if not isinstance(items, list) or not items:
                break
            for it in items:
                it_id = it.get("id")
                if it_id:
                    key = f"id:{it_id}"
                else:
                    cand_title = _normalize_text_basic(it.get("name"))
                    cand_artists = " ".join(a.get("name", "") for a in it.get("artists", []))
                    cand_artist_norm = _normalize_artist_for_search(cand_artists)
                    album_info = (it.get("album") or {}) if kind == "track" else it
                    cand_album_name = _normalize_title_for_search((album_info.get("name") or ""))
                    key = f"key:{cand_title}|{cand_artist_norm}|{cand_album_name}"
                if key in seen_keys:
                    continue
                overall_idx += 1
                seen_keys.add(key)
                if kind == "track":
                    cand_title = _normalize_text_basic(it.get("name"))
                    cand_artists = " ".join(a.get("name", "") for a in it.get("artists", []))
                    cand_artist_norm = _normalize_artist_for_search(cand_artists)
                    album_info = it.get("album", {}) or {}
                    cand_album_name = _normalize_title_for_search(album_info.get("name"))
                else:
                    cand_title = _normalize_text_basic(it.get("name"))
                    cand_artist_norm = _normalize_text_basic(" ".join(a.get("name", "") for a in it.get("artists", [])))
                    cand_album_name = cand_title
                if PRINT_SEARCH_INFO:
                    logging.info("Candidate #%d: title='%s' | artist='%s' | album='%s'", overall_idx, cand_title, cand_artist_norm, cand_album_name)
                title_ok = _tokens_in_candidate(title_tokens, cand_title)
                artist_ok = (not artist_tokens) or _tokens_in_candidate(artist_tokens, cand_artist_norm) or (remixer_tokens and _tokens_in_candidate(remixer_tokens, cand_artist_norm))
                album_ok = True
                if album_tokens:
                    album_ok = _tokens_in_candidate(album_tokens, cand_album_name)
                accepted = bool(title_ok and artist_ok and album_ok)
                if PRINT_SEARCH_INFO:
                    logging.info("ACCEPTED" if accepted else "REJECTED")
                if accepted:
                    return it
                if overall_idx >= combined_limit:
                    break
            if overall_idx >= combined_limit:
                break
            offset += per_request
            if len(items) < per_request:
                break
        if overall_idx >= combined_limit:
            break

    # Fallback: artist->albums->tracks exploration
    if n_artist:
        artist_search_q = f'artist:"{n_artist}"'
        if PRINT_SEARCH_INFO:
            logging.info("Fallback artist search: %s", artist_search_q)
        artist_resp = spotifysearch(token, artist_search_q, type_="artist", limit=1, offset=0, market=MARKET)
        artist_items = []
        try:
            artist_items = artist_resp.get("artists", {}).get("items", []) if artist_resp else []
        except Exception:
            artist_items = []
        if artist_items:
            artist_id = artist_items[0].get("id")
            if PRINT_SEARCH_INFO:
                logging.info("Found artist id=%s; enumerating albums", artist_id)
            if artist_id:
                a_off = 0
                while True:
                    a_resp = spotify_get_artist_albums(token, artist_id, limit=SPOTIFY_MAX_LIMIT, offset=a_off, market=MARKET)
                    if not a_resp:
                        break
                    albums = a_resp.get("items", []) or []
                    if not albums:
                        break
                    for alb in albums:
                        alb_id = alb.get("id")
                        if not alb_id:
                            continue
                        t_off = 0
                        while True:
                            t_resp = spotify_get_album_tracks(token, alb_id, limit=SPOTIFY_MAX_LIMIT, offset=t_off, market=MARKET)
                            if not t_resp:
                                break
                            tracks = t_resp.get("items", []) or []
                            if not tracks:
                                break
                            for tr in tracks:
                                tr_id = tr.get("id")
                                if tr_id and f"id:{tr_id}" in seen_keys:
                                    continue
                                it_like = {"id": tr.get("id"), "name": tr.get("name"), "artists": tr.get("artists", []), "album": {"name": alb.get("name")}}
                                cand_title = _normalize_text_basic(it_like.get("name"))
                                cand_artists = " ".join(a.get("name", "") for a in it_like.get("artists", []))
                                cand_artist_norm = _normalize_artist_for_search(cand_artists)
                                cand_album_name = _normalize_title_for_search(alb.get("name"))
                                title_ok = _tokens_in_candidate(title_tokens, cand_title)
                                artist_ok = (not artist_tokens) or _tokens_in_candidate(artist_tokens, cand_artist_norm) or (remixer_tokens and _tokens_in_candidate(remixer_tokens, cand_artist_norm))
                                album_ok = True
                                if album_tokens:
                                    album_ok = _tokens_in_candidate(album_tokens, cand_album_name)
                                if title_ok and artist_ok and album_ok:
                                    return it_like
                                seen_keys.add(f"id:{tr_id}" if tr_id else f"key:{cand_title}|{cand_artist_norm}|{cand_album_name}")
                            if len(tracks) < SPOTIFY_MAX_LIMIT:
                                break
                            t_off += SPOTIFY_MAX_LIMIT
                    if len(albums) < SPOTIFY_MAX_LIMIT:
                        break
                    a_off += SPOTIFY_MAX_LIMIT
    return None
