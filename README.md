# Music Metadata Handler

**Short description**  
A Jupyter Notebook based tool to update metadata (title, artist, album, date, track, disc, genre and cover art) for audio files (`.flac`, `.mp3`, `.wav`) using the Spotify API. The notebook creates and edits a temporary copy for each file, sends the original file to the system trash (Recycle Bin), and replaces the original with the modified copy. For WAV support, the script writes both RIFF `LIST/INFO` tags **and** an `id3` chunk (ID3v2.3 with APIC + textual frames encoded in UTF-16) to maximize compatibility with tag editors such as MP3tag.

**Disclaimer:** Windows Explorer may not display WAV metadata natively. Use a dedicated tag editor such as **Mp3tag** to view and verify WAV metadata. It is recommended to convert the music files to **FLAC** and work with FLACs for long-term tagging and library management when possible.

**MP3 bug:** If you do not see MP3 metadata on Windows Explorer after running the program, open all the files processed with **Mp3tag**, select them (**Ctrl + Alt**) and save (**Ctrl + S**).

---

## Features
- Supports FLAC, MP3 (ID3), and WAV (ID3 chunk preferred, otherwise RIFF INFO).
- Infers artist/title from filename when tags are missing (supports `Artist - Title` or `Title - Artist`).
- Uses ISRC search first when available, otherwise robust normalized containment searches via Spotify (fielded and plain queries).
- Option to only update genre.
- Downloads album art and embeds it for FLAC/MP3/WAV.
- Processes files ordered by *creation date* when the filesystem exposes it; falls back to modification date where creation time is unavailable.
- Works interactively inside a Jupyter Notebook (recommended for stepwise testing and safe runs on batches).

---

## Requirements
- Python 3.8+ (recommended)
- Python packages:

