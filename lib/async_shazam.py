"""
Shazam-based audio identification for radio streams.
Captures audio from a stream URL via ffmpeg and identifies it using ShazamIO.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time

import aiohttp
from shazamio import Shazam

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)

_LOGGER = logging.getLogger(__name__)

# Suppress noisy "skipping junk" / "invalid mpeg audio header" warnings from shazamio internals
for _name in ("shazamio", "pydub", "pydub.converter"):
    logging.getLogger(_name).setLevel(logging.ERROR)


class _ShazamNoiseFilter(logging.Filter):
    """Filter out shazamio's internal audio decoder noise from the root logger."""
    _JUNK = ("skipping junk", "invalid mpeg audio header", "format marker")
    def filter(self, record):
        msg = record.getMessage().lower()
        return not any(j in msg for j in self._JUNK)


def suppress_shazam_noise():
    """Add noise filter to all handlers on the root logger. Call after logging setup."""
    root = logging.getLogger()
    root.addFilter(_ShazamNoiseFilter())
    for handler in root.handlers:
        handler.addFilter(_ShazamNoiseFilter())

# Cache TTL constants
_CACHE_TTL_HIT = 180   # seconds to cache a successful identification
_CACHE_TTL_MISS = 60   # seconds to cache a failed identification (no match)
_CACHE_MAX_ENTRIES = 20

# User-editable stream URL override file (repo root, sibling of lib/)
_STREAM_URLS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stream_urls.json")

# TuneIn OPML endpoint for resolving station IDs to stream URLs
_TUNEIN_URL = "http://opml.radiotime.com/Tune.ashx?id={station_id}&render=json"

# Radio-Browser.info API for looking up station stream URLs by name
_RADIO_BROWSER_URL = "https://de1.api.radio-browser.info/json/stations/byname/{name}"


