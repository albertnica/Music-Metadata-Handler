# Music Metadata Handler

A command-line and library tool to update audio file metadata (title, artist, album, date, track, disc, genre and cover art) for FLAC, MP3 and WAV files using the Spotify Web API. The project provides:

- A CLI entrypoint: `main.py` (recommended for scripted/batch runs).
- A modular Python package under `modules/` providing the implementation (search, tag I/O, WAV helpers, filename utils).

This README documents installation, configuration, CLI usage and module responsibilities so you can use the tool as a script or import the modules in your own workflows.

## Features
- Supports FLAC, MP3 (ID3) and WAV (LIST/INFO + ID3 chunk where applicable).
- Infers artist/title from filename when tags are missing (supports `Artist - Title` and `Title - Artist` modes).
- Uses ISRC search first when available; otherwise performs normalized, token-based Spotify searches.
- Optionally only update genre (safe mode).
- Downloads and embeds album art for supported formats.
- Processes files ordered by creation time (falls back to modification time).

## Installation

1. Create and activate a virtual environment (Windows only, recommended):

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

## Configuration (`config.py`) and credentials

The repository expects a local `config.py` with runtime flags and endpoint constants — see `config.py` for defaults. The program loads `credentials.json` which must contain your Spotify credentials and optionally a `music_path`. Example `credentials.json`:

```json
{
  "client_id": "YOUR_SPOTIFY_CLIENT_ID",
  "client_secret": "YOUR_SPOTIFY_CLIENT_SECRET",
  "music_path": "C:/path/to/your/music/folder"
}
```

Important config flags in `config.py`:
- `RECURSIVE` (bool): whether to scan subfolders.
- `PROCESS_TOP_X` (int): limit number of files to process in one run.
- `OVERWRITE_TITLE_ARTIST_OR_ALBUM` (0|1): whether to overwrite title/artist/album fields.
- `UPDATE_ONLY_GENRE` (0|1): if 1, only update genre tags.
- `PRINT_SEARCH_INFO` (0|1): enable verbose search/debug logs.
- `SEARCH_CANDIDATE_LIMIT` (int): how many Spotify candidates to consider per file.
- Endpoint URLs and timeouts are also defined in `config.py`.

## CLI usage (main.py)

`main.py` is the recommended entrypoint for non-interactive runs. Example:

```bash
python main.py --music-path "C:/Music" --process-top-x 20 --recursive
```

Available flags (short explanation):
- `--music-path <path>`: override `music_path` from `credentials.json`.
- `--process-top-x <int>`: number of files to process in this run (overrides `config.PROCESS_TOP_X`).
- `--recursive`: scan subfolders (flag; present => True, absent => False).
- `--overwrite-taa`: overwrite title/artist/album fields when writing (flag; present => True).
- `--update-only-genre`: only update genre tags (flag; present => True).

## Modules overview

- `modules/processor.py`: high-level single-file processing (read tags, search Spotify, build metadata map, write tags and images).
- `modules/spotify_client.py`: Spotify token and HTTP wrappers, and the matching/search helpers.
- `modules/tag_utils.py`: image download, genre lookup and format-specific tag helpers (ID3/FLAC/RIFF).
- `modules/wav_utils.py`: WAV rebuilding and chunk insertion helpers (LIST/INFO and `id3 ` insertion).
- `modules/filename_utils.py`: filename parsing, safe temp-copy helpers and sending originals to trash.
- `modules/search_utils.py`: normalization, token extraction and query-building utilities.
- `modules/core.py`: compatibility shims and file iteration helpers (`iter_audio_files`, `get_creation_time`).

The modules import constants from `config.py` and will raise an import-time error if required constants or `config.py` are missing.

## Troubleshooting

- Spotify 401 errors: check `client_id` / `client_secret` and system clock.
- No matches: enable `PRINT_SEARCH_INFO` to review sanitization and token checks.
- WAV cover art support is limited in many players; prefer FLAC for long-term tagging.
- Originals are sent to system trash — check your recycle bin to restore if needed.

## License

See `LICENSE` in the repository root.