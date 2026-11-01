"""
ui_server.py
FastAPI web application and local studio dashboard for Music Metadata Handler.

Why this module exists:
Provides an interactive graphical dashboard for inspecting local music directories,
filtering tracks by missing metadata criteria, selecting individual files for processing,
and streaming real-time pipeline execution logs via Server-Sent Events (SSE).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Literal
import webbrowser

import aiohttp
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

import config
from modules.core import LocalTrackInfo, PendingTagUpdate, ResolvedMetadata
from modules.filename_utils import infer_artist_title_from_filename
from modules.processor import commit_tags_to_file, extract_local_tags, match_track_via_browser
from modules.scanner import KNOWN_CRITERIA, scan_audio_library
from modules.spotify_client import SpotifyWebScraper, download_image_bytes

# Ensure valid stdout/stderr streams when launched windowless via pythonw.exe
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
elif sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
elif sys.stderr.encoding != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TEMPLATES_DIR = config.MAIN_DIR / "templates"
INDEX_HTML_PATH = TEMPLATES_DIR / "index.html"


# ---------------------------------------------------------------------------
# Pydantic V2 Schemas
# ---------------------------------------------------------------------------

class TrackItemSchema(BaseModel):
    """Pydantic V2 representation of an inspected audio file in the library."""
    file_path: str = Field(..., description="Absolute filesystem path to the audio file")
    filename: str = Field(..., description="Filename including extension")
    rel_path: str = Field(..., description="Relative path from scanned root directory")
    ext: str = Field(..., description="File extension (.flac or .mp3)")
    title: str = Field("", description="Existing embedded title tag")
    artist: str = Field("", description="Existing embedded artist tag")
    album: str = Field("", description="Existing embedded album tag")
    date: str = Field("", description="Existing embedded release date tag")
    genre: str = Field("", description="Existing embedded genre tag")
    has_cover: bool = Field(False, description="Whether cover art is embedded in the file")
    fn_artist: str = Field("", description="Artist parsed from filename")
    fn_title: str = Field("", description="Title parsed from filename")
    issues: list[str] = Field(default_factory=list, description="List of missing metadata flags")


class ScanRequest(BaseModel):
    """Request payload for initiating a directory metadata scan."""
    folder_path: str = Field(..., description="Target directory path to inspect")
    recursive: bool = Field(False, description="Whether to scan subdirectories recursively")
    criteria: list[str] = Field(
        default=["all"],
        description="Filter criteria: all, lack_cover, lack_title, lack_artist, lack_album, lack_date, lack_genre",
    )
    filename_parse_mode: int = Field(0, description="0 = Artist - Title, 1 = Title - Artist")


class ScanResponse(BaseModel):
    """Response payload containing scanned library results and health statistics."""
    success: bool = Field(True, description="Whether the scan completed successfully")
    folder_path: str = Field(..., description="Absolute path of the scanned directory")
    total_scanned: int = Field(..., description="Total audio files found on disk")
    total_matched: int = Field(..., description="Total tracks satisfying active filter criteria")
    flac_count: int = Field(..., description="Total FLAC files found")
    mp3_count: int = Field(..., description="Total MP3 files found")
    tracks: list[TrackItemSchema] = Field(default_factory=list, description="Track inventory")


class FixRequest(BaseModel):
    """Request payload to trigger metadata fixing on a list of selected files."""
    file_paths: list[str] = Field(..., min_length=1, description="List of absolute file paths to fix")
    overwrite_taa: bool = Field(True, description="Overwrite existing Title/Artist/Album tags")
    update_only_genre: bool = Field(False, description="Only update Genre tag and keep other tags untouched")
    scrape_album_details: bool = Field(True, description="Visit album page for release date and track numbers")
    filename_parse_mode: int = Field(0, description="0 = Artist - Title, 1 = Title - Artist")
    require_confirmation: bool = Field(True, description="Require user confirmation before overwriting audio files")


class CommitRequest(BaseModel):
    """Request payload for committing approved metadata updates to disk."""
    approved_paths: list[str] = Field(..., min_length=1, description="List of approved file paths to write")


class StagedReviewTrack(BaseModel):
    """Schema representing proposed metadata changes for user review prior to disk commit."""
    file_path: str
    filename: str
    ext: str
    original: dict[str, str]
    proposed: dict[str, Any]
    approved: bool = True


class StagedReviewResponse(BaseModel):
    """Response payload containing pending metadata updates awaiting user approval."""
    success: bool = True
    total: int
    tracks: list[StagedReviewTrack]


class SystemStatusResponse(BaseModel):
    """Response payload detailing server configuration and current pipeline task state."""
    status: str = Field("ready", description="Current engine state")
    is_running: bool = Field(False, description="Whether a background synchronization task is active")
    default_music_path: str = Field("", description="Default music path loaded from credentials.json")
    supported_extensions: list[str] = Field(default_factory=list, description="Supported audio formats")
    current_progress: dict[str, Any] = Field(default_factory=dict, description="Active task telemetry")


class ApiResponse(BaseModel):
    """Standardized mutation API envelope."""
    success: bool = Field(..., description="Execution status flag")
    message: str = Field(..., description="Informational message")


class DirectoryBrowseResponse(BaseModel):
    """Response payload containing chosen directory path."""
    success: bool = Field(..., description="Whether a directory was selected")
    path: str = Field("", description="Selected directory path")


# ---------------------------------------------------------------------------
# Background Task State & Event Broadcasting
# ---------------------------------------------------------------------------

class PipelineManager:
    """Coordinates active background metadata synchronization and SSE broadcasting."""

    def __init__(self) -> None:
        self.is_running: bool = False
        self.cancel_requested: bool = False
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.staged_updates: dict[str, PendingTagUpdate] = {}
        self.staged_reviews: list[dict[str, Any]] = []
        self.current_progress: dict[str, Any] = {
            "current": 0,
            "total": 0,
            "stage": "idle",
            "file": "",
            "updated": 0,
            "skipped": 0,
            "failed": 0,
        }

    def subscribe(self) -> asyncio.Queue[str]:
        """Subscribes an SSE connection to broadcasted execution events."""
        queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Removes an active SSE queue subscription."""
        self.subscribers.discard(queue)

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """Pushes an SSE event packet to all connected clients."""
        packet = f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        dead_queues: list[asyncio.Queue[str]] = []
        for q in self.subscribers:
            try:
                q.put_nowait(packet)
            except asyncio.QueueFull:
                dead_queues.append(q)
        for dq in dead_queues:
            self.subscribers.discard(dq)

    async def broadcast_log(self, text: str, level: str = "info") -> None:
        """Convenience method to stream a terminal-style log line."""
        await self.broadcast("log", {"message": text, "level": level})