```bash
pip install -r requirements.txt
````

**Optional**

* A virtual environment is recommended to isolate dependencies:

```bash
python -m venv venv
source venv/bin/activate   # macOS/Linux
venv\Scripts\activate      # Windows
```

---

## Credentials

Place a `credentials.json` file in the same folder as the notebook with the following minimal structure:

```json
{
  "client_id": "YOUR_SPOTIFY_CLIENT_ID",
  "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
  "music_path": "C:/path/to/your/music/folder"
}
```

* `client_id` & `client_secret`: Spotify API credentials (Client Credentials flow).
* `music_path`: path to the directory with the audio files to update.

**Note:** Keep `credentials.json` secure and do not commit it to public repositories.

---

## Notebook usage overview

Mi Cuchurrufleto — Run this project inside a Jupyter Notebook; if you’re new, open the `.ipynb` in Visual Studio Code, choose the Python/virtualenv kernel with the project's dependencies installed, click **Run All**, and collapse/expand cells via the cell toolbar (ellipsis) on each cell.

### Typical notebook cell layout

1. **Configuration cell** — Put all imports, constants and user-editable flags (`from ... import ...`, `CREDENTIALS_PATH`, `RECURSIVE`, `PROCESS_TOP_X`, endpoint URLs, `REQUEST_TIMEOUT`, `OVERWRITE_TITLE_ARTIST_OR_ALBUM`, `UPDATE_ONLY_GENRE`, `PRINT_SEARCH_INFO`, `SEARCH_CANDIDATE_LIMIT`, `MARKET`, and `logging.basicConfig`).
   **Justification:** Centralizing configuration and imports in one cell makes it trivial to change runtime behavior and ensures the selected kernel/environment has the required packages before running any logic cells.
2. **MUSIC UTILITIES SECTION** — Include filename parsing and filesystem helpers (`_FILENAME_SPLIT_RE`, `infer_artist_title_from_filename`, `unique_temp_copy`, `send_original_to_trash`).
   **Justification:** These are pure local utilities (no network). Keeping them isolated allows quick unit tests on filename inference and temp-copy behavior without invoking Spotify or tag-writing logic.
3. **SEARCH UTILITIES SECTION** — Place all normalization and token-matching helpers (`_strip_parentheses_with_feat`, `_extract_remixer_tokens_from_title`, `_normalize_text_basic`, `_normalize_artist_for_search`, `_normalize_title_for_search`, `_tokens`, `_tokens_in_candidate`, `_build_sanitized_query`).
   **Justification:** Search normalization is core to match correctness; grouping it makes it easy to tweak sanitization rules and re-run only the search logic during debugging.
4. **SPOTIFY CLIENT / SEARCH SECTION** — Add Spotify HTTP wrappers and search logic (`get_spotify_token`, `spotifysearch`, `spotify_get_artist_albums`, `spotify_get_album_tracks`, `spotify_find_best_match`).
   **Justification:** Network/auth logic should be separated so you can refresh tokens or re-run queries independently and inspect raw Spotify responses without touching file I/O.
5. **TAG WRITING / FORMAT-SPECIFIC HANDLERS** — Put format-specific read/write functions (`download_image_bytes`, `get_artist_genres`, `remove_existing_pictures_generic`, `set_genre_on_audio`, `add_picture_to_audio`).
   **Justification:** Tag I/O is potentially destructive; isolating these functions lets you test them safely on single sample files before running bulk operations.
6. **CORE: update metadata for a single file (handles FLAC/MP3/WAV)** — Include the worker function `overwrite_metadata_with_spotify(file_path, token)` (the full read → search → write flow for one file).
   **Justification:** Having the core worker in one cell makes single-file manual invocation easy (e.g., call it on a test path), enabling iterative verification of behavior before mass processing.
7. **FILE ITERATION + MAIN** — Add `iter_audio_files`, `get_creation_time`, the orchestration `main()` (load credentials, obtain token, build `paths`, sort by creation time fallback to mtime, loop and call worker), and the `if __name__ == "__main__": main()` guard.
   **Justification:** This cell runs the end-to-end pipeline; keep it last so all helpers and network functions are defined. It’s the only cell you need to modify minimally (e.g., `PROCESS_TOP_X`) to control a full run.

---

## Configuration options explained

* `RECURSIVE` (`True`/`False`): search for files recursively in subfolders.
* `FILENAME_PARSE_MODE` (`1`/`0`): if 1, it considers `Title - Artist` filename format; if 0, it considers `Artist - Title` filename format.
* `PROCESS_TOP_X` (int): number of files to process in this run (sorted by creation date). Useful to test with small batches.
* `OVERWRITE_TITLE_ARTIST_OR_ALBUM` (`1`/`0`): if 1, overwrite title/artist/album using Spotify results; if 0, preserve existing tags.
* `UPDATE_ONLY_GENRE` (`1`/`0`): if 1, only update genre tags (safe mode).
* `PRINT_SEARCH_INFO` (`1`/`0`): enable extended debugging/info logs on how queries and matches were performed.
* `SEARCH_CANDIDATE_LIMIT` (int): how many Spotify search candidates to consider per file.
* `MARKET` (string or `None`): pass e.g. `"US"` or `"ES"` to restrict regional results.

**Recommended safe workflow**

1. Set `PROCESS_TOP_X = 5` and `PRINT_SEARCH_INFO = 1`.
2. Run the notebook cells and inspect the printed matching decisions.
3. If outcomes look good, increase `PROCESS_TOP_X` or set to a larger number to process more files.

---

## How it works

1. **Read tags**: read current metadata from the file using Mutagen.
2. **Fallback to filename**: if artist/title missing, try to infer from filename.
3. **Search Spotify**: try ISRC search; else attempt normalized containment search (fielded and plain queries).
4. **Select candidate**: choose the first candidate that satisfies token containment checks.
5. **Get metadata & cover**: extract title, artist, album, track/disc numbers, date, image URL, and optionally genres (artist-level if available).
6. **Write to a temp file**: copy original to a uniquely-named `.tmp` file, write metadata to the temp file.
7. **Replace original safely**: send original to trash (via `send2trash`) and move temp file to original path.

---

## Logging and debug information

* Basic info logs are printed via Python's `logging` module.
* If `PRINT_SEARCH_INFO = 1`, the notebook prints:

  * Sanitized search input,
  * Each constructed query,
  * Candidate titles/artists/albums with ACCEPTED/REJECTED decisions,
  * Token checks and reason for acceptance.
* Use these logs to fine-tune normalization or to detect corner-case titles (live versions, remixes, etc.).

---

## Platform notes & file timestamps

* The notebook sorts files by *creation/birth time* when the filesystem exposes it (`st_birthtime` on some systems).
* On Windows, `st_ctime` is used as creation time.
* On Linux filesystems that do not expose birth time, the code falls back to `st_mtime` (modification time).
* If you need guaranteed birthtime on Linux, additional platform-specific calls would be required (not included by default).

---

## Common issues & troubleshooting

* **Spotify authentication errors (401)**: check `client_id` and `client_secret` and ensure your system time is correct.
* **No matches found**: enable `PRINT_SEARCH_INFO` to inspect sanitization and possible mismatches (remixes, alternate titles).
* **Cover art not embedded for WAV**: many players do not reliably support embedded images in WAV; consider converting to FLAC/MP3 if art embedding is critical.
* **Files not changed**: ensure the notebook runs with adequate file permissions and that `music_path` is correct.
* **Accidental deletes**: originals are moved to the system trash; verify trash contents to restore if needed.

---

## Safety & best practices

* **Always test on a small subset** with `PROCESS_TOP_X` before applying to the entire library.
* **Keep backups** of irreplaceable music files.
* **Use `UPDATE_ONLY_GENRE = 1`** as a minimally-invasive first pass.
* **Keep credentials.json private**.

---

## Additional notes

* You can adapt the notebook to produce a `requirements.txt`, log file output to disk, or CSV report of changes applied.
* If desired, a cell can be added to print which timestamp (birth/ctime/mtime) was used for each file to audit ordering decisions.

---