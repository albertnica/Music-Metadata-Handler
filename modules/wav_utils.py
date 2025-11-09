"""
modules/wav_utils.py
WAV-specific helpers: detect ID3v2 at file start, extract candidate bytes, rebuild a clean WAV
via Python's wave module, and apply LIST/INFO and 'id3 ' chunks.
"""

from pathlib import Path
import tempfile
import wave
import logging
import struct
from typing import Optional

from modules.tag_utils import build_info_list_chunk, build_id3_bytes_for_wav, strip_id3_and_list_info, insert_chunk_before_data, download_image_bytes

# Parse ID3v2 header (syncsafe size). Returns full header size (including 10-byte header and optional footer)
def parse_id3v2_header_size(head_bytes: bytes) -> int:
    if len(head_bytes) < 10 or head_bytes[:3] != b"ID3":
        return 0
    sz_bytes = head_bytes[6:10]
    size = (sz_bytes[0] & 0x7F) << 21 | (sz_bytes[1] & 0x7F) << 14 | (sz_bytes[2] & 0x7F) << 7 | (sz_bytes[3] & 0x7F)
    flags = head_bytes[5]
    footer = 10 if (flags & 0x10) else 0
    return 10 + size + footer


def get_candidate_bytes_from_wav(orig_bytes: bytes) -> bytes:
    """
    Determine the appropriate slice of the original WAV bytes to use for rebuilding:
    - If ID3v2 at start: skip the ID3v2 header chunk
    - Else if RIFF occurs at offset > 0: slice from RIFF
    - Else use entire file
    """
    head = orig_bytes[:65536]
    id3_head_size = parse_id3v2_header_size(head)
    if id3_head_size > 0:
        logging.info("WAV: detected ID3v2 header at start (size=%d). Using slice after header.", id3_head_size)
        return orig_bytes[id3_head_size:]
    riff_idx = head.find(b"RIFF")
    if riff_idx > 0:
        logging.info("WAV: RIFF found at offset %d in header; using slice.", riff_idx)
        return orig_bytes[riff_idx:]
    logging.info("WAV: no ID3 at start and no RIFF in first 64KB; using entire file as candidate.")
    return orig_bytes


def rebuild_clean_wav(candidate_bytes: bytes) -> Optional[bytes]:
    """
    Rebuild a clean WAV using wave module to ensure proper chunk layout.
    Returns bytes of clean WAV or None on failure.
    """
    tmp_in_name = None
    tmp_out_name = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_in:
            tmp_in_name = tmp_in.name
            tmp_in.write(candidate_bytes)
        with wave.open(tmp_in_name, 'rb') as r:
            params = r.getparams()
            frames = r.readframes(r.getnframes())
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_out:
            tmp_out_name = tmp_out.name
        with wave.open(tmp_out_name, 'wb') as w:
            w.setparams(params)
            w.writeframes(frames)
        clean_bytes = Path(tmp_out_name).read_bytes()
        return clean_bytes
    except Exception as e:
        logging.error("WAV rebuild failed: %s", e)
        return None
    finally:
        # cleanup temp intermediate files if present
        try:
            if tmp_in_name:
                Path(tmp_in_name).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if tmp_out_name:
                # keep tmp_out until caller reads/writes; caller should unlink
                pass
        except Exception:
            pass


def apply_metadata_chunks_to_wav(clean_bytes: bytes, list_chunk: bytes, id3_bytes: bytes) -> Optional[bytes]:
    """
    Insert LIST/INFO and 'id3 ' chunks before 'data' chunk.
    list_chunk: full LIST chunk bytes (including 'LIST' + size + 'INFO' + subchunks) or b''.
    id3_bytes: bytes for id3 (already padded to even length) or b''.
    Returns modified bytes or None on failure.
    """
    try:
        # strip existing id3/LIST INFO to avoid duplicates first
        try:
            clean_bytes = strip_id3_and_list_info(clean_bytes)
        except Exception as e:
            logging.info("WAV: failed to strip existing id3/LIST chunks (non-fatal): %s", e)

        if list_chunk:
            # pass only the "INFO"+subchunks as chunk_data to insert_chunk_before_data
            clean_bytes = insert_chunk_before_data(clean_bytes, b"LIST", list_chunk[8:])  # list_chunk[0:8] is 'LIST'+size
            logging.info("WAV: LIST/INFO chunk inserted.")
        if id3_bytes:
            clean_bytes = insert_chunk_before_data(clean_bytes, b"id3 ", id3_bytes)
            logging.info("WAV: 'id3 ' chunk (ID3v2.3 with APIC + textual frames) inserted.")
        return clean_bytes
    except Exception as e:
        logging.error("WAV: failed to insert metadata chunks: %s", e)
        return None


def finalize_wav_with_metadata(temp_path: Path, image_url: Optional[str], metadata_map: dict) -> bool:
    """
    High-level helper used by processor.process_single_file:
    - read bytes from temp_path
    - determine candidate bytes and rebuild a clean WAV
    - build LIST chunk and ID3 bytes using tag_utils builders
    - insert chunks and write back to temp_path
    """
    try:
        orig_bytes = Path(temp_path).read_bytes()
    except Exception as e:
        logging.error("WAV: could not read temp file bytes: %s", e)
        return False

    candidate_bytes = get_candidate_bytes_from_wav(orig_bytes)
    clean_bytes = rebuild_clean_wav(candidate_bytes)
    if clean_bytes is None:
        return False

    # build list chunk and id3 bytes
    list_chunk = build_info_list_chunk(metadata_map)
    image_bytes = None
    image_mime = None
    if image_url:
        got = download_image_bytes(image_url)
        if got:
            image_bytes, image_mime = got
    id3_bytes = build_id3_bytes_for_wav(image_bytes, image_mime, metadata_map)

    # Insert chunks
    final_bytes = apply_metadata_chunks_to_wav(clean_bytes, list_chunk, id3_bytes)
    if final_bytes is None:
        return False

    try:
        Path(temp_path).write_bytes(final_bytes)
    except Exception as e:
        logging.error("Failed writing final WAV bytes to temp file: %s", e)
        return False

    return True