pipeline_manager = PipelineManager()


# ---------------------------------------------------------------------------
# Helper: Native Windows Directory Picker
# ---------------------------------------------------------------------------

def _pick_directory_native(initial_dir: str | None = None) -> str:
    """
    Opens the Windows native folder browser dialog via Tkinter in a separate thread.
    Why: Provides zero-friction directory selection without requiring users to type paths.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(
            initialdir=initial_dir or os.path.expanduser("~"),
            title="Select Music Library Folder",
        )
        root.destroy()
        return folder or ""
    except Exception as exc:
        logging.debug("Native folder picker failed: %s", exc)
        return ""


def load_default_music_path() -> str:
    """Reads default library path from `credentials.json` if available, falling back to C:/."""
    if not config.CREDENTIALS_PATH.exists():
        return config.DEFAULT_BASE_PATH
    try:
        data = json.loads(config.CREDENTIALS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            val = str(data.get("music_path") or data.get("source_dir") or "").strip()
            return val or config.DEFAULT_BASE_PATH
    except Exception:
        pass
    return config.DEFAULT_BASE_PATH


# ---------------------------------------------------------------------------
# Execution Pipeline Worker
# ---------------------------------------------------------------------------

async def run_fix_pipeline_async(request: FixRequest) -> None:
    """
    Asynchronously executes the 3-stage metadata fixing pipeline for selected files.

    Why: Running inside an asyncio task keeps the FastAPI web server non-blocking while
    streaming progress bars and diagnostic logs to the browser dashboard.
    """
    pipeline_manager.is_running = True
    pipeline_manager.cancel_requested = False

    paths = [Path(p) for p in request.file_paths if Path(p).exists()]
    total_files = len(paths)

    pipeline_manager.current_progress = {
        "current": 0,
        "total": total_files,
        "stage": "stage1",
        "file": "",
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }

    await pipeline_manager.broadcast_log(
        f"[START] Beginning metadata repair for {total_files} selected track(s)...",
        "info",
    )

    # Apply temporary runtime overrides from request
    config.OVERWRITE_TITLE_ARTIST_OR_ALBUM = 1 if request.overwrite_taa else 0
    config.UPDATE_ONLY_GENRE = 1 if request.update_only_genre else 0
    config.SCRAPE_ALBUM_DETAILS = 1 if request.scrape_album_details else 0
    config.FILENAME_PARSE_MODE = request.filename_parse_mode

    runtime_cfg = config.RuntimeConfig.from_module()

    try:
        # -----------------------------------------------------
        # STAGE 1: Extract Local Tags
        # -----------------------------------------------------
        await pipeline_manager.broadcast_log(
            f"Stage 1/3: Reading local tags and filename heuristics ({total_files} files)...",
            "info",
        )
        local_data_list: list[LocalTrackInfo] = []

        for p in paths:
            if pipeline_manager.cancel_requested:
                await pipeline_manager.broadcast_log("[CANCELLED] Task cancelled by user.", "warning")
                return

            res = await asyncio.to_thread(extract_local_tags, p)
            if res is not None:
                local_data_list.append(res)
            else:
                pipeline_manager.current_progress["skipped"] += 1
                await pipeline_manager.broadcast_log(f"  [?] Insufficient metadata: {p.name}", "warning")

        if not local_data_list:
            await pipeline_manager.broadcast_log("No tracks qualified for search. Aborting.", "warning")
            return

        # -----------------------------------------------------
        # STAGE 2: Selenium Web Player Matching + CDN Cover Art
        # -----------------------------------------------------
        await pipeline_manager.broadcast_log(
            f"Stage 2/3: Searching Spotify Web Player via Chrome ({len(local_data_list)} tracks)...",
            "info",
        )
        pipeline_manager.current_progress["stage"] = "stage2"

        matched_items: list[tuple[LocalTrackInfo, ResolvedMetadata, str | None]] = []
        skipped_names: list[str] = []

        scraper = SpotifyWebScraper()
        try:
            await asyncio.to_thread(scraper.ensure_logged_in)

            for idx, track_info in enumerate(local_data_list, start=1):
                if pipeline_manager.cancel_requested:
                    await pipeline_manager.broadcast_log("[CANCELLED] Task cancelled by user.", "warning")
                    return

                pipeline_manager.current_progress["current"] = idx
                pipeline_manager.current_progress["file"] = track_info.file_path.name

                await pipeline_manager.broadcast(
                    "progress",
                    {
                        "current": idx,
                        "total": len(local_data_list),
                        "file": track_info.file_path.name,
                        "stage": "stage2",
                    },
                )

                await pipeline_manager.broadcast_log(
                    f"  [{idx}/{len(local_data_list)}] Searching Spotify: {track_info.file_path.name}",
                    "info",
                )

                match_res = await asyncio.to_thread(
                    match_track_via_browser,
                    scraper,
                    track_info,
                )

                if match_res is not None:
                    resolved_meta, image_url = match_res
                    matched_items.append((track_info, resolved_meta, image_url))
                    await pipeline_manager.broadcast_log(
                        f"     -> MATCH: '{resolved_meta.title}' by '{resolved_meta.artist}'",
                        "success",
                    )
                else:
                    skipped_names.append(track_info.file_path.name)
                    pipeline_manager.current_progress["skipped"] += 1
                    await pipeline_manager.broadcast_log(
                        f"     -> NO MATCH for '{track_info.file_path.name}'",
                        "warning",
                    )
        finally:
            await asyncio.to_thread(scraper.close)

        valid_updates: list[PendingTagUpdate] = []
        image_url_map: dict[str, str] = {}
        if matched_items:
            await pipeline_manager.broadcast_log(
                f"Downloading high-resolution cover art for {len(matched_items)} tracks...",
                "info",
            )
            for track, meta, img_url in matched_items:
                if img_url:
                    image_url_map[str(track.file_path)] = img_url

            semaphore = asyncio.Semaphore(runtime_cfg.max_concurrent_requests)
            async with aiohttp.ClientSession() as session:
                async def _download_cover(
                    track: LocalTrackInfo,
                    meta: ResolvedMetadata,
                    img_url: str | None,
                ) -> PendingTagUpdate:
                    img_bytes: bytes | None = None
                    mime: str | None = None
                    if img_url and not runtime_cfg.update_only_genre:
                        art = await download_image_bytes(session, img_url, semaphore=semaphore)
                        if art:
                            img_bytes, mime = art
                    return PendingTagUpdate(
                        file_path=track.file_path,
                        ext=track.ext,
                        metadata_map=meta,
                        image_bytes=img_bytes,
                        mime=mime,
                    )

                download_tasks = [
                    _download_cover(track, meta, img_url)
                    for track, meta, img_url in matched_items
                ]
                valid_updates = list(await asyncio.gather(*download_tasks))

        # Check if pre-commit confirmation is required by user
        if request.require_confirmation and valid_updates:
            pipeline_manager.staged_updates.clear()
            pipeline_manager.staged_reviews.clear()

            review_items: list[dict[str, Any]] = []
            for upd in valid_updates:
                f_str = str(upd.file_path)
                pipeline_manager.staged_updates[f_str] = upd
                t_orig = next((t for t in local_data_list if str(t.file_path) == f_str), None)
                m = upd.metadata_map

                review_items.append({
                    "file_path": f_str,
                    "filename": upd.file_path.name,
                    "ext": upd.ext,
                    "original": {
                        "title": (t_orig.id3_title or t_orig.fn_title or "") if t_orig else "",
                        "artist": (t_orig.id3_artist or t_orig.fn_artist or "") if t_orig else "",
                        "album": (t_orig.id3_album or "") if t_orig else "",
                    },
                    "proposed": {
                        "title": m.title,
                        "artist": m.artist,
                        "album": m.album,
                        "date": m.date,
                        "genre": m.genre,
                        "image_url": image_url_map.get(f_str) or "",
                        "has_artwork": bool(upd.image_bytes),
                    },
                    "approved": True,
                })

            pipeline_manager.staged_reviews = review_items
            pipeline_manager.is_running = False
            pipeline_manager.current_progress["stage"] = "review_pending"

            await pipeline_manager.broadcast("review_ready", {"tracks": review_items})
            await pipeline_manager.broadcast_log(
                f"[CONFIRMATION REQUIRED] {len(review_items)} track(s) ready for review. Please confirm changes before overwriting files.",
                "warning",
            )
            return

        # -----------------------------------------------------
        # STAGE 3: Commit Metadata to Disk
        # -----------------------------------------------------
        if valid_updates:
            await pipeline_manager.broadcast_log(
                f"Stage 3/3: Committing {len(valid_updates)} metadata updates to disk...",
                "info",
            )
            pipeline_manager.current_progress["stage"] = "stage3"

            for update_item in valid_updates:
                if pipeline_manager.cancel_requested:
                    await pipeline_manager.broadcast_log("[CANCELLED] Task cancelled by user.", "warning")
                    return

                success = await asyncio.to_thread(
                    commit_tags_to_file,
                    update_item,
                    output_dir=None,
                    source_root=None,
                )

                if success:
                    pipeline_manager.current_progress["updated"] += 1
                    await pipeline_manager.broadcast_log(
                        f"  [SAVED] {update_item.file_path.name}",
                        "success",
                    )
                else:
                    pipeline_manager.current_progress["failed"] += 1
                    await pipeline_manager.broadcast_log(
                        f"  [FAILED] {update_item.file_path.name}",
                        "error",
                    )

        summary = {
            "updated": pipeline_manager.current_progress["updated"],
            "skipped": pipeline_manager.current_progress["skipped"],
            "failed": pipeline_manager.current_progress["failed"],
            "total": total_files,
        }
        await pipeline_manager.broadcast("complete", summary)
        await pipeline_manager.broadcast_log(
            f"[COMPLETE] Updated: {summary['updated']} | Skipped: {summary['skipped']} | Failed: {summary['failed']}",
            "success",
        )

    except Exception as exc:
        logging.exception("Fatal error in pipeline runner: %s", exc)
        await pipeline_manager.broadcast_log(f"[FATAL ERROR] {exc}", "error")
        await pipeline_manager.broadcast("error", {"detail": str(exc)})

    finally:
        pipeline_manager.is_running = False
        pipeline_manager.current_progress["stage"] = "idle"


# ---------------------------------------------------------------------------
# FastAPI Lifespan & Application Setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manages application initialization and graceful shutdown."""
    default_path = load_default_music_path()
    logging.info("Music Metadata Studio initialized (Default path: %s)", default_path or "None")
    yield
    logging.info("Music Metadata Studio shutting down...")


