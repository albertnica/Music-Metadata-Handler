"""
modules/processor.py
Per-file processing orchestrator. Read tags, apply filename fallback,
search Spotify (ISRC first, containment fallback) and write metadata for FLAC/MP3/WAV.
"""

from pathlib import Path
import logging
from typing import Optional, Tuple, Dict, Any
import shutil

# Configuration
from config import OVERWRITE_TITLE_ARTIST_OR_ALBUM, UPDATE_ONLY_GENRE, PRINT_SEARCH_INFO, MARKET, SEARCH_CANDIDATE_LIMIT

# Utilities from other modules
from modules.filename_utils import infer_artist_title_from_filename, unique_temp_copy, send_original_to_trash
from modules.search_utils import _strip_parentheses_with_feat, _normalize_artist_for_search, _normalize_title_for_search, _tokens, _tokens_in_candidate, _extract_remixer_tokens_from_title, _normalize_text_basic
from modules.spotify_client import spotifysearch, spotify_find_best_match, get_spotify_token
from modules.tag_utils import (
    download_image_bytes,
    get_artist_genres,
    remove_existing_pictures_generic,
    set_genre_on_audio,
)
from modules import wav_utils  # wav_utils contains WAV rebuild/insert helpers

# Mutagen imports
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TCON, TSRC, ID3NoHeaderError
from mutagen.wave import WAVE

# Basic helpers
def _val_to_str(v):
    try:
        if v is None:
            return None
        if isinstance(v, (list, tuple)) and v:
            v0 = v[0]
            if hasattr(v0, "text"):
                txt = getattr(v0, "text")
                if isinstance(txt, (list, tuple)):
                    return str(txt[0])
                return str(txt)
            return str(v0)
        if hasattr(v, "text"):
            txt = getattr(v, "text")
            if isinstance(txt, (list, tuple)):
                return str(txt[0])
            return str(txt)
        return str(v)
    except Exception:
        try:
            return str(v)
        except Exception:
            return None


def first_tag_generic(audio_obj, tags, key):
    """
    Generic extractor across FLAC/ID3/WAVE.
    """
    try:
        if isinstance(audio_obj, FLAC):
            v = tags.get(key)
            if v:
                if isinstance(v, (list, tuple)):
                    return str(v[0])
                return str(v)
            return None
        if isinstance(audio_obj, ID3):
            map_frames = {
                "artist": "TPE1",
                "albumartist": "TPE2",
                "album": "TALB",
                "title": "TIT2",
                "date": "TDRC",
                "tracknumber": "TRCK",
                "discnumber": "TPOS",
                "isrc": "TSRC",
                "genre": "TCON",
            }
            frame = map_frames.get(key)
            if frame and frame in tags:
                f = tags.getall(frame)
                if f:
                    try:
                        txt = f[0].text
                        if isinstance(txt,(list,tuple)):
                            return str(txt[0])
                        return str(txt)
                    except Exception:
                        try:
                            return str(f[0])
                        except Exception:
                            return None
            return None
        if getattr(tags, "get", None):
            v = tags.get(key)
            if v:
                return _val_to_str(v)
            alt_keys = {
                "title":["INAM","NAME","TITLE","TIT2"],
                "artist":["IART","AUTH","ARTIST","TPE1"],
                "album":["IPRD","ALBUM","TALB"],
                "date":["ICRD","DATE","YEAR","TDRC"],
                "tracknumber":["ITRK","TRACKNUMBER","TRCK"],
                "discnumber":["TPOS","DISCNUMBER"],
                "isrc":["TSRC","ISRC"],
                "genre":["IGEN","IGNR","GENR","GENRE","TCON"]
            }
            for alt in alt_keys.get(key, []):
                try:
                    vv = tags.get(alt)
                    if vv:
                        return _val_to_str(vv)
                except Exception:
                    continue
        return None
    except Exception:
        return None


