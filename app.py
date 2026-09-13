"""
app.py — TikTok → MP4 downloader with web UI.

Flow:
    User pastes TikTok URL in browser
        ↓
    Server runs headless Chromium via Playwright
        ↓
    Fills clipssaver.com form, clicks Get Video → Save Video
        ↓
    Downloads bytes using page.request (same session) → ./downloads/
        ↓
    Returns both URLs to the browser:
        - download_url  -> /download/<filename>  (your server, stable)
        - remote_url    -> clipssaver.com/api/... (session-bound, for reference)

Files needed:
    app.py
    templates/index.html

Endpoints:
    GET  /                      -> UI
    POST /api/fetch             -> { url } -> { ok, filename, download_url, remote_url, source_url, size_bytes }
    GET  /download/<filename>   -> serves the MP4 as attachment
    GET  /api/logs              -> live log lines
"""

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import (
    Flask, request, jsonify, render_template,
    send_from_directory, abort,
)

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)


# ===========================================================================
# Logging
# ===========================================================================
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)

    return logging.getLogger("clipssaver_fetcher")


log = logging.getLogger("clipssaver_fetcher")


# ===========================================================================
# In-memory log buffer for the UI
# ===========================================================================
LOG_BUFFER: deque[str] = deque(maxlen=500)
LOG_LOCK = threading.Lock()


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        with LOG_LOCK:
            LOG_BUFFER.append(msg)


def _install_buffer_handler():
    root = logging.getLogger()
    if any(isinstance(h, _BufferHandler) for h in root.handlers):
        return
    h = _BufferHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(h)


# ===========================================================================
# Config
# ===========================================================================
DOWNLOAD_DIR = os.path.abspath("./downloads")

@dataclass
class FetchConfig:
    download_dir: str = DOWNLOAD_DIR
    headless: bool = True
    page_timeout_ms: int = 45_000
    get_video_timeout_ms: int = 60_000
    save_video_timeout_ms: int = 90_000
    download_timeout_ms: int = 120_000
    max_retries: int = 2
    retry_backoff_s: float = 2.0
    slow_mo_ms: int = 0


@dataclass
class FetchResult:
    source_url: str        # original TikTok URL
    filename: str          # saved filename
    saved_path: str        # absolute local path
    size_bytes: int        # file size
    remote_url: str        # clipssaver.com/api/download/file?... (session-bound)


TIKTOK_TOOL_URL = "https://clipssaver.com/tiktok-video-downloader"


# ===========================================================================
# Helpers
# ===========================================================================
def _sanitize_filename(name: str, fallback: str = "tiktok_video.mp4") -> str:
    name = (name or "").strip() or fallback
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    stem, dot, ext = name.rpartition(".")
    if len(name) > 180:
        name = (stem[:170] + dot + ext) if dot else name[:180]
    return name


def _unique_path(directory: str, filename: str) -> str:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return str(candidate)
    stem, ext = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem} ({i}){ext}"
        if not candidate.exists():
            return str(candidate)
        i += 1


def _filename_from_api_url(url: str) -> str | None:
    try:
        qs = parse_qs(urlparse(url).query)
        return qs.get("filename", [None])[0]
    except Exception:
        return None


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# ===========================================================================
# Fetcher — the core
# ===========================================================================
def fetch_tiktok_video(tiktok_url: str, cfg: FetchConfig | None = None) -> FetchResult | None:
    """
    Run clipssaver end-to-end and save the MP4 to disk.
    Returns FetchResult on success, None on failure.
    """
    cfg = cfg or FetchConfig()
    os.makedirs(cfg.download_dir, exist_ok=True)

    log.info("=" * 72)
    log.info("Starting fetch for: %s", tiktok_url)
    log.info("Download dir: %s", cfg.download_dir)

    attempt = 0
    last_error: Exception | None = None

    while attempt < cfg.max_retries:
        attempt += 1
        log.info("--- Attempt %d/%d ---", attempt, cfg.max_retries)
        try:
            return _fetch_once(tiktok_url, cfg)
        except PlaywrightTimeoutError as e:
            last_error = e
            log.warning("Timeout on attempt %d: %s", attempt, e)
        except PlaywrightError as e:
            last_error = e
            log.warning("Playwright error on attempt %d: %s", attempt, e)
        except Exception as e:  # noqa: BLE001
            last_error = e
            log.exception("Unexpected error on attempt %d: %s", attempt, e)

        if attempt < cfg.max_retries:
            backoff = cfg.retry_backoff_s * attempt
            log.info("Retrying in %.1fs...", backoff)
            time.sleep(backoff)

    log.error("All %d attempts failed for %s", cfg.max_retries, tiktok_url)
    if last_error:
        log.error("Last error: %s", last_error)
    return None