class ShazamIdentifier:
    """Identifies songs playing on radio streams using ShazamIO + ffmpeg."""

    def __init__(self, session, interval=30, capture_duration=10):
        """
        Initialize the identifier.

        Args:
            session: aiohttp.ClientSession to reuse for HTTP requests.
            interval: Minimum seconds between identification attempts.
            capture_duration: Seconds of audio to capture for each attempt.
        """
        self._session = session
        self._interval = interval
        self._capture_duration = capture_duration

        self._shazam = Shazam()
        self._cache = {}            # key: (station, uri) -> {"result": ..., "timestamp": float}
        self._pending_task = None   # currently running asyncio.Task, if any
        self._last_identify_time = 0.0
        self._last_failed = False
        self._previous_success = False
        self._should_revert = False

        # Check ffmpeg availability at startup — disable feature if missing
        if shutil.which("ffmpeg") is None:
            _LOGGER.warning(
                "ffmpeg not found on PATH — Shazam identification disabled. "
                "Install ffmpeg to enable this feature."
            )
            self.available = False
        else:
            self.available = True

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def identify_async(self, sonos_data):
        """
        Fire-and-forget: launch a background identification task if conditions allow.

        Does nothing if:
        - ffmpeg is unavailable
        - A task is already running
        - The throttle interval hasn't elapsed
        """
        if not self.available:
            return

        if self._pending_task is not None and not self._pending_task.done():
            # Already working on an identification — don't pile up tasks
            return

        now = time.monotonic()
        if now - self._last_identify_time < self._interval:
            return

        self._last_identify_time = now
        _LOGGER.debug("Starting Shazam identification...")
        self._pending_task = asyncio.create_task(self._do_identify(sonos_data))

    def get_result(self):
        """
        Return the completed identification result dict, or None.

        Result dict keys: "track", "artist", "album" (may be ""), "image_url" (may be "").
        Returns None if no completed task is available.
        """
        if self._pending_task is None:
            return None

        if not self._pending_task.done():
            return None

        task = self._pending_task
        self._pending_task = None

        try:
            result = task.result()
        except Exception as err:
            _LOGGER.warning("Shazam identification task raised an exception: %s", err)
            result = None

        if isinstance(result, dict):
            self._previous_success = True
        else:
            if self._previous_success:
                self._should_revert = True
            self._previous_success = False

        return result

    @property
    def should_revert(self):
        """True once after a failed identification follows a successful one (track ended)."""
        revert = self._should_revert
        self._should_revert = False
        return revert

    # ------------------------------------------------------------------
    # Internal: main identification flow
    # ------------------------------------------------------------------

    async def _do_identify(self, sonos_data):
        """
        Full identification pipeline:
        1. Check cache
        2. Resolve stream URL from sonos_data.uri
        3. Capture audio via ffmpeg
        4. Call ShazamIO
        5. Parse + cache result
        """
        station = getattr(sonos_data, "station", "") or ""
        uri = getattr(sonos_data, "uri", "") or ""

        # --- Resolve stream URL (cached to avoid repeated API lookups) ---
        cache_key = (station, uri)
        cached = self._cache.get(cache_key)
        if cached is not None:
            age = time.monotonic() - cached["timestamp"]
            ttl = _CACHE_TTL_HIT if cached["result"] else _CACHE_TTL_MISS
            if age < ttl:
                stream_url = cached["result"]
            else:
                del self._cache[cache_key]
                cached = None

        if cached is None:
            stream_url = await self._extract_stream_url(uri, station)
            self._store_cache(cache_key, stream_url)

        if not stream_url:
            _LOGGER.warning("Could not resolve stream URL for URI: %s", uri)
            return None

        # --- Capture audio (always re-capture since radio content changes) ---
        audio_bytes = await self._capture_from_stream(stream_url, self._capture_duration)
        if not audio_bytes:
            _LOGGER.warning("ffmpeg returned no audio from: %s", stream_url)
            self._cache.pop(cache_key, None)
            if station:
                self._mark_stream_url_failed(station, stream_url)
            return None

        # ffmpeg succeeded — persist the URL so future runs skip resolution
        if station:
            self._save_stream_url(station, stream_url)

        # --- Identify ---
        # ShazamIO's recognize() expects a file path, so write to a temp file
        # and clean up immediately after recognition.
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.write(fd, audio_bytes)
            os.close(fd)
            raw = await self._shazam.recognize(tmp_path)
        except Exception as err:
            _LOGGER.warning("ShazamIO recognition failed: %s", err)
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        result = self._parse_result(raw)

        if result:
            _LOGGER.info(
                "Shazam identified: %s — %s", result.get("artist"), result.get("track")
            )

        return result

    # ------------------------------------------------------------------
    # Internal: stream URL resolution
    # ------------------------------------------------------------------

    async def _extract_stream_url(self, uri, station_name):
        """
        Resolve a Sonos URI to a playable HTTP stream URL.

        Tries in order:
        1. User override file (stream_urls.json in repo root)
        2. x-rincon-mp3radio:// prefix — strip and prepend http://
        3. Already http(s):// — use directly
        4. x-sonosapi-radio/stream: — extract TuneIn station ID, query OPML API
        5. Radio-Browser.info lookup by station name
        """
        if not uri:
            return None

        # 1. User override file — checked first so the user can always force a URL
        if station_name:
            url = self._load_user_stream_url(station_name)
            if url:
                _LOGGER.debug("Using user-supplied stream URL for %r: %s", station_name, url)
                return url

        # 2. x-rincon-mp3radio://host/path -> http://host/path
        if uri.startswith("x-rincon-mp3radio://"):
            return "http://" + uri[len("x-rincon-mp3radio://"):]

        # 3. Direct HTTP(S) URL
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri

        # 4. x-sonosapi-radio: or x-sonosapi-stream: with TuneIn ID -> OPML lookup
        if uri.startswith(("x-sonosapi-radio:", "x-sonosapi-stream:")):
            match = re.search(r"tunein%3a(\d+)", uri) or re.search(r"(s\d+)", uri)
            if match:
                station_id = match.group(1)
                if not station_id.startswith("s"):
                    station_id = "s" + station_id
                url = await self._resolve_tunein(station_id)
                if url:
                    return url

        # 5. Radio-Browser.info lookup by station name
        if station_name:
            url = await self._resolve_radio_browser(station_name)
            if url:
                return url

        return None

    async def _resolve_tunein(self, station_id):
        """Query TuneIn's OPML API and return the first playable stream URL."""
        url = _TUNEIN_URL.format(station_id=station_id)
        try:
            async with self._session.get(url, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                # Response: {"head": {...}, "body": [{"url": "...", ...}, ...]}
                body = data.get("body", [])
                for item in body:
                    item_url = item.get("url", "")
                    if item_url.startswith("http") and "tunein.com/service/Audio" not in item_url:
                        return item_url
        except Exception as err:
            _LOGGER.warning("TuneIn OPML lookup failed for %s: %s", station_id, err)
        return None

    async def _resolve_radio_browser(self, station_name):
        """Look up a station's stream URL via the Radio-Browser.info API."""
        from urllib.parse import quote
        url = _RADIO_BROWSER_URL.format(name=quote(station_name))
        try:
            async with self._session.get(url, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                stations = await resp.json()
                for station in stations:
                    stream_url = station.get("url_resolved") or station.get("url")
                    if stream_url and stream_url.startswith("http"):
                        return stream_url
        except Exception as err:
            _LOGGER.warning("Radio-Browser.info lookup failed for %s: %s", station_name, err)
        return None

    def _load_user_stream_url(self, station_name):
        """
        Look up a station name in the user's stream_urls.json override file.

        The file is loaded fresh each call (it's small and the user may edit
        it while the app is running). Lookup is case-insensitive.

        Supports two value formats:
        - Plain string: user-supplied URL, always returned as-is.
        - Dict with "url" key: auto-discovered entry. Skipped if "status" == "failed".

        Returns the URL string if found and usable, None otherwise.
        """
        if not os.path.exists(_STREAM_URLS_FILE):
            return None
        try:
            with open(_STREAM_URLS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            station_lower = station_name.lower()
            for key, value in data.items():
                if key.lower() == station_lower:
                    if isinstance(value, str):
                        return value
                    if isinstance(value, dict):
                        if value.get("status") == "failed":
                            return None
                        return value.get("url")
        except Exception as err:
            _LOGGER.warning("Failed to read %s: %s", _STREAM_URLS_FILE, err)
        return None

    def _save_stream_url(self, station_name, url):
        """
        Persist an auto-discovered stream URL to stream_urls.json.

        Does not overwrite existing user-supplied entries (plain strings).
        Writes/updates the entry as {"url": url, "auto": true}.
        """
        try:
            if os.path.exists(_STREAM_URLS_FILE):
                with open(_STREAM_URLS_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            else:
                data = {
                    "_comment": "Station name to stream URL mappings for Shazam audio capture. "
                               "To add a station, just add a name and URL. Entries with 'auto' "
                               "were discovered automatically. Entries marked 'failed' could not "
                               "be reached and should be corrected or deleted."
                }

            station_lower = station_name.lower()
            for key, value in data.items():
                if key.lower() == station_lower:
                    # Never overwrite a user-supplied plain string entry
                    if isinstance(value, str):
                        return
                    # Update the existing dict entry
                    data[key] = {"url": url, "auto": True}
                    break
            else:
                # No existing entry — create one using the original station name
                data[station_name] = {"url": url, "auto": True}

            with open(_STREAM_URLS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)

            _LOGGER.info("Saved stream URL for %r to stream_urls.json", station_name)
        except Exception as err:
            _LOGGER.warning("Failed to save stream URL for %r: %s", station_name, err)

    def _mark_stream_url_failed(self, station_name, url):
        """
        Mark an auto-discovered stream URL as failed in stream_urls.json.

        Only modifies dict entries with "auto": true. User-supplied plain string
        entries are left untouched.
        """
        if not os.path.exists(_STREAM_URLS_FILE):
            return
        try:
            with open(_STREAM_URLS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            station_lower = station_name.lower()
            for key, value in data.items():
                if key.lower() == station_lower:
                    # User-supplied entries are sacred — never modify them
                    if isinstance(value, str):
                        return
                    if isinstance(value, dict) and value.get("auto"):
                        data[key] = {**value, "status": "failed"}
                    break
            else:
                # No matching entry — nothing to mark
                return

            with open(_STREAM_URLS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=4)

            _LOGGER.info("Marked stream URL as failed for %r in stream_urls.json", station_name)
        except Exception as err:
            _LOGGER.warning("Failed to mark stream URL as failed for %r: %s", station_name, err)

    # ------------------------------------------------------------------
    # Internal: audio capture
    # ------------------------------------------------------------------

    async def _capture_from_stream(self, url, duration):
        """
        Capture `duration` seconds of audio from `url` using ffmpeg.
        Returns raw WAV bytes suitable for ShazamIO, or None on failure.
        No temp files — data flows via stdout pipe.
        """
        cmd = [
            "ffmpeg",
            "-i", url,
            "-t", str(duration),
            "-f", "wav",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", "16000",
            "-loglevel", "error",
            "pipe:1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Add a generous timeout: capture duration + 30s for connection/buffering
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=duration + 30
            )
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg timed out capturing from: %s", url)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return None
        except Exception as err:
            _LOGGER.warning("ffmpeg subprocess error for %s: %s", url, err)
            return None

        if proc.returncode != 0:
            err_text = stderr.decode(errors="replace").strip() if stderr else ""
            _LOGGER.warning(
                "ffmpeg exited with code %d for %s: %s",
                proc.returncode, url, err_text
            )
            return None

        return stdout if stdout else None

    # ------------------------------------------------------------------
    # Internal: result parsing
    # ------------------------------------------------------------------

    def _parse_result(self, raw):
        """
        Extract structured metadata from a ShazamIO response dict.

        ShazamIO response structure:
          raw["track"]["title"]           — track name
          raw["track"]["subtitle"]        — artist name
          raw["track"]["images"]          — dict with "coverart", "coverarthq" keys
          raw["track"]["share"]["image"]  — fallback image URL
          raw["track"]["sections"]        — list of metadata sections

        Returns {"track": str, "artist": str, "album": str, "image_url": str}
        or None if no track was identified.
        """
        if not raw or "track" not in raw:
            return None

        track_data = raw["track"]
        if not track_data:
            return None

        track_name = track_data.get("title", "")
        artist_name = track_data.get("subtitle", "")

        if not track_name and not artist_name:
            return None

        # Extract album from sections (type "SONG", metadata key "Album")
        album_name = ""
        for section in track_data.get("sections", []):
            if section.get("type") == "SONG":
                for meta in section.get("metadata", []):
                    if meta.get("title", "").lower() == "album":
                        album_name = meta.get("text", "")
                        break
            if album_name:
                break

        # Extract image: prefer high-quality cover art, fall back to share image
        images = track_data.get("images", {})
        image_url = (
            images.get("coverarthq")
            or images.get("coverart")
            or track_data.get("share", {}).get("image", "")
        )

        return {
            "track": track_name,
            "artist": artist_name,
            "album": album_name,
            "image_url": image_url,
        }

    # ------------------------------------------------------------------
    # Internal: cache management
    # ------------------------------------------------------------------

    def _store_cache(self, key, result):
        """Store a result in the cache, evicting the oldest entry if at capacity."""
        if len(self._cache) >= _CACHE_MAX_ENTRIES and key not in self._cache:
            # Evict the oldest entry by insertion timestamp
            oldest_key = min(self._cache, key=lambda k: self._cache[k]["timestamp"])
            del self._cache[oldest_key]

        self._cache[key] = {
            "result": result,
            "timestamp": time.monotonic(),
        }
