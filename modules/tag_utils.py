"""
modules/tag_utils.py
Utilities for downloading images asynchronously, obtaining artist genres, 
and manipulating mutagen metadata tags natively for FLAC and ID3 formats.
"""

from pathlib import Path
from typing import List, Optional, Tuple
import aiohttp

# Mutagen imports
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TCON, TSRC

# Import endpoints for artist lookup from config.py
from config import SPOTIFY_ARTIST_URL


async def download_image_bytes(session: aiohttp.ClientSession, url: str) -> Optional[Tuple[bytes, str]]:
    """
    Download image bytes asychronously from a given URL.
    Reason: Decouples the blocking HTTP request into the async event loop 
    so that image fetching doesn't pause the entire queue.
    """
    try:
        async with session.get(url) as r:
            if r.status == 200:
                mime = r.headers.get("Content-Type", "") or "image/jpeg"
                content = await r.read()
                return content, mime
            return None
    except Exception:
        return None


async def get_artist_genres(session: aiohttp.ClientSession, token: str, artist_id: str) -> List[str]:
    """
    Asynchronously retrieve an artist's associated genres.
    Reason: Artists often have metadata schemas that describe their base sound, 
    useful when individual tracks lack explicit genre tagging in Spotify.
    """
    try:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(SPOTIFY_ARTIST_URL.format(artist_id), headers=headers) as r:
            if r.status == 200:
                j = await r.json()
                genres = j.get("genres", [])
                if isinstance(genres, list):
                    return genres
    except Exception:
        pass
    return []


def remove_existing_pictures_generic(path: Path, audio_obj) -> None:
    """
    Remove existing embedded pictures from an audio object (ID3/FLAC).
    Reason: Cleanses the file of old, low-res, or incorrect artwork before 
    inserting the freshly downloaded high-fidelity image from Spotify.
    """
    ext = path.suffix.lower()
    try:
        if isinstance(audio_obj, ID3):
            try:
                audio_obj.delall("APIC")
            except Exception:
                pass
            return
        if ext == ".flac":
            if hasattr(audio_obj, "clear_pictures"):
                try:
                    audio_obj.clear_pictures()
                    return
                except Exception:
                    pass
            if hasattr(audio_obj, "pictures"):
                try:
                    audio_obj.pictures[:] = []
                    return
                except Exception:
                    pass
    except Exception:
        pass


def set_genre_on_audio(path: Path, audio_tmp, genres_list: List[str]) -> None:
    """
    Set or remove genre tags on an audio object for MP3/FLAC formats safely.
    Reason: Standardizes genre delimiter mapping since ID3 and Vorbis Comments 
    have varied ways of storing multiple genres natively.
    """
    ext = path.suffix.lower()
    genre_value = "; ".join(genres_list) if genres_list else None
    
    try:
        if isinstance(audio_tmp, ID3):
            if genre_value:
                try:
                    audio_tmp.delall("TCON")
                except Exception:
                    pass
                audio_tmp.add(TCON(encoding=3, text=genre_value))
            else:
                try:
                    audio_tmp.delall("TCON")
                except Exception:
                    pass
            return
            
        if ext == ".flac":
            if audio_tmp.tags is None:
                audio_tmp.tags = {}
            if genre_value:
                audio_tmp.tags["genre"] = [genre_value]
            else:
                for k in ("genre", "genres"):
                    if k in audio_tmp.tags:
                        del audio_tmp.tags[k]
            return
            
        if hasattr(audio_tmp, "tags"):
            if audio_tmp.tags is None:
                audio_tmp.tags = {}
            if genre_value:
                audio_tmp.tags["genre"] = [genre_value]
            else:
                try:
                    if "genre" in audio_tmp.tags:
                        del audio_tmp.tags["genre"]
                except Exception:
                    pass
    except Exception:
        pass
