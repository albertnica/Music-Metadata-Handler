"""
modules/processor.py
Per-file processing orchestrator. 
This module has been decoupled to support asynchronous batch processing.
It exposes atomic stages: extracting local tags, querying Spotify asymptotically, 
and committing updates safely to disk.
"""

from pathlib import Path
import logging
from typing import Optional, Tuple, Dict, Any
import shutil
import asyncio

# Configuration
from config import OVERWRITE_TITLE_ARTIST_OR_ALBUM, UPDATE_ONLY_GENRE, PRINT_SEARCH_INFO, MARKET, SEARCH_CANDIDATE_LIMIT

# Utilities from other modules
from modules.filename_utils import infer_artist_title_from_filename, unique_temp_copy, send_original_to_trash
from modules.search_utils import _strip_parentheses_with_feat
from modules.spotify_client import spotifysearch, spotify_find_best_match, _debug_log
from modules.tag_utils import (
    download_image_bytes,
    get_artist_genres,
    remove_existing_pictures_generic,
    set_genre_on_audio,
)

# Mutagen imports
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TCON, TSRC, ID3NoHeaderError

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
        return None
    except Exception:
        return None


def read_audio_object(file_path: Path):
    ext = file_path.suffix.lower()
    audio = None
    try:
        if ext == ".flac":
            audio = FLAC(str(file_path))
        elif ext == ".mp3":
            try:
                audio = ID3(str(file_path))
            except ID3NoHeaderError:
                audio = ID3()
        else:
            return None, None, ext
    except Exception as e:
        return None, None, ext

    try:
        if isinstance(audio, FLAC):
            tags = audio.tags or {}
        elif isinstance(audio, ID3):
            tags = audio
        else:
            tags = {}
    except Exception:
        tags = {}
    return audio, tags, ext


def extract_local_tags(file_path: Path) -> Optional[Dict[str, Any]]:
    """
    STAGE 1 (Sync): Extracts baseline metadata from the local audio file to queue for Spotify.
    """
    audio_obj, tags, ext = read_audio_object(file_path)
    if audio_obj is None:
        logging.info("Unsupported format or unreadable file: %s", file_path.name)
        return None

    isrc_tag = first_tag_generic(audio_obj, tags, "isrc") or first_tag_generic(audio_obj, tags, "ISRC")
    id3_artist = first_tag_generic(audio_obj, tags, "artist") or first_tag_generic(audio_obj, tags, "albumartist")
    id3_album = first_tag_generic(audio_obj, tags, "album")
    id3_title = first_tag_generic(audio_obj, tags, "title")

    fn_artist, fn_title = infer_artist_title_from_filename(file_path)

    if not id3_artist and not fn_artist and not id3_title and not fn_title:
        logging.info("Insufficient metadata to search for: %s", file_path.name)
        return None

    return {
        "file_path": file_path,
        "id3_artist": id3_artist,
        "id3_title": id3_title,
        "id3_album": id3_album,
        "fn_artist": fn_artist,
        "fn_title": fn_title,
        "artist": id3_artist or fn_artist, # Fallback generic
        "album": id3_album,
        "title": id3_title or fn_title, # Fallback generic
        "isrc_tag": isrc_tag,
        "ext": ext
    }


def map_spotify_match_to_metadata(match: dict, artist: Optional[str], album: Optional[str], title: Optional[str], isrc_tag: Optional[str]) -> Tuple[Dict[str, str], Optional[str], Optional[str], Optional[str]]:
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
        artists = match.get("artists", [])
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


