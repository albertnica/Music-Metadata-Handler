# Music Metadata Handler

**Short description**  
A Jupyter Notebook based tool to update metadata (title, artist, album, date, track, disc, genre and cover art) for audio files (`.flac`, `.mp3`, `.wav`) using the Spotify API. The notebook creates and edits a temporary copy for each file, sends the original file to the system trash, and replaces the original with the modified copy (original preserved in trash).

---

## Features
- Supports FLAC, MP3 (ID3), and WAV (ID3 chunk preferred, otherwise RIFF INFO).
- Infers artist/title from filename when tags are missing (supports `Artist - Title` and single-hyphen fallback; if no separator, the full filename stem is treated as the title).
- Uses ISRC search first when available, otherwise robust normalized containment searches via Spotify (fielded and plain queries).
- Option to only update genre (safe mode).
- Downloads album art and embeds it for FLAC/MP3 (skips embedding for WAV by default due to inconsistent player support).
- Processes files ordered by *creation date* when the filesystem exposes it; falls back to modification date where creation time is unavailable.
- Works interactively inside a Jupyter Notebook (recommended for stepwise testing and safe runs on batches).

---

## Requirements
- Python 3.8+ (recommended)
- Python packages:
  - `requests`
  - `mutagen`
  - `send2trash`

**Install via pip**
```bash
pip install requests mutagen send2trash
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
  "music_path": "/path/to/your/music/folder"
}
```

* `client_id` & `client_secret`: Spotify API credentials (Client Credentials flow).
* `music_path`: path to the directory with the audio files to update.

**Note:** Keep `credentials.json` secure and do not commit it to public repositories.

---

## Notebook usage overview

This project is intended to be executed inside a Jupyter Notebook. The notebook approach is recommended because:

* It allows incremental testing on small batches before applying changes to the entire library.
* You can inspect logs and intermediate results in cells before proceeding.
* You can modify flags directly in a configuration cell and rerun only the relevant parts.

### Typical notebook cell layout

1. **Configuration cell** — set constants such as `CREDENTIALS_PATH`, `RECURSIVE`, `PROCESS_TOP_X`, `UPDATE_ONLY_GENRE`, `OVERWRITE_TITLE_ARTIST_OR_ALBUM`, `PRINT_SEARCH_INFO`, `SEARCH_CANDIDATE_LIMIT`, and `MARKET`.
2. **Imports & helper functions** — `mutagen`, `requests`, `send2trash`, and utility functions remain as modular cells.
3. **Spotify authentication cell** — obtains a client credentials token from Spotify.
4. **File discovery cell** — collects files from `music_path` and sorts them by creation time (with fallback).
5. **Processing / main loop cell** — iterate files and call the `overwrite_metadata_with_spotify` logic.
6. **Summary / reporting cell** — prints final counters and any errors to examine.

**Running the notebook**

* Open the notebook with Jupyter:

```bash
jupyter notebook
# or for Jupyter Lab:
jupyter lab
```

* Edit the configuration cell to point to your `credentials.json` and set flags.
* Run the configuration and helper cells first, then the authentication and main processing cells.
* Start with `PROCESS_TOP_X = 10` or `UPDATE_ONLY_GENRE = 1` to test safely before scaling.

---

## Configuration options explained

* `RECURSIVE` (`True`/`False`): search for files recursively in subfolders.
* `PROCESS_TOP_X` (int): number of files to process in this run. Useful to test with small batches.
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

## To do (known problems)

* Problem with some .WAV not being correctly handled.
* Add metada just with the filename.
* Cell segmentation.

---

## Additional notes

* You can adapt the notebook to produce a `requirements.txt`, log file output to disk, or CSV report of changes applied.
* If desired, a cell can be added to print which timestamp (birth/ctime/mtime) was used for each file to audit ordering decisions.

---