def read_audio_object(file_path: Path):
    """
    Open file with mutagen and return tuple (audio_obj, tags, ext, wav_has_id3 flag).
    """
    ext = file_path.suffix.lower()
    wav_has_id3 = False
    audio = None
    try:
        if ext == ".flac":
            audio = FLAC(str(file_path))
        elif ext == ".mp3":
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()
        elif ext == ".wav":
            try:
                audio = ID3(str(file_path))
                wav_has_id3 = True
            except ID3NoHeaderError:
                try:
                    audio = WAVE(str(file_path))
                    wav_has_id3 = False
                except Exception:
                    audio = None
                    wav_has_id3 = False
        else:
            logging.info("Unsupported format: %s", file_path.name)
            return None, None, ext, wav_has_id3
    except Exception as e:
        logging.error("Could not open %s: %s", file_path.name, e)
        return None, None, ext, wav_has_id3

    # gather tags safely
    try:
        if isinstance(audio, FLAC):
            tags = audio.tags or {}
        elif isinstance(audio, ID3):
            tags = audio
        elif isinstance(audio, WAVE):
            tags = getattr(audio, "tags", {}) or {}
        else:
            tags = {}
    except Exception:
        tags = {}
    return audio, tags, ext, wav_has_id3


def extract_basic_tags(audio_obj, tags, file_path: Path) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Return (artist, album, title, isrc) with filename fallback.
    """
    artist = first_tag_generic(audio_obj, tags, "artist") or first_tag_generic(audio_obj, tags, "albumartist")
    album = first_tag_generic(audio_obj, tags, "album")
    title = first_tag_generic(audio_obj, tags, "title")
    isrc_tag = first_tag_generic(audio_obj, tags, "isrc") or first_tag_generic(audio_obj, tags, "ISRC")

    ai, ti = infer_artist_title_from_filename(file_path)
    artist = artist or ai
    title = title or ti

    return artist, album, title, isrc_tag


def map_spotify_match_to_metadata(
    match: dict,
    artist: Optional[str],
    album: Optional[str],
    title: Optional[str],
    isrc_tag: Optional[str],
) -> Tuple[Dict[str, str], Optional[str], Optional[str], Optional[str]]:
    """
    Build metadata_map dict from spotify match.
    """
    meta_artist = None
    meta_title = None
    meta_album = None
    meta_date = None
    meta_track = None
    meta_disc = None
    image_url = None
    artist_id = None
    genres_list = []

    if "album" in match and "name" in match:
        meta_title = match.get("name")
        album_info = match.get("album", {})
        meta_album = album_info.get("name")
        artists = match.get("artists", [])  # typically track-level artists
        if artists:
            names = [a.get("name") for a in artists if a.get("name")]
            meta_artist = ", ".join(names) if names else None
            artist_id = artists[0].get("id")
        meta_track = str(match.get("track_number")) if match.get("track_number") else None
        meta_disc = str(match.get("disc_number")) if match.get("disc_number") else None
        images = album_info.get("images", [])
        if images:
            image_url = images[0].get("url")
        meta_date = album_info.get("release_date")
        if isinstance(album_info.get("genres"), list) and album_info.get("genres"):
            genres_list = album_info.get("genres", [])
    else:
        meta_album = match.get("name")
        artists = match.get("artists", [])
        if artists:
            names = [a.get("name") for a in artists if a.get("name")]
            meta_artist = ", ".join(names) if names else None
            artist_id = artists[0].get("id")
        images = match.get("images", [])
        if images:
            image_url = images[0].get("url")
        meta_date = match.get("release_date")
        if isinstance(match.get("genres"), list) and match.get("genres"):
            genres_list = match.get("genres", [])

    metadata_map = {
        "title": meta_title or title or "",
        "artist": meta_artist or artist or "",
        "album": meta_album or album or "",
        "date": meta_date or "",
        "track": meta_track or "",
        "disc": meta_disc or "",
        "genre": "; ".join(genres_list) if genres_list else "",
        "isrc": isrc_tag or ""
    }
    return metadata_map, image_url, artist_id, meta_artist


# Main processing function
def process_single_file(file_path: Path, token: str) -> bool:
    """
    Process one file: read tags, search Spotify (ISRC first), obtain metadata_map,
    and write tags / pictures. WAV handling delegated to wav_utils.
    Returns True if updated, False otherwise.
    """
    audio_obj, tags, ext, wav_has_id3 = read_audio_object(file_path)
    if audio_obj is None and ext not in (".flac", ".mp3", ".wav"):
        return False

    artist, album, title, isrc_tag = extract_basic_tags(audio_obj, tags, file_path)
    if not artist and not title:
        logging.info("Insufficient metadata for: %s", file_path.name)
        return False

    artist_for_search = _strip_parentheses_with_feat(artist) if artist else None
    title_for_search = _strip_parentheses_with_feat(title) if title else None
    album_for_search = _strip_parentheses_with_feat(album) if album else None

    # Try ISRC first
    match = None
    if isrc_tag:
        isrc_q = f'isrc:"{isrc_tag.strip()}"'
        if PRINT_SEARCH_INFO:
            logging.info("Attempting ISRC search: %s", isrc_q)
        try:
            j = spotifysearch(token, isrc_q, type_="track", limit=1, offset=0, market=MARKET)
            if j:
                items = j.get("tracks", {}).get("items", [])
                if items:
                    match = items[0]
        except Exception:
            match = None

    if not match:
        match = spotify_find_best_match(token, artist_for_search, album_for_search, title_for_search, combined_limit=SEARCH_CANDIDATE_LIMIT)

    if not match:
        logging.info("No Spotify match for: %s", file_path.name)
        return False

    metadata_map, image_url, artist_id, meta_artist = map_spotify_match_to_metadata(match, artist, album, title, isrc_tag)

    # Optionally fetch genres via artist id (if not already present)
    if artist_id:
        try:
            g = get_artist_genres(token, artist_id)
            if g:
                metadata_map["genre"] = "; ".join(g)
        except Exception:
            pass

    # Create temp copy
    temp_path = unique_temp_copy(file_path)
    try:
        # FLAC write
        if ext == ".flac":
            audio_tmp = FLAC(str(temp_path))
            if audio_tmp.tags is None:
                audio_tmp.tags = {}
            if OVERWRITE_TITLE_ARTIST_OR_ALBUM:
                audio_tmp.tags["title"] = [metadata_map["title"]]
                audio_tmp.tags["artist"] = [metadata_map["artist"]]
                if metadata_map["album"]:
                    audio_tmp.tags["album"] = [metadata_map["album"]]
            else:
                # set only if missing (keeps previous)
                if not first_tag_generic(audio_tmp, audio_tmp.tags, "title") and metadata_map["title"]:
                    audio_tmp.tags["title"] = [metadata_map["title"]]
                if not first_tag_generic(audio_tmp, audio_tmp.tags, "artist") and metadata_map["artist"]:
                    audio_tmp.tags["artist"] = [metadata_map["artist"]]
                if metadata_map["album"] and not first_tag_generic(audio_tmp, audio_tmp.tags, "album"):
                    audio_tmp.tags["album"] = [metadata_map["album"]]

            if metadata_map["date"]:
                audio_tmp.tags["date"] = [metadata_map["date"]]
            if metadata_map["track"]:
                audio_tmp.tags["tracknumber"] = [metadata_map["track"]]
            if metadata_map["disc"]:
                audio_tmp.tags["discnumber"] = [metadata_map["disc"]]
            if metadata_map["genre"]:
                set_genre_on_audio(file_path, audio_tmp, metadata_map["genre"].split("; ") if metadata_map["genre"] else [])
            if image_url:
                got = download_image_bytes(image_url)
                if got:
                    image_bytes, mime = got
                    pic = Picture()
                    pic.data = image_bytes
                    pic.type = 3
                    pic.mime = mime
                    try:
                        remove_existing_pictures_generic(file_path, audio_tmp)
                    except Exception:
                        pass
                    try:
                        audio_tmp.add_picture(pic)
                    except Exception:
                        pass
            try:
                audio_tmp.save()
            except Exception:
                try:
                    audio_tmp.save(str(temp_path))
                except Exception:
                    pass

        # MP3 write
        elif ext == ".mp3":
            try:
                audio_tmp = ID3(str(temp_path))
            except ID3NoHeaderError:
                audio_tmp = ID3()
            if OVERWRITE_TITLE_ARTIST_OR_ALBUM:
                audio_tmp.delall("TIT2"); audio_tmp.add(TIT2(encoding=3, text=metadata_map["title"]))
                audio_tmp.delall("TPE1"); audio_tmp.add(TPE1(encoding=3, text=metadata_map["artist"]))
                if metadata_map["album"]:
                    audio_tmp.delall("TALB"); audio_tmp.add(TALB(encoding=3, text=metadata_map["album"]))
            else:
                if not first_tag_generic(audio_tmp, audio_tmp, "title") and metadata_map["title"]:
                    audio_tmp.delall("TIT2"); audio_tmp.add(TIT2(encoding=3, text=metadata_map["title"]))
                if not first_tag_generic(audio_tmp, audio_tmp, "artist") and metadata_map["artist"]:
                    audio_tmp.delall("TPE1"); audio_tmp.add(TPE1(encoding=3, text=metadata_map["artist"]))
                if metadata_map["album"] and not first_tag_generic(audio_tmp, audio_tmp, "album"):
                    audio_tmp.delall("TALB"); audio_tmp.add(TALB(encoding=3, text=metadata_map["album"]))
            if metadata_map["date"]:
                audio_tmp.delall("TDRC"); audio_tmp.add(TDRC(encoding=3, text=metadata_map["date"]))
            if metadata_map["track"]:
                audio_tmp.delall("TRCK"); audio_tmp.add(TRCK(encoding=3, text=metadata_map["track"]))
            if metadata_map["disc"]:
                audio_tmp.delall("TPOS"); audio_tmp.add(TPOS(encoding=3, text=metadata_map["disc"]))
            if metadata_map["genre"]:
                audio_tmp.delall("TCON"); audio_tmp.add(TCON(encoding=3, text=metadata_map["genre"]))
            if metadata_map["isrc"]:
                try:
                    audio_tmp.delall("TSRC"); audio_tmp.add(TSRC(encoding=3, text=metadata_map["isrc"]))
                except Exception:
                    pass
            if image_url:
                got = download_image_bytes(image_url)
                if got:
                    image_bytes, mime = got
                    try:
                        remove_existing_pictures_generic(file_path, audio_tmp)
                    except Exception:
                        pass
                    try:
                        audio_tmp.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
                    except Exception:
                        pass
            try:
                audio_tmp.save(str(temp_path))
            except Exception:
                try:
                    audio_tmp.save()
                except Exception:
                    pass

        # WAV: delegate to wav_utils to handle rebuild and chunk insertion
        elif ext == ".wav":
            updated = wav_utils.finalize_wav_with_metadata(temp_path, image_url, metadata_map)
            if not updated:
                # clean up temp if wav_utils failed
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except Exception:
                    pass
                return False

        else:
            logging.info("Unsupported for write: %s", file_path.name)
            if Path(temp_path).exists():
                try:
                    Path(temp_path).unlink()
                except Exception:
                    pass
            return False

        # Replace original: send original to trash and move temp into place
        send_original_to_trash(file_path)
        shutil.move(str(temp_path), str(file_path))
        return True

    except Exception as e:
        if Path(temp_path).exists():
            try:
                Path(temp_path).unlink()
            except Exception:
                pass
        logging.error("Failed updating %s: %s", file_path.name, e)
        return False