async def fetch_spotify_data_async(session, semaphore, token: str, local_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    STAGE 2 (Async): Network bound layer gathering Spotify metadata and art without blocking CPU logic.
    Follows a cascading fallback: ISRC -> ID3 -> Filename.
    """
    match = None
    
    # 1. ISRC exact strict query
    if local_data["isrc_tag"]:
        isrc_q = f'isrc:"{local_data["isrc_tag"].strip()}"'
        try:
            j = await spotifysearch(session, semaphore, token, isrc_q, type_="track", limit=1, offset=0, market=MARKET)
            if j and "error" not in j:
                items = j.get("tracks", {}).get("items", [])
                if items:
                    match = items[0]
                    _debug_log([f"[{local_data['file_path'].name}]", f"  MATCH FOUND via ISRC: {local_data['isrc_tag']}"])
        except Exception:
            pass
            
    # 2. ID3 Metadata search
    if not match and local_data["id3_artist"] and local_data["id3_title"]:
        artist_for_search = _strip_parentheses_with_feat(local_data["id3_artist"])
        title_for_search = _strip_parentheses_with_feat(local_data["id3_title"])
        album_for_search = _strip_parentheses_with_feat(local_data["id3_album"]) if local_data["id3_album"] else None
        match = await spotify_find_best_match(session, semaphore, token, artist_for_search, album_for_search, title_for_search, combined_limit=SEARCH_CANDIDATE_LIMIT)

    # 3. Filename parser search
    if not match and local_data["fn_artist"] and local_data["fn_title"]:
        artist_for_search = _strip_parentheses_with_feat(local_data["fn_artist"])
        title_for_search = _strip_parentheses_with_feat(local_data["fn_title"])
        match = await spotify_find_best_match(session, semaphore, token, artist_for_search, None, title_for_search, combined_limit=SEARCH_CANDIDATE_LIMIT)

    if not match:
        logging.info("No Spotify match for: %s", local_data["file_path"].name)
        return None

    metadata_map, image_url, artist_id, meta_artist = map_spotify_match_to_metadata(match, local_data["artist"], local_data["album"], local_data["title"], local_data["isrc_tag"])

    if artist_id:
        try:
            g = await get_artist_genres(session, token, artist_id)
            if g:
                metadata_map["genre"] = "; ".join(g)
        except Exception:
            pass

    image_bytes, mime = None, None
    if image_url:
        got = await download_image_bytes(session, image_url)
        if got:
            image_bytes, mime = got

    return {
        "file_path": local_data["file_path"],
        "metadata_map": metadata_map,
        "image_bytes": image_bytes,
        "mime": mime,
        "ext": local_data["ext"]
    }


def commit_tags_to_file(update_data: Dict[str, Any], output_dir: Optional[Path] = None) -> bool:
    """
    STAGE 3 (Sync): ThreadPool bound layer to write the updated tags to the disk without freezing the event loop.
    Re-reads the mutagn audio object to avoid RAM exhaustion when processing thousands of files.
    """
    file_path = update_data["file_path"]
    metadata_map = update_data["metadata_map"]
    image_bytes = update_data["image_bytes"]
    mime = update_data["mime"]
    ext = update_data["ext"]

    temp_path = unique_temp_copy(file_path)
    
    try:
        if ext == ".flac":
            audio_tmp = FLAC(str(temp_path))
            if audio_tmp.tags is None: audio_tmp.tags = {}
            if OVERWRITE_TITLE_ARTIST_OR_ALBUM:
                audio_tmp.tags["title"] = [metadata_map["title"]]
                audio_tmp.tags["artist"] = [metadata_map["artist"]]
                if metadata_map["album"]: audio_tmp.tags["album"] = [metadata_map["album"]]
            else:
                if not first_tag_generic(audio_tmp, audio_tmp.tags, "title") and metadata_map["title"]:
                    audio_tmp.tags["title"] = [metadata_map["title"]]
                if not first_tag_generic(audio_tmp, audio_tmp.tags, "artist") and metadata_map["artist"]:
                    audio_tmp.tags["artist"] = [metadata_map["artist"]]
                if metadata_map["album"] and not first_tag_generic(audio_tmp, audio_tmp.tags, "album"):
                    audio_tmp.tags["album"] = [metadata_map["album"]]

            if metadata_map["date"]: audio_tmp.tags["date"] = [metadata_map["date"]]
            if metadata_map["track"]: audio_tmp.tags["tracknumber"] = [metadata_map["track"]]
            if metadata_map["disc"]: audio_tmp.tags["discnumber"] = [metadata_map["disc"]]
            if metadata_map["genre"]:
                set_genre_on_audio(file_path, audio_tmp, metadata_map["genre"].split("; ") if metadata_map["genre"] else [])
                
            if image_bytes:
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = mime
                try: remove_existing_pictures_generic(file_path, audio_tmp)
                except Exception: pass
                try: audio_tmp.add_picture(pic)
                except Exception: pass
                
            try: audio_tmp.save()
            except Exception:
                try: audio_tmp.save(str(temp_path))
                except Exception: pass

        elif ext == ".mp3":
            try: audio_tmp = ID3(str(temp_path))
            except ID3NoHeaderError: audio_tmp = ID3()
            
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
                    
            if metadata_map["date"]: audio_tmp.delall("TDRC"); audio_tmp.add(TDRC(encoding=3, text=metadata_map["date"]))
            if metadata_map["track"]: audio_tmp.delall("TRCK"); audio_tmp.add(TRCK(encoding=3, text=metadata_map["track"]))
            if metadata_map["disc"]: audio_tmp.delall("TPOS"); audio_tmp.add(TPOS(encoding=3, text=metadata_map["disc"]))
            if metadata_map["genre"]: audio_tmp.delall("TCON"); audio_tmp.add(TCON(encoding=3, text=metadata_map["genre"]))
            if metadata_map["isrc"]:
                try: audio_tmp.delall("TSRC"); audio_tmp.add(TSRC(encoding=3, text=metadata_map["isrc"]))
                except Exception: pass
                
            if image_bytes:
                try: remove_existing_pictures_generic(file_path, audio_tmp)
                except Exception: pass
                try: audio_tmp.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
                except Exception: pass
                
            try: audio_tmp.save(str(temp_path))
            except Exception:
                try: audio_tmp.save()
                except Exception: pass
        else:
            if Path(temp_path).exists():
                try: Path(temp_path).unlink()
                except Exception: pass
            return False

        dest_path = Path(output_dir) / file_path.name if output_dir else file_path
        try:
            dest_parent = dest_path.parent
            if not dest_parent.exists():
                dest_parent.mkdir(parents=True, exist_ok=True)
        except Exception: pass
        
        send_original_to_trash(file_path)
        shutil.move(str(temp_path), str(dest_path))
        logging.info("Successfully updated tags for: %s", dest_path.name)
        return True

    except Exception as e:
        if Path(temp_path).exists():
            try: Path(temp_path).unlink()
            except Exception: pass
        logging.error("Failed updating %s on disk: %s", file_path.name, e)
        return False