app = FastAPI(
    title="Music Metadata Studio",
    description="Interactive Web Dashboard for local music metadata inspection and Spotify synchronization.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Centralizes unhandled exception formatting into standard JSON envelopes."""
    logging.error("Unhandled API error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "detail": str(exc)},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    """Serves the single-page application dashboard."""
    if not INDEX_HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="templates/index.html not found.")
    content = INDEX_HTML_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/api/status", response_model=SystemStatusResponse)
async def get_system_status() -> SystemStatusResponse:
    """Returns engine availability, configuration paths, and active task progress."""
    return SystemStatusResponse(
        status="running" if pipeline_manager.is_running else "ready",
        is_running=pipeline_manager.is_running,
        default_music_path=load_default_music_path(),
        supported_extensions=list(config.SUPPORTED_EXTENSIONS),
        current_progress=pipeline_manager.current_progress,
    )


@app.post("/api/browse-directory", response_model=DirectoryBrowseResponse)
async def browse_directory() -> DirectoryBrowseResponse:
    """Triggers the native Windows directory picker dialog."""
    default_dir = load_default_music_path()
    chosen_path = await asyncio.to_thread(_pick_directory_native, default_dir)
    return DirectoryBrowseResponse(
        success=bool(chosen_path),
        path=chosen_path,
    )


@app.post("/api/scan", response_model=ScanResponse)
async def scan_directory(request: ScanRequest) -> ScanResponse:
    """
    Performs a fast multithreaded scan of the requested directory, evaluating tag health.
    """
    target_path = Path(request.folder_path)
    if not target_path.exists() or not target_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Directory does not exist: {request.folder_path}",
        )

    config.FILENAME_PARSE_MODE = request.filename_parse_mode

    scan_data = await asyncio.to_thread(
        scan_audio_library,
        target_path,
        recursive=request.recursive,
        criteria=request.criteria,
    )

    track_items = [TrackItemSchema(**item) for item in scan_data["tracks"]]

    return ScanResponse(
        success=True,
        folder_path=scan_data["folder_path"],
        total_scanned=scan_data["total_scanned"],
        total_matched=scan_data["total_matched"],
        flac_count=scan_data["flac_count"],
        mp3_count=scan_data["mp3_count"],
        tracks=track_items,
    )


@app.post("/api/fix", response_model=ApiResponse)
async def start_fix_task(request: FixRequest) -> ApiResponse:
    """
    Launches background metadata repair for the submitted list of track paths.
    """
    if pipeline_manager.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A synchronization task is already in progress.",
        )

    asyncio.create_task(run_fix_pipeline_async(request))
    return ApiResponse(
        success=True,
        message=f"Started repair for {len(request.file_paths)} track(s).",
    )


@app.post("/api/commit", response_model=ApiResponse)
async def commit_staged_tracks(request: CommitRequest) -> ApiResponse:
    """
    Applies approved staged metadata updates atomically to disk files.

    Why: Gives the user full oversight to review and selectively confirm which audio files
    will be overwritten before any Vorbis Comment or ID3 frame is touched.
    """
    if pipeline_manager.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A synchronization task is already running.",
        )

    approved_set = set(request.approved_paths)
    to_commit: list[PendingTagUpdate] = [
        upd for p_str, upd in pipeline_manager.staged_updates.items()
        if p_str in approved_set
    ]

    if not to_commit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No matching staged tracks found for the approved file paths.",
        )

    async def _execute_commit() -> None:
        pipeline_manager.is_running = True
        pipeline_manager.cancel_requested = False
        pipeline_manager.current_progress["stage"] = "stage3"
        total = len(to_commit)

        await pipeline_manager.broadcast_log(
            f"[COMMIT] User confirmed! Writing tags to {total} audio file(s)...",
            "info",
        )

        updated = 0
        failed = 0

        for idx, update_item in enumerate(to_commit, start=1):
            if pipeline_manager.cancel_requested:
                await pipeline_manager.broadcast_log("[CANCELLED] Commit cancelled by user.", "warning")
                break

            success = await asyncio.to_thread(
                commit_tags_to_file,
                update_item,
                output_dir=None,
                source_root=None,
            )

            if success:
                updated += 1
                await pipeline_manager.broadcast_log(
                    f"  [SAVED {idx}/{total}] {update_item.file_path.name}",
                    "success",
                )
            else:
                failed += 1
                await pipeline_manager.broadcast_log(
                    f"  [FAILED {idx}/{total}] {update_item.file_path.name}",
                    "error",
                )

        pipeline_manager.staged_updates.clear()
        pipeline_manager.staged_reviews.clear()
        pipeline_manager.is_running = False
        pipeline_manager.current_progress["stage"] = "idle"

        summary = {
            "updated": updated,
            "skipped": 0,
            "failed": failed,
            "total": total,
        }
        await pipeline_manager.broadcast("complete", summary)
        await pipeline_manager.broadcast_log(
            f"[COMPLETE] Commit finished! Updated: {updated} | Failed: {failed}",
            "success",
        )

    asyncio.create_task(_execute_commit())
    return ApiResponse(
        success=True,
        message=f"Began committing {len(to_commit)} approved tracks to disk.",
    )


@app.post("/api/discard", response_model=ApiResponse)
async def discard_staged_tracks() -> ApiResponse:
    """Discards currently staged metadata updates without writing to disk."""
    count = len(pipeline_manager.staged_updates)
    pipeline_manager.staged_updates.clear()
    pipeline_manager.staged_reviews.clear()
    pipeline_manager.current_progress["stage"] = "idle"
    await pipeline_manager.broadcast_log(
        f"[DISCARDED] {count} staged track update(s) discarded. No disk files were modified.",
        "info",
    )
    return ApiResponse(success=True, message=f"Discarded {count} staged updates.")


@app.post("/api/cancel", response_model=ApiResponse)
async def cancel_fix_task() -> ApiResponse:
    """Requests cancellation of the currently active fixing task."""
    if not pipeline_manager.is_running:
        return ApiResponse(success=True, message="No task currently running.")

    pipeline_manager.cancel_requested = True
    return ApiResponse(success=True, message="Cancellation signal sent to pipeline runner.")


@app.post("/api/shutdown", response_model=ApiResponse)
async def shutdown_server() -> ApiResponse:
    """
    Halts any active synchronization task, releases socket handles, and terminates the server process.

    Why: Allows users running windowless instances via run_ui.bat (pythonw.exe) to cleanly
    stop the background server process directly from the web interface.
    """
    if pipeline_manager.is_running:
        pipeline_manager.cancel_requested = True
    loop = asyncio.get_running_loop()
    loop.call_later(0.4, lambda: os._exit(0))
    return ApiResponse(success=True, message="Music Metadata Studio server shutting down.")


@app.get("/api/stream")
async def event_stream() -> StreamingResponse:
    """Server-Sent Events endpoint streaming real-time console logs and progress updates."""
    queue = pipeline_manager.subscribe()

    async def _generator() -> AsyncIterator[str]:
        try:
            # Yield initial connection confirmation packet
            yield f"event: connected\ndata: {json.dumps({'message': 'SSE connected'})}\n\n"
            while True:
                packet = await queue.get()
                yield packet
        except asyncio.CancelledError:
            pass
        finally:
            pipeline_manager.unsubscribe(queue)

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def kill_process_on_port(target_port: int) -> None:
    """
    Terminates any existing orphaned process listening on target_port before binding.

    Why: Guarantees port availability upon launching or restarting the UI server,
    preventing socket bind errors (WinError 10048) caused by lingering instances.
    """
    import subprocess
    import time

    current_pid = os.getpid()
    killed_any = False

    if sys.platform == "win32":
        try:
            cmd = f'powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort {target_port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique"'
            out = subprocess.check_output(cmd, shell=True, text=True).strip()
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid != current_pid and pid > 0:
                        logging.info("Terminating existing process on port %d (PID %d)...", target_port, pid)
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
                        killed_any = True
        except Exception:
            pass

    if killed_any:
        time.sleep(0.8)


def run_server(host: str = "127.0.0.1", port: int = 8484, open_browser: bool = False) -> None:
    """Launches the Uvicorn web server hosting the dashboard."""
    kill_process_on_port(port)
    url = f"http://{host}:{port}"
    print(f"\n[SERVER] Music Metadata Studio running at {url}")
    print("[SERVER] Press Ctrl+C in console to terminate.\n")

    if open_browser:
        async def _open() -> None:
            await asyncio.sleep(1.2)
            webbrowser.open(url)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_open())
            else:
                loop.run_until_complete(_open())
        except Exception:
            pass

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Music Metadata Studio Web Dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host address")
    parser.add_argument("--port", type=int, default=8484, help="Bind port (default: 8484)")
    parser.add_argument("--open-browser", action="store_true", help="Automatically launch browser tab")
    cli_args = parser.parse_args()

    run_server(host=cli_args.host, port=cli_args.port, open_browser=cli_args.open_browser)

