"""
app.py — TikTok → MP4 downloader with verbose logging for Render.

Every log line is flushed to stdout immediately so it shows up in
Render's log viewer in real time.

Endpoints:
    GET  /                      -> UI
    POST /api/fetch             -> fetch a TikTok, save MP4
    GET  /download/<filename>   -> serve saved MP4
    GET  /api/logs              -> live log lines for the UI
    GET  /healthz               -> health check for Render
"""

import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Enable Playwright's own debug logging BEFORE importing it.
# Streams to stderr -> captured by Render.
# Set to "pw:api" for every action, or "pw:browser" for just browser events.
os.environ.setdefault("DEBUG", "pw:api")

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
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | [%(request_id)s] | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_request_id: ContextVar[str] = ContextVar("request_id", default="--------")


class _RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record."""
    def filter(self, record):
        record.request_id = _request_id.get()
        return True


class _FlushingStreamHandler(logging.StreamHandler):
    """
    StreamHandler that flushes after every emit.
    Required on Render — stdout is buffered when not attached to a TTY,
    so logs otherwise sit in a 4KB buffer and appear 'delayed'.
    """
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> logging.Logger:
    """Configure root logger with a flushing stdout handler + optional file."""
    root = logging.getLogger()
    root.setLevel(level)

    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    rid_filter = _RequestIdFilter()

    console = _FlushingStreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(rid_filter)
    root.addHandler(console)

    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(formatter)
            fh.addFilter(rid_filter)
            root.addHandler(fh)
        except Exception as e:
            # Don't crash startup if the filesystem is read-only
            print(f"[warn] Could not create file log {log_file}: {e}", file=sys.stderr)

    # Quiet noisy libraries
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

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
        "%(asctime)s | %(levelname)-7s | [%(request_id)s] | %(message)s",
        datefmt="%H:%M:%S",
    ))
    h.addFilter(_RequestIdFilter())
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
    source_url: str
    filename: str
    saved_path: str
    size_bytes: int
    remote_url: str


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
# Fetcher
# ===========================================================================
def fetch_tiktok_video(tiktok_url: str, cfg: FetchConfig | None = None) -> FetchResult | None:
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
    """Single attempt. Every step is logged so Render shows full progress."""
    t0 = time.time()

    log.info("[step 1/8] Launching Chromium (headless=%s)", cfg.headless)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=cfg.headless,
            slow_mo=cfg.slow_mo_ms,
            args=[
                # Required inside Docker / Render
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--single-process",              # helps with limited memory
                # Clipboard permissions
                "--enable-features=ClipboardReadWrite",
                "--disable-features=ClipboardPermissionPrompt",
            ],
        )
        log.info("[step 1/8] Chromium launched in %.2fs", time.time() - t0)

        log.info("[step 2/8] Creating browser context")
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
        log.info("[step 2/8] Context + page ready")

        # ---- verbose telemetry -------------------------------------------
        page.on("console", lambda msg: log.info("[browser console] %s: %s",
                                                msg.type, msg.text))
        page.on("pageerror", lambda err: log.warning("[page error] %s", err))

        def on_request_failed(req):
            if "/api/promo-events" in req.url:
                return
            log.info("[request failed] %s %s", req.method, req.url)
        page.on("requestfailed", on_request_failed)

        def on_response(resp):
            if "/api/download/file" in resp.url:
                log.info("[api response] %s %s", resp.status, resp.url)
        page.on("response", on_response)

        page.on("download", lambda d: log.info(
            "[download event] suggested=%s url=%s",
            d.suggested_filename, d.url))

        def on_dialog(dialog):
            log.info("[dialog] %s: %s — accepting", dialog.type, dialog.message)
            try:
                dialog.accept()
            except Exception:
                log.debug("dialog.accept() raised", exc_info=True)
        page.on("dialog", on_dialog)

        try:
            # ---- 3. navigate ---------------------------------------------
            log.info("[step 3/8] Navigating to %s", TIKTOK_TOOL_URL)
            t_nav = time.time()
            page.goto(TIKTOK_TOOL_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            log.info("[step 3/8] Now on: %s (%.2fs)",
                     page.url, time.time() - t_nav)

            # ---- 4. fill textbox -----------------------------------------
            log.info("[step 4/8] Filling TikTok URL into textbox")
            url_box = page.get_by_role("textbox", name="Paste TikTok video link")
            url_box.wait_for(state="visible", timeout=15_000)
            url_box.click()
            url_box.fill(tiktok_url)
            log.info("[step 4/8] Textbox filled")

            # ---- 5. click Get Video --------------------------------------
            log.info("[step 5/8] Clicking 'Get Video'")
            t_get = time.time()
            get_btn = page.get_by_role("button", name="Get Video")
            get_btn.wait_for(state="visible", timeout=cfg.get_video_timeout_ms)
            get_btn.click()
            log.info("[step 5/8] 'Get Video' clicked")

            # ---- 6. wait for Save Video ----------------------------------
            log.info("[step 6/8] Waiting for 'Save Video' button (timeout=%dms)",
                     cfg.save_video_timeout_ms)
            save_btn = page.get_by_role("button", name="Save Video")
            save_btn.wait_for(state="visible", timeout=cfg.save_video_timeout_ms)
            log.info("[step 6/8] 'Save Video' visible after %.2fs",
                     time.time() - t_get)

            # ---- 7. click Save Video -> capture download -----------------
            log.info("[step 7/8] Clicking 'Save Video' to capture download")
            t_dl = time.time()
            with page.expect_download(timeout=cfg.download_timeout_ms) as dl_info:
                save_btn.click()

            download = dl_info.value
            download_url = download.url
            api_fname = _filename_from_api_url(download_url)
            suggested = api_fname or download.suggested_filename
            safe_name = _sanitize_filename(suggested or "tiktok_video.mp4")

            log.info("[step 7/8] Download intercepted after %.2fs",
                     time.time() - t_dl)
            log.info("           remote_url: %s", download_url)
            log.info("           filename:   %s", safe_name)

            # ---- 8. fetch bytes via page.request (same session) ----------
            log.info("[step 8/8] Fetching bytes via page.request (same session)")
            t_fetch = time.time()
            resp = page.request.get(download_url)

            if not resp.ok:
                body_preview = resp.text()[:300]
                log.error("[step 8/8] page.request failed: HTTP %s — %s",
                          resp.status, body_preview)
                raise RuntimeError(
                    f"Download failed: HTTP {resp.status} — {body_preview}")

            body = resp.body()
            log.info("[step 8/8] Got %s bytes in %.2fs",
                     len(body), time.time() - t_fetch)

            # ---- save to disk --------------------------------------------
            save_path = _unique_path(cfg.download_dir, safe_name)
            with open(save_path, "wb") as f:
                f.write(body)

            size = os.path.getsize(save_path)
            if size == 0:
                raise RuntimeError(f"Downloaded file is empty: {save_path}")

            log.info("✅ Saved (%s): %s", _human_size(size), save_path)
            log.info("Total elapsed: %.2fs", time.time() - t0)

            return FetchResult(
                source_url=tiktok_url,
                filename=os.path.basename(save_path),
                saved_path=save_path,
                size_bytes=size,
                remote_url=download_url,
            )

        finally:
            log.info("Closing browser context")
            try:
                context.close()
            except Exception:
                log.debug("context.close() raised, ignoring", exc_info=True)
            try:
                browser.close()
            except Exception:
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

FETCH_LOCK = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    rid = uuid.uuid4().hex[:8]
    _request_id.set(rid)

    data = request.get_json(silent=True) or {}
    tiktok_url = (data.get("url") or "").strip()

    log.info(">>> INCOMING REQUEST %s: %s", rid, tiktok_url)

    if not tiktok_url:
        log.warning("Missing url parameter")
        return jsonify(ok=False, error="Missing 'url' in request body"), 400
    if "tiktok.com" not in tiktok_url:
        log.warning("Not a TikTok URL")
        return jsonify(ok=False, error="That doesn't look like a TikTok URL"), 400

    with FETCH_LOCK:
        log.info("Lock acquired, starting fetch")
        result = fetch_tiktok_video(tiktok_url, FETCH_CFG)

    if not result:
        log.error("<<< REQUEST %s FAILED", rid)
        return jsonify(ok=False, error="Fetch failed. Check the server log."), 502

    own_url = f"/download/{result.filename}"
    payload = asdict(result)
    payload["ok"] = True
    payload["download_url"] = own_url

    log.info("<<< REQUEST %s DONE: %s", rid, own_url)
    return jsonify(payload)


@app.route("/download/<path:filename>")
def download_file(filename):
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
# Bootstrap logging at import time so Gunicorn picks it up
# ===========================================================================
# When running under Gunicorn, `__main__` is never executed, so we have to
# configure logging at module import time (i.e. when the worker imports app.py).
setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    log_file=os.getenv("LOG_FILE", "./logs/clipssaver.log"),
)
_install_buffer_handler()

# Ensure download dir exists at startup
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
except Exception:
    pass

log.info("=" * 72)
log.info("Flask app initialized")
log.info("Download dir: %s", DOWNLOAD_DIR)
log.info("Log level: %s", os.getenv("LOG_LEVEL", "INFO"))
log.info("Playwright DEBUG: %s", os.getenv("DEBUG", "(unset)"))
log.info("=" * 72)


# ===========================================================================
# Local dev entry point
# ===========================================================================
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)