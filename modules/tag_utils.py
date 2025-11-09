"""
"modules/tag_utils.py"
Utilities for downloading images, obtaining artist genres, and constructing/manipulating metadata.
"""

from pathlib import Path
from typing import List, Optional, Tuple
import requests
import logging
import io
import struct
import unicodedata
import tempfile
import wave

# Mutagen imports
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TCON, TSRC, ID3NoHeaderError
from mutagen.wave import WAVE

# Import endpoints for artist lookup from config.py (required)
from config import SPOTIFY_ARTIST_URL, REQUEST_TIMEOUT


def download_image_bytes(url: str) -> Optional[Tuple[bytes, str]]:
    """
    Download image bytes from URL and return (bytes, mime) or None on failure.
    """
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "") or "image/jpeg"
        return r.content, mime
    except Exception:
        return None


def get_artist_genres(token: str, artist_id: str) -> List[str]:
    """
    Return artist genres list from Spotify artist endpoint or empty list.
    """
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(SPOTIFY_ARTIST_URL.format(artist_id), headers=headers, timeout=REQUEST_TIMEOUT)
        if r.ok:
            j = r.json()
            genres = j.get("genres", [])
            if isinstance(genres, list):
                return genres
    except Exception:
        pass
    return []


def remove_existing_pictures_generic(path: Path, audio_obj) -> None:
    """
    Remove existing embedded pictures from an audio object (ID3/FLAC).
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
    Set or remove genre tags on audio object for MP3/FLAC/WAV formats.
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
        if ext == ".wav":
            if getattr(audio_tmp, "tags", None) is None:
                audio_tmp.tags = {}
            keys_to_try = ["IGNR", "IGEN", "GENR", "GENRE"]
            if genre_value:
                for k in keys_to_try:
                    try:
                        audio_tmp.tags[k] = [genre_value]
                        break
                    except Exception:
                        continue
            else:
                for k in keys_to_try:
                    try:
                        if k in audio_tmp.tags:
                            del audio_tmp.tags[k]
                    except Exception:
                        pass
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


# Helpers to build RIFF LIST/INFO and ID3 bytes
def _encode_text_for_info(s: str) -> bytes:
    """
    Encode text for RIFF INFO fields (UTF-8 with even-byte padding).
    """
    b = s.encode("utf-8")
    if len(b) % 2 == 1:
        b += b'\x00'
    return b


def build_info_list_chunk(metadata: dict) -> bytes:
    """
    Build a RIFF LIST/INFO chunk from metadata dictionary.
    """
    subchunks = b""
    mapping = [
        ("INAM", "title"),
        ("IART", "artist"),
        ("IPRD", "album"),
        ("ICRD", "date"),
        ("ITRK", "track"),
        ("TPOS", "disc"),
        ("IGNR", "genre"),
    ]
    for cid, key in mapping:
        v = metadata.get(key)
        if v:
            data = _encode_text_for_info(str(v))
            subchunks += cid.encode('ascii') + struct.pack('<I', len(data)) + data
    if not subchunks:
        return b""
    size = 4 + len(subchunks)  # "INFO" + subchunks
    chunk = b"LIST" + struct.pack('<I', size) + b"INFO" + subchunks
    return chunk


def build_id3_bytes_for_wav(image_bytes: Optional[bytes], mime: Optional[str], metadata: dict) -> bytes:
    """
    Build an ID3v2.3 tag in memory including APIC and textual frames:
    TIT2, TPE1, TALB, TDRC, TRCK, TPOS, TCON, TSRC (if provided).
    Use encoding=1 (UTF-16) for textual frames for better WAV+Mp3tag compatibility.
    """
    id3 = ID3()
    try:
        if image_bytes:
            id3.add(APIC(encoding=3, mime=mime or "image/jpeg", type=3, desc="Cover", data=image_bytes))
        # textual frames in UTF-16
        if metadata.get("title"):
            id3.add(TIT2(encoding=1, text=str(metadata["title"])))
        if metadata.get("artist"):
            id3.add(TPE1(encoding=1, text=str(metadata["artist"])))
        if metadata.get("album"):
            id3.add(TALB(encoding=1, text=str(metadata["album"])))
        if metadata.get("date"):
            id3.add(TDRC(encoding=1, text=str(metadata["date"])))
        if metadata.get("track"):
            id3.add(TRCK(encoding=1, text=str(metadata["track"])))
        if metadata.get("disc"):
            id3.add(TPOS(encoding=1, text=str(metadata["disc"])))
        if metadata.get("genre"):
            id3.add(TCON(encoding=1, text=str(metadata["genre"])))
        if metadata.get("isrc"):
            try:
                id3.add(TSRC(encoding=1, text=str(metadata["isrc"])))
            except Exception:
                pass
        bio = io.BytesIO()
        id3.save(bio, v2_version=3)
        b = bio.getvalue()
        if len(b) % 2 == 1:
            b += b'\x00'
        return b
    except Exception:
        return b""


# RIFF helpers
def find_first_riff_offset(b: bytes) -> int:
    """
    Return the byte offset of the first 'RIFF' occurrence or -1 if not found.
    """
    return b.find(b"RIFF")


def parse_riff_chunks_and_find_data_offset(b: bytes, start_offset: int = 0):
    """
    Parse RIFF chunks and find the 'data' chunk offset and size.
    """
    if len(b) < start_offset + 12:
        return -1, None, start_offset + 4
    if b[start_offset:start_offset+4] != b"RIFF":
        return -1, None, start_offset + 4
    off = start_offset + 12
    end = len(b)
    while off + 8 <= end:
        cid = b[off:off+4]
        sz = struct.unpack_from('<I', b, off+4)[0]
        if cid == b"data":
            return off, sz, start_offset + 4
        advance = 8 + sz + (sz % 2)
        off += advance
    return -1, None, start_offset + 4


def insert_chunk_before_data(original_bytes: bytes, chunk_id: bytes, chunk_data: bytes) -> bytes:
    """
    Insert a RIFF chunk (chunk_id) with chunk_data before the 'data' chunk.
    """
    riff_off = find_first_riff_offset(original_bytes)
    if riff_off == -1:
        raise RuntimeError("RIFF header not found in file")
    data_off, data_sz, riff_size_field = parse_riff_chunks_and_find_data_offset(original_bytes, riff_off)
    add_len = 8 + len(chunk_data)
    orig_riff_size = struct.unpack_from('<I', original_bytes, riff_off+4)[0]
    new_riff_size = orig_riff_size + add_len
    new_bytes = bytearray(original_bytes)
    struct.pack_into('<I', new_bytes, riff_off+4, new_riff_size)
    chunk = bytearray()
    chunk += chunk_id
    chunk += struct.pack('<I', len(chunk_data))
    chunk += chunk_data
    if data_off == -1:
        new_bytes.extend(chunk)
        return bytes(new_bytes)
    else:
        new = new_bytes[:data_off] + chunk + new_bytes[data_off:]
        return bytes(new)


def strip_id3_and_list_info(orig_bytes: bytes) -> bytes:
    """
    Remove any existing 'id3 ' chunks and 'LIST' chunks whose subtype is 'INFO'
    from a RIFF/WAVE byte buffer. Rebuilds RIFF size field accordingly.
    """
    riff_off = find_first_riff_offset(orig_bytes)
    if riff_off == -1:
        return orig_bytes
    if len(orig_bytes) < riff_off + 12:
        return orig_bytes

    off = riff_off + 12
    end = len(orig_bytes)
    kept_chunks = bytearray()
    while off + 8 <= end:
        cid = orig_bytes[off:off+4]
        sz = struct.unpack_from('<I', orig_bytes, off+4)[0]
        data_start = off + 8
        data_end = data_start + sz
        if data_end > end:
            # malformed - keep rest and break
            kept_chunks += orig_bytes[off:end]
            break
        skip = False
        if cid == b"id3 ":
            skip = True
        elif cid == b"LIST":
            # check subtype (first 4 bytes inside LIST data)
            if orig_bytes[data_start:data_start+4] == b"INFO":
                skip = True
        if not skip:
            # include chunk + padding byte if present
            chunk_end = data_end + (sz % 2)
            kept_chunks += orig_bytes[off:chunk_end]
        off = data_end + (sz % 2)

    new_riff_size = 4 + len(kept_chunks)  # 'WAVE' (4) + kept chunks
    new_buf = bytearray()
    new_buf += b"RIFF"
    new_buf += struct.pack('<I', new_riff_size)
    new_buf += orig_bytes[riff_off+8:riff_off+12]  # 'WAVE' (4 bytes) - keep original WAVE id
    new_buf += kept_chunks
    return bytes(new_buf)
