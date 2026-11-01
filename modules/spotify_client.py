"""
modules/spotify_client.py
Selenium-backed Spotify Web Player DOM scraper and metadata client.

Why this module exists:
Bypasses official Spotify Web API `/v1/search` restrictions and rate limits (HTTP 429/403)
by driving a persistent Chrome WebDriver session (`data/spotify_chrome_profile`) against
the free Spotify Web Player (`https://open.spotify.com`), combined with local genre
caching (`data/genre_cache.json`) and high-resolution CDN cover art extraction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import logging
import re
import time
from typing import Any
import urllib.parse
import urllib.request

import aiohttp
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from modules.search_utils import (
    build_sanitized_query,
    extract_remixer_tokens_from_title,
    normalize_artist_for_search,
    normalize_text_basic,
    normalize_title_for_search,
    tokens,
)

# Matches any Spotify album cover CDN URL (`ab67616d0000` + 4-hex size code + 24-hex image ID)
# and rewrites the size code to `b273` (canonical 640x640 JPEG front cover).
_SPOTIFY_CDN_THUMB_RE: re.Pattern[str] = re.compile(
    r"(https://i\.scdn\.co/image/ab67616d0000)[0-9a-fA-F]{4}([0-9a-fA-F]{16,32})"
)

# Month name lookup table (English & Spanish) for normalizing album page footer dates into YYYY-MM-DD.
_MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1, "enero": 1, "ene": 1,
    "february": 2, "feb": 2, "febrero": 2,
    "march": 3, "mar": 3, "marzo": 3,
    "april": 4, "apr": 4, "abril": 4, "abr": 4,
    "may": 5, "mayo": 5,
    "june": 6, "jun": 6, "junio": 6,
    "july": 7, "jul": 7, "julio": 7,
    "august": 8, "aug": 8, "agosto": 8, "ago": 8,
    "september": 9, "sep": 9, "sept": 9, "septiembre": 9, "setiembre": 9,
    "october": 10, "oct": 10, "octubre": 10,
    "november": 11, "nov": 11, "noviembre": 11,
    "december": 12, "dec": 12, "diciembre": 12, "dic": 12,
}

_EN_DATE_RE: re.Pattern[str] = re.compile(
    r"^([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})$"
)
_ES_DATE_RE: re.Pattern[str] = re.compile(
    r"^(\d{1,2})\s+(?:de\s+)?([A-Za-z]+)(?:\s+de)?,?\s+((?:19|20)\d{2})$"
)
_ISO_DATE_RE: re.Pattern[str] = re.compile(
    r"^((?:19|20)\d{2})[-/](0[1-9]|1[0-2])[-/](0[1-9]|[12]\d|3[01])$"
)
_YEAR_ONLY_RE: re.Pattern[str] = re.compile(r"^((?:19|20)\d{2})$")

# Single-roundtrip JavaScript payload to extract candidate tracks from `/search/.../tracks`.
_SEARCH_TRACKS_EXTRACTOR_JS: str = """
const rows = document.querySelectorAll("div[data-testid='tracklist-row']");
const results = [];
rows.forEach((row, idx) => {
    try {
        const titleA = row.querySelector("a[data-testid='internal-track-link'], a[href*='/track/']");
        if (!titleA) return;
        const title = (titleA.innerText || "").trim();
        const trackHref = titleA.href || "";
        let trackId = "";
        if (trackHref.includes("/track/")) {
            trackId = trackHref.split("/track/")[1].split("?")[0].split("/")[0].trim();
        }

        const artistEls = row.querySelectorAll("a[href*='/artist/']");
        const artists = [];
        const seenArtists = new Set();
        artistEls.forEach(a => {
            const name = (a.innerText || "").trim();
            const href = a.href || "";
            let aid = "";
            if (href.includes("/artist/")) {
                aid = href.split("/artist/")[1].split("?")[0].split("/")[0].trim();
            }
            const key = aid || name.toLowerCase();
            if (name && !seenArtists.has(key)) {
                seenArtists.add(key);
                artists.push({ id: aid, name: name });
            }
        });

        let albumName = "";
        let albumId = "";
        const albumA = row.querySelector("a[href*='/album/']");
        if (albumA) {
            albumName = (albumA.innerText || "").trim();
            const aHref = albumA.href || "";
            if (aHref.includes("/album/")) {
                albumId = aHref.split("/album/")[1].split("?")[0].split("/")[0].trim();
            }
        }

        let imageUrl = "";
        const imgEl = row.querySelector("img[src*='ab67616d'], img");
        if (imgEl && imgEl.src) {
            imageUrl = imgEl.src;
        }

        results.push({
            id: trackId || `dom_${idx}`,
            name: title,
            artists: artists,
            album: {
                id: albumId,
                name: albumName,
                images: imageUrl ? [{ url: imageUrl }] : [],
                release_date: ""
            }
        });
    } catch (e) {}
});
return results;
"""

# Single-roundtrip JavaScript payload to extract full release date, album cover, and track numbers from `/album/{id}`.
_ALBUM_DETAILS_EXTRACTOR_JS: str = """
const result = {
    release_date_candidates: [],
    cover_url: "",
    tracks_by_id: {},
    tracks_by_title: {}
};
try {
    // 1. Collect full release date candidates from <p data-encore-id="text"> footer and header [data-testid="release-date"]
    const pEls = document.querySelectorAll("p[data-encore-id='text']");
    pEls.forEach(p => {
        const txt = (p.innerText || "").trim();
        if (txt && txt.length < 45 && /\\b(19|20)\\d{2}\\b/.test(txt) && !txt.includes("©") && !txt.includes("℗")) {
            result.release_date_candidates.push(txt);
        }
    });
    const dateEl = document.querySelector("[data-testid='release-date']");
    if (dateEl && dateEl.innerText) {
        result.release_date_candidates.push(dateEl.innerText.trim());
    }

    // 2. Scope album cover lookup strictly to [data-testid="entity-header"] or main ab67616d image
    // (avoiding sidebar [data-testid="entity-image"] and bottom [data-testid="card-image"] elements).
    const headerContainer = document.querySelector("[data-testid='entity-header']");
    if (headerContainer) {
        const hdrImg = headerContainer.querySelector("img[src*='ab67616d']");
        if (hdrImg && hdrImg.src) {
            result.cover_url = hdrImg.src;
        }
    }
    if (!result.cover_url) {
        const allAbImgs = document.querySelectorAll("img[src*='i.scdn.co/image/ab67616d']");
        for (const img of allAbImgs) {
            const tid = img.getAttribute("data-testid") || "";
            if (tid !== "entity-image" && tid !== "card-image" && tid !== "cover-art-image") {
                result.cover_url = img.src;
                break;
            }
        }
    }

    // 3. Extract track positions from the album tracklist
    const rows = document.querySelectorAll("div[data-testid='tracklist-row']");
    rows.forEach((row, i) => {
        const idxEl = row.querySelector("div[aria-colindex='1'] span");
        const numTxt = idxEl ? (idxEl.innerText || "").trim() : "";
        const trackNum = /^\\d+$/.test(numTxt) ? parseInt(numTxt, 10) : (i + 1);

        const titleA = row.querySelector("a[data-testid='internal-track-link'], a[href*='/track/']");
        if (titleA) {
            const tName = (titleA.innerText || "").trim().toLowerCase();
            if (tName && !(tName in result.tracks_by_title)) {
                result.tracks_by_title[tName] = trackNum;
            }
            const href = titleA.href || "";
            if (href.includes("/track/")) {
                const tid = href.split("/track/")[1].split("?")[0].split("/")[0].trim();
                if (tid) {
                    result.tracks_by_id[tid] = trackNum;
                }
            }
        }
    });
} catch (e) {}
return result;
"""


def parse_spotify_release_date(candidates: Sequence[str]) -> str:
    """
    Normalizes Spotify Web Player date strings (e.g., `'November 14, 2025'`,
    `'14 de noviembre de 2025'`, or `'2025'`) into ISO-8601 `'YYYY-MM-DD'` or `'YYYY'`.

    Why: The album page footer renders localized full dates (`Month DD, YYYY`) while
    the header only displays the 4-digit year. Parsing the footer text first yields
    complete `YYYY-MM-DD` metadata for ID3 `TDRC` and FLAC `date` tags.
    """
    fallback_year = ""
    for raw in candidates:
        text = raw.strip()
        if not text:
            continue

        iso_match = _ISO_DATE_RE.match(text)
        if iso_match:
            return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"

        en_match = _EN_DATE_RE.match(text)
        if en_match:
            month_num = _MONTH_MAP.get(en_match.group(1).lower())
            if month_num:
                day_num = int(en_match.group(2))
                year_str = en_match.group(3)
                return f"{year_str}-{month_num:02d}-{day_num:02d}"

        es_match = _ES_DATE_RE.match(text)
        if es_match:
            month_num = _MONTH_MAP.get(es_match.group(2).lower())
            if month_num:
                day_num = int(es_match.group(1))
                year_str = es_match.group(3)
                return f"{year_str}-{month_num:02d}-{day_num:02d}"

        year_match = _YEAR_ONLY_RE.match(text)
        if year_match and not fallback_year:
            fallback_year = year_match.group(1)

    return fallback_year


def select_primary_genre(genres: Sequence[str]) -> str:
    """
    Selects the most representative primary genre from a list of Spotify artist genres.

    Why: Spotify returns artist genres in alphabetical/arbitrary order where broad tags
    like 'pop' or 'edm' frequently shadow specific subgenres like 'Melodic Bass' or 'Dubstep'.
    """
    if not genres:
        return ""

    clean_genres = [g.lower().strip() for g in genres if g and g.strip()]
    if not clean_genres:
        return ""

    for key, mapped_name in config.GENRE_PRIORITY:
        for genre_str in clean_genres:
            if key == genre_str or key in genre_str:
                return mapped_name

    return clean_genres[0].title()


def upgrade_spotify_image_url(url: str | None) -> str | None:
    """
    Upgrades any Spotify `ab67616d` CDN thumbnail URL (`64x64`, `150x150`, `300x300`)
    to canonical `640x640` JPEG resolution (`ab67616d0000b273`).

    Why: Search result rows in the Spotify Web Player DOM render `64x64` thumbnails
    (`ab67616d00004851...`). Rewriting the size hash prefix to `ab67616d0000b273`
    yields the full `640x640` `image/jpeg` front cover directly from Spotify's CDN.
    """
    if not url:
        return None
    return _SPOTIFY_CDN_THUMB_RE.sub(r"\1b273\2", url.strip())


def debug_log(messages: Sequence[str]) -> None:
    """
    Appends diagnostic search trace lines to `config.DEBUG_LOG_PATH` when
    `config.PRINT_SEARCH_INFO` is enabled.

    Why: Gating disk writes behind `PRINT_SEARCH_INFO` prevents unconditional file I/O
    during standard batch executions.
    """
    if not messages or not bool(config.PRINT_SEARCH_INFO):
        return
    try:
        with config.DEBUG_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write("\n" + "\n".join(messages) + "\n" + ("-" * 50) + "\n")
    except OSError as exc:
        logging.debug("Failed writing to search debug log: %s", exc)


def score_candidate(
    artist_tokens: Sequence[str],
    title_tokens: Sequence[str],
    cand_artist_norm: str,
    cand_title: str,
    remixer_tokens: Sequence[str] | None = None,
) -> float:
    """
    Evaluates lexical similarity between local track tokens and a Spotify candidate.

    Why: Spotify's search ranking often prioritizes popularity over exact lexical match
    (e.g., returning a radio edit instead of a specific remix, or a tribute cover).
    This weighted set-intersection heuristic rewards title/artist/remixer overlap and
    penalizes both missing local tokens and extraneous candidate tokens.
    """
    cand_art_toks = set(cand_artist_norm.split())
    cand_tit_toks = set(cand_title.split())

    local_art_toks = set(artist_tokens)
    local_tit_toks = set(title_tokens)

    title_matches = len(local_tit_toks.intersection(cand_tit_toks))
    artist_matches: float = float(len(local_art_toks.intersection(cand_art_toks)))

    remixer_matches = 0
    if remixer_tokens:
        remixer_matches = len(set(remixer_tokens).intersection(cand_art_toks))
        artist_matches += remixer_matches * 1.5

    artist_penalty = len(cand_art_toks - local_art_toks) * 0.2
    title_penalty = len(cand_tit_toks - local_tit_toks) * 0.1
    missed_title = len(local_tit_toks - cand_tit_toks) * 0.5
    missed_artist = len(local_art_toks - cand_art_toks) * 0.5

    score = (
        (title_matches * 2.0)
        + (artist_matches * 1.5)
        - artist_penalty
        - title_penalty
        - missed_title
        - missed_artist
    )

    is_valid = title_matches > 0
    if local_art_toks and artist_matches == 0 and remixer_matches == 0:
        is_valid = False

    return score if is_valid else -999.0


class SpotifyWebScraper:
    """
    Selenium-based Spotify Web Player client using an isolated persistent Chrome profile
    (`data/spotify_chrome_profile`) and local genre cache (`data/genre_cache.json`).

    Why: Eliminates dependency on Spotify Developer API credentials and `/v1/search`
    rate limits by scraping track candidates and album details directly from the
    rendered Web Player DOM.
    """

    def __init__(self) -> None:
        self._driver: webdriver.Chrome | None = None
        self._logged_in_verified: bool = False
        self._album_cache: dict[str, dict[str, Any]] = {}
        self._genre_cache: dict[str, str] | None = None

    @staticmethod
    def _cleanup_profile_locks() -> None:
        """
        Removes stale Chromium lock files left behind by abnormally terminated sessions.
        Why: Prevents `SessionNotCreatedException` when relaunching Chrome with `--user-data-dir`.
        """
        config.CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
            lock_path = config.CHROME_PROFILE_DIR / lock_name
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _init_driver(self) -> webdriver.Chrome:
        """
        Initializes or reuses the persistent Chrome WebDriver instance.
        Why: Reusing a single browser session across all tracks in Stage 2 avoids the
        3-second browser startup overhead on every file.
        """
        if self._driver is not None:
            try:
                _ = self._driver.title
                return self._driver
            except Exception:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None

        logging.info("Initializing Chrome with persistent profile: %s", config.CHROME_PROFILE_DIR)
        self._cleanup_profile_locks()

        options = Options()
        options.add_argument(f"--user-data-dir={config.CHROME_PROFILE_DIR.resolve()}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "const newProto = navigator.__proto__; "
                    "delete newProto.webdriver; "
                    "navigator.__proto__ = newProto;"
                )
            },
        )
        self._driver = driver
        return driver

    def ensure_logged_in(self) -> bool:
        """
        Verifies an authenticated Spotify Web Player session (`sp_dc` cookie),
        restoring saved cookies or prompting interactive browser login when necessary.

        Why: Spotify's `/search/.../tracks` view requires an authenticated (free or premium)
        session to render full tracklist rows reliably without login wall redirects.
        """
        if self._logged_in_verified:
            return True

        driver = self._init_driver()
        driver.get(config.SPOTIFY_WEB_BASE_URL)
        time.sleep(3)

        config.COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)

        def _is_guest() -> bool:
            try:
                login_buttons = driver.find_elements(By.CSS_SELECTOR, "[data-testid='login-button']")
                if login_buttons and login_buttons[0].is_displayed():
                    return True
                cookies_now = driver.get_cookies()
                if not any(c.get("name") == "sp_dc" for c in cookies_now):
                    return True
            except Exception:
                pass
            return False

        # Only inject saved cookies if the persistent Chrome profile is not already logged in.
        if _is_guest() and config.COOKIE_FILE.exists():
            logging.info("Loading saved Spotify session cookies...")
            try:
                cookies = json.loads(config.COOKIE_FILE.read_text(encoding="utf-8"))
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        pass
                driver.refresh()
                time.sleep(3)
            except Exception as exc:
                logging.warning("Could not load saved session cookies: %s", exc)

        if _is_guest():
            logging.warning("Not logged in to Spotify Web Player or session expired.")
            logging.warning("Please log in with your free Spotify account in the opened Chrome window.")
            while True:
                input("  -> Press ENTER here in the console once you have completed the login...")
                if not _is_guest():
                    logging.info("Spotify Web Player session detected. Saving cookies...")
                    break
                logging.warning("Session not detected yet. Please finish logging in inside Chrome.")

        try:
            saved_cookies = driver.get_cookies()
            config.COOKIE_FILE.write_text(
                json.dumps(saved_cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logging.warning("Could not persist session cookies: %s", exc)

        self._logged_in_verified = True
        return True

    def search_tracks_dom(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """
        Navigates to `https://open.spotify.com/search/{query}/tracks` and extracts up to
        `limit` track candidates directly from the rendered DOM via JavaScript.

        Why: Extracting all visible `div[data-testid='tracklist-row']` elements in a single
        `execute_script` round-trip avoids dozens of slow Selenium WebElement IPC calls.
        """
        cleaned_q = query.strip()
        if not cleaned_q:
            return []

        self.ensure_logged_in()
        driver = self._init_driver()
        encoded_q = urllib.parse.quote(cleaned_q)
        search_url = config.SPOTIFY_WEB_SEARCH_TRACKS_URL.format(encoded_q)

        try:
            driver.get(search_url)
            WebDriverWait(driver, max(4, min(10, int(config.REQUEST_TIMEOUT)))).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='tracklist-row']"))
            )
            # Brief settle delay so React finishes hydrating artist/album links and cover thumbnails.
            time.sleep(0.4)
            raw_items = driver.execute_script(_SEARCH_TRACKS_EXTRACTOR_JS)
            if not isinstance(raw_items, list):
                return []

            normalized_items: list[dict[str, Any]] = []
            for item in raw_items[: max(1, limit)]:
                if not isinstance(item, dict):
                    continue
                album_info = item.get("album") or {}
                images = album_info.get("images") or []
                if images and images[0].get("url"):
                    images[0]["url"] = upgrade_spotify_image_url(images[0]["url"])
                normalized_items.append(item)
            return normalized_items
        except Exception as exc:
            logging.debug("DOM track search returned no rows for '%s': %s", cleaned_q, exc)
            return []

    def scrape_album_details_dom(self, album_id: str) -> dict[str, Any]:
        """
        Navigates to `https://open.spotify.com/album/{album_id}` (with in-memory caching)
        to extract the album's full `release_date` (`YYYY-MM-DD`), `640x640` `cover_url`,
        and track numbers.

        Why: Search result rows omit release dates and track numbers. Caching by `album_id`
        ensures multiple tracks from the same album only trigger a single album page load.
        """
        if not album_id:
            return {}
        if album_id in self._album_cache:
            return self._album_cache[album_id]

        self.ensure_logged_in()
        driver = self._init_driver()
        album_url = config.SPOTIFY_WEB_ALBUM_URL.format(album_id)

        try:
            driver.get(album_url)
            WebDriverWait(driver, max(4, min(10, int(config.REQUEST_TIMEOUT)))).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-testid='tracklist-row']"))
            )
            time.sleep(0.4)
            raw_details = driver.execute_script(_ALBUM_DETAILS_EXTRACTOR_JS)
            if isinstance(raw_details, dict):
                candidates = raw_details.get("release_date_candidates") or []
                parsed_date = parse_spotify_release_date(
                    [str(c) for c in candidates if c]
                )
                raw_cover = raw_details.get("cover_url")
                upgraded_cover = (
                    upgrade_spotify_image_url(str(raw_cover)) if raw_cover else ""
                )
                normalized_details: dict[str, Any] = {
                    "release_date": parsed_date,
                    "cover_url": upgraded_cover or "",
                    "tracks_by_id": raw_details.get("tracks_by_id") or {},
                    "tracks_by_title": raw_details.get("tracks_by_title") or {},
                }
                self._album_cache[album_id] = normalized_details
                return normalized_details
        except Exception as exc:
            logging.debug("Failed scraping album details for %s: %s", album_id, exc)

        self._album_cache[album_id] = {}
        return {}

    def find_best_match(
        self,
        artist: str | None,
        album: str | None,
        title: str | None,
        combined_limit: int | None = None,
        allow_inverted: bool = True,
    ) -> dict[str, Any] | None:
        """
        Searches the Spotify Web Player DOM using progressively relaxed plain-text queries
        and selects the highest-scoring candidate via `score_candidate`.

        Why: The Web Player's search bar performs best with plain token queries
        (`"{artist} {title}"`) rather than strict API field prefixes (`track:...`).
        Evaluating both direct and inverted token orientations (`allow_inverted=True`)
        guarantees that filenames formatted as "Title - Artist" rather than "Artist - Title"
        are matched seamlessly without discarding the top Spotify result.
        """
        runtime_cfg = config.RuntimeConfig.from_module()
        limit_ceiling = (
            runtime_cfg.search_candidate_limit
            if combined_limit is None
            else max(1, int(combined_limit))
        )

        n_artist = normalize_artist_for_search(artist) if artist else ""
        n_title = normalize_title_for_search(title) if title else ""
        n_album = normalize_title_for_search(album) if album else ""

        artist_tokens = tokens(n_artist)
        title_tokens = tokens(n_title)
        remixer_tokens = extract_remixer_tokens_from_title(title or "")

        queries: list[str] = []
        q_artist_title = build_sanitized_query(n_artist, n_title, "", fielded=False)
        if q_artist_title:
            queries.append(q_artist_title)

        q_full = build_sanitized_query(n_artist, n_title, n_album, fielded=False)
        if q_full and q_full not in queries:
            queries.append(q_full)

        q_title_only = build_sanitized_query("", n_title, "", fielded=False)
        if q_title_only and q_title_only not in queries:
            queries.append(q_title_only)

        if allow_inverted:
            q_artist_only = build_sanitized_query("", n_artist, "", fielded=False)
            if q_artist_only and q_artist_only not in queries:
                queries.append(q_artist_only)

        seen_keys: set[str] = set()
        overall_idx = 0
        best_match: dict[str, Any] | None = None
        best_score = -999.0

        log_buffer: list[str] = [
            f"[{title} | {artist}]",
            f"  Tokens -> Title: {title_tokens}, Artist: {artist_tokens}",
        ]

        for query_str in queries:
            log_buffer.append(f"  -> DOM SEARCH QUERY: '{query_str}'")
            items = self.search_tracks_dom(query_str, limit=limit_ceiling)
            if not items:
                log_buffer.append("     [?] 0 DOM rows returned")
                continue

            for item in items:
                cand_title = normalize_text_basic(item.get("name"))
                cand_artists = " ".join(a.get("name", "") for a in item.get("artists", []))
                cand_artist_norm = normalize_artist_for_search(cand_artists)

                item_id = item.get("id")
                dedup_key = f"id:{item_id}" if item_id else f"key:{cand_title}|{cand_artist_norm}"
                if dedup_key in seen_keys:
                    continue

                overall_idx += 1
                seen_keys.add(dedup_key)

                score_direct = score_candidate(
                    artist_tokens,
                    title_tokens,
                    cand_artist_norm,
                    cand_title,
                    remixer_tokens,
                )

                score_inv = -999.0
                if allow_inverted and artist_tokens and title_tokens:
                    score_inv = score_candidate(
                        title_tokens,
                        artist_tokens,
                        cand_artist_norm,
                        cand_title,
                        remixer_tokens,
                    )

                if score_direct > 0 and score_direct >= score_inv:
                    score = score_direct
                    cand_label = f"SCORE: {score:.2f}"
                elif score_inv > 0:
                    score = score_inv - 0.2
                    cand_label = f"SCORE: {score:.2f} (inverted Title/Artist match)"
                else:
                    score = max(score_direct, score_inv)
                    cand_label = f"SCORE: {score:.2f}"

                log_buffer.append(
                    f"     Candidate {overall_idx}: '{item.get('name')}' by '{cand_artists}' -> {cand_label}"
                )

                if score > best_score and score > 0:
                    best_score = score
                    best_match = item

                if overall_idx >= limit_ceiling:
                    break

            if best_match and best_score >= 2.0:
                break
            if overall_idx >= limit_ceiling:
                break

        if best_match is None:
            log_buffer.append("  NO MATCH FOUND.")
            debug_log(log_buffer)
            return None

        # Enrich winning candidate with album page metadata (release_date, track_number, high-res cover).
        if runtime_cfg.scrape_album_details and not runtime_cfg.update_only_genre:
            self._enrich_match_with_album_details(best_match)

        # Resolve genre via local genre_cache.json (+ session token fallback if uncached).
        artists_list = best_match.get("artists") or []
        artist_ids = [str(a.get("id") or "").strip() for a in artists_list if a.get("id")]
        artist_names = [str(a.get("name") or "").strip() for a in artists_list if a.get("name")]
        resolved_genres = self.resolve_artist_genres(artist_ids, artist_names)
        if resolved_genres:
            best_match.setdefault("album", {})["genres"] = resolved_genres
            best_match["genres"] = resolved_genres

        log_buffer.append(
            f"  MATCH FOUND: '{best_match.get('name')}' Score: {best_score:.2f}"
        )
        debug_log(log_buffer)
        return best_match

    def _enrich_match_with_album_details(self, match: dict[str, Any]) -> None:
        """
        Populates `album.release_date`, `track_number`, and upgraded `album.images`
        on `match` using `scrape_album_details_dom`.
        """
        album_info = match.get("album")
        if not isinstance(album_info, dict):
            return
        album_id = str(album_info.get("id") or "").strip()
        if not album_id:
            return

        details = self.scrape_album_details_dom(album_id)
        if not details:
            return

        if details.get("release_date"):
            album_info["release_date"] = details["release_date"]

        # Only overwrite search-row cover art if the album page yielded a valid ab67616d JPEG URL.
        existing_images = album_info.get("images") or []
        has_valid_ab67_cover = bool(
            existing_images
            and isinstance(existing_images[0], dict)
            and "ab67616d0000b273" in str(existing_images[0].get("url") or "")
        )
        album_cover = str(details.get("cover_url") or "")
        if album_cover and ("ab67616d" in album_cover or not has_valid_ab67_cover):
            album_info["images"] = [{"url": album_cover}]

        track_id = str(match.get("id") or "").strip()
        track_title_lower = str(match.get("name") or "").strip().lower()
        tracks_by_id = details.get("tracks_by_id") or {}
        tracks_by_title = details.get("tracks_by_title") or {}

        track_num = tracks_by_id.get(track_id) or tracks_by_title.get(track_title_lower)
        if track_num:
            match["track_number"] = track_num

    def _load_genre_cache(self) -> dict[str, str]:
        """Loads `data/genre_cache.json` into memory once per session."""
        if self._genre_cache is not None:
            return self._genre_cache
        config.GENRE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if config.GENRE_CACHE_FILE.exists():
            try:
                data = json.loads(config.GENRE_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._genre_cache = {str(k): str(v) for k, v in data.items()}
                    return self._genre_cache
            except Exception as exc:
                logging.debug("Failed loading genre_cache.json: %s", exc)
        self._genre_cache = {}
        return self._genre_cache

    def _save_genre_cache(self) -> None:
        """Persists in-memory genre cache updates back to `data/genre_cache.json`."""
        if self._genre_cache is None:
            return
        try:
            config.GENRE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.GENRE_CACHE_FILE.write_text(
                json.dumps(self._genre_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logging.debug("Failed saving genre_cache.json: %s", exc)

    def get_session_token(self) -> str | None:
        """
        Extracts the active Spotify Web Player OAuth access token from the browser context
        as an optional fallback for uncached artist genres.
        """
        if self._driver is None:
            return None
        try:
            token_data = self._driver.execute_async_script(
                """
                const callback = arguments[arguments.length - 1];
                fetch('https://open.spotify.com/get_access_token?reason=transport&productType=web_player')
                    .then(res => res.json())
                    .then(data => callback(data))
                    .catch(err => callback({error: err.toString()}));
                """
            )
            if isinstance(token_data, dict) and token_data.get("accessToken"):
                return str(token_data["accessToken"])
        except Exception as exc:
            logging.debug("Could not extract Web Player session token: %s", exc)
        return None

    def resolve_artist_genres(
        self,
        artist_ids: Sequence[str],
        artist_names: Sequence[str],
    ) -> list[str]:
        """
        Resolves the primary genre for a track's artists using multi-tier resolution
        (ported from `Telegram-Music-Downloader`):
        1. Primary artist ID in `data/genre_cache.json`
        2. Primary artist name (exact or case-insensitive) in `data/genre_cache.json`
        3. Featured artist IDs / names in `data/genre_cache.json`
        4. Web Player session token fallback against `/v1/artists/{id}`, mapped via
           `select_primary_genre` and cached back to `data/genre_cache.json`.
        """
        genre_cache = self._load_genre_cache()
        lower_map = {k.lower(): v for k, v in genre_cache.items()}

        primary_aid = artist_ids[0].strip() if artist_ids else ""
        primary_name = artist_names[0].strip() if artist_names else ""

        # Tier 1: Primary artist ID in cache
        if primary_aid:
            cached = genre_cache.get(primary_aid)
            if cached and cached != "Unknown":
                return [cached]

        # Tier 2: Primary artist name in cache
        if primary_name:
            cached = genre_cache.get(primary_name) or lower_map.get(primary_name.lower())
            if cached and cached != "Unknown":
                if primary_aid:
                    genre_cache[primary_aid] = cached
                    self._save_genre_cache()
                return [cached]

        # Tier 3: Featured artists in cache
        for alt_id in artist_ids[1:]:
            cached = genre_cache.get(alt_id)
            if cached and cached != "Unknown":
                return [cached]

        for alt_name in artist_names[1:]:
            cached = genre_cache.get(alt_name) or lower_map.get(alt_name.lower())
            if cached and cached != "Unknown":
                return [cached]

        # Tier 4: Live Web Player session token fallback for uncached 22-char artist ID
        if primary_aid and len(primary_aid) == 22:
            session_token = self.get_session_token()
            if session_token:
                req = urllib.request.Request(
                    config.SPOTIFY_ARTIST_URL.format(primary_aid),
                    headers={"Authorization": f"Bearer {session_token}"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            payload = json.loads(resp.read().decode("utf-8"))
                            raw_genres = payload.get("genres") or []
                            if isinstance(raw_genres, list) and raw_genres:
                                selected = select_primary_genre([str(g) for g in raw_genres])
                                if selected and selected != "Unknown":
                                    genre_cache[primary_aid] = selected
                                    if primary_name:
                                        genre_cache[primary_name] = selected
                                    self._save_genre_cache()
                                    return [selected]
                except Exception as exc:
                    logging.debug("Live artist genre fallback skipped for %s: %s", primary_aid, exc)

        return []

    def close(self) -> None:
        """
        Closes the Chrome WebDriver instance and releases profile locks.

        Why: Frees system RAM/CPU immediately after Stage 2 finishes instead of holding
        the browser process open during Stage 3 disk writes.
        """
        if self._driver is not None:
            logging.info("Closing Chrome WebDriver session...")
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
            self._logged_in_verified = False


async def download_image_bytes(
    session: aiohttp.ClientSession,
    url: str,
    semaphore: asyncio.Semaphore | None = None,
) -> tuple[bytes, str] | None:
    """
    Downloads album artwork bytes and MIME type asynchronously from Spotify's CDN (`i.scdn.co`).

    Why: Spotify CDN URLs (`https://i.scdn.co/image/...`) are publicly accessible without
    API tokens. Fetching cover art via `aiohttp` keeps image downloads fast and non-blocking.
    """
    upgraded_url = upgrade_spotify_image_url(url)
    if not upgraded_url:
        return None
    timeout = aiohttp.ClientTimeout(total=max(1, int(config.REQUEST_TIMEOUT)))

    async def _fetch() -> tuple[bytes, str] | None:
        async with session.get(upgraded_url, timeout=timeout) as resp:
            if resp.status == 200:
                mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                content = await resp.read()
                return content, mime
        return None

    try:
        if semaphore is not None:
            async with semaphore:
                return await _fetch()
        return await _fetch()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logging.debug("Failed downloading cover art from %s: %s", upgraded_url, exc)
        return None


async def get_artist_genres(
    session: aiohttp.ClientSession,
    token: str,
    artist_id: str,
    semaphore: asyncio.Semaphore | None = None,
) -> list[str]:
    """
    Resolves artist genres from `data/genre_cache.json` (backward-compatible async signature).
    """
    _ = (session, token, semaphore)
    if not artist_id:
        return []
    scraper = SpotifyWebScraper()
    return scraper.resolve_artist_genres([artist_id], [])


# Backward-compatible aliases
SpotifyClient = SpotifyWebScraper
_debug_log = debug_log
_score_candidate = score_candidate