def _fetch_once(tiktok_url: str, cfg: FetchConfig) -> FetchResult:
    """Single attempt. Saves file to disk. Raises on failure."""
    with sync_playwright() as p:
        log.debug("Launching chromium (headless=%s)", cfg.headless)
        browser = p.chromium.launch(
            headless=cfg.headless,
            slow_mo=cfg.slow_mo_ms,
            args=[
                "--enable-features=ClipboardReadWrite",
                "--disable-features=ClipboardPermissionPrompt",
            ],
        )

        context = browser.new_context(
            accept_downloads=True,
            permissions=["clipboard-read", "clipboard-write"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(cfg.page_timeout_ms)

        page = context.new_page()

        page.on("console", lambda msg: log.debug("[browser] %s: %s",
                                                 msg.type, msg.text))
        page.on("pageerror", lambda err: log.warning("[page error] %s", err))

        def on_request_failed(req):
            if "/api/promo-events" in req.url:
                return
            log.debug("[request failed] %s %s", req.method, req.url)
        page.on("requestfailed", on_request_failed)

        page.on("download", lambda d: log.info(
            "[download event] suggested=%s url=%s",
            d.suggested_filename, d.url))

        def on_response(resp):
            if "/api/download/file" in resp.url:
                log.info("[api response] %s %s", resp.status, resp.url)
        page.on("response", on_response)

        def on_dialog(dialog):
            log.info("[dialog] %s: %s — accepting", dialog.type, dialog.message)
            try:
                dialog.accept()
            except Exception:
                log.debug("dialog.accept() raised", exc_info=True)
        page.on("dialog", on_dialog)

        try:
            # 1. Navigate to tool page
            log.info("Navigating to %s", TIKTOK_TOOL_URL)
            page.goto(TIKTOK_TOOL_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            log.info("Now on: %s", page.url)

            # 2. Fill textbox
            log.info("Filling TikTok URL into textbox")
            url_box = page.get_by_role("textbox", name="Paste TikTok video link")
            url_box.wait_for(state="visible", timeout=15_000)
            url_box.click()
            url_box.fill(tiktok_url)

            # 3. Click "Get Video"
            log.info("Clicking 'Get Video'")
            get_btn = page.get_by_role("button", name="Get Video")
            get_btn.wait_for(state="visible", timeout=cfg.get_video_timeout_ms)
            get_btn.click()

            # 4. Wait for "Save Video"
            log.info("Waiting for 'Save Video' button (timeout=%dms)",
                     cfg.save_video_timeout_ms)
            save_btn = page.get_by_role("button", name="Save Video")
            save_btn.wait_for(state="visible", timeout=cfg.save_video_timeout_ms)
            log.info("'Save Video' is visible")

            # 5. Click "Save Video" -> intercept download
            log.info("Clicking 'Save Video' to capture download")
            with page.expect_download(timeout=cfg.download_timeout_ms) as dl_info:
                save_btn.click()

            download = dl_info.value
            download_url = download.url
            api_fname = _filename_from_api_url(download_url)
            suggested = api_fname or download.suggested_filename
            safe_name = _sanitize_filename(suggested or "tiktok_video.mp4")

            log.info("Download intercepted")
            log.info("  remote_url: %s", download_url)
            log.info("  filename:   %s", safe_name)

            # 6. Fetch bytes using the SAME browser session (page.request)
            #    The clipssaver URL is session-gated, so this is the only way.
            log.info("Fetching bytes via page.request (same session)")
            resp = page.request.get(download_url)

            if not resp.ok:
                body_preview = resp.text()[:300]
                log.error("page.request failed: HTTP %s — %s",
                          resp.status, body_preview)
                raise RuntimeError(
                    f"Download failed: HTTP {resp.status} — {body_preview}")

            body = resp.body()
            log.info("Got %s bytes from server", len(body))

            # 7. Save to disk
            save_path = _unique_path(cfg.download_dir, safe_name)
            with open(save_path, "wb") as f:
                f.write(body)

            size = os.path.getsize(save_path)
            if size == 0:
                raise RuntimeError(f"Downloaded file is empty: {save_path}")

            log.info("✅ Saved (%s): %s", _human_size(size), save_path)

            return FetchResult(
                source_url=tiktok_url,
                filename=os.path.basename(save_path),
                saved_path=save_path,
                size_bytes=size,
                remote_url=download_url,
            )

        finally:
            log.debug("Closing browser context")
            try:
                context.close()
            except Exception:  # noqa: BLE001
                log.debug("context.close() raised, ignoring", exc_info=True)
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                log.debug("browser.close() raised, ignoring", exc_info=True)


# ===========================================================================
# Flask app
# ===========================================================================
app = Flask(__name__)

FETCH_CFG = FetchConfig(
    download_dir=DOWNLOAD_DIR,
    headless=True,
    max_retries=2,
)

FETCH_LOCK = threading.Lock()   # serialize — one browser at a time


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json(silent=True) or {}
    tiktok_url = (data.get("url") or "").strip()

    if not tiktok_url:
        return jsonify(ok=False, error="Missing 'url' in request body"), 400
    if "tiktok.com" not in tiktok_url:
        return jsonify(ok=False, error="That doesn't look like a TikTok URL"), 400

    log.info("UI request: fetch %s", tiktok_url)

    with FETCH_LOCK:
        result = fetch_tiktok_video(tiktok_url, FETCH_CFG)

    if not result:
        return jsonify(ok=False, error="Fetch failed. Check the server log."), 502

    # Build OUR server's download URL (stable, never expires)
    own_url = f"/download/{result.filename}"

    payload = asdict(result)
    payload["ok"] = True
    payload["download_url"] = own_url      # /download/<filename>
    # payload["remote_url"] is already in asdict(result)

    return jsonify(payload)


@app.route("/download/<path:filename>")
def download_file(filename):
    """Serve a saved MP4 as an attachment."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        abort(400)
    full = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.isfile(full):
        abort(404)
    return send_from_directory(
        DOWNLOAD_DIR, filename,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/api/logs")
def api_logs():
    with LOG_LOCK:
        lines = list(LOG_BUFFER)[-200:]
    return jsonify(lines=lines)


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    setup_logging(level=logging.INFO, log_file="./logs/clipssaver.log")
    _install_buffer_handler()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    log.info("Starting app on http://127.0.0.1:5000")
    log.info("Download dir: %s", DOWNLOAD_DIR)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)