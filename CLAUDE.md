# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raspberry Pi application that displays currently-playing Sonos music info on either a Pimoroni inky wHAT e-ink display or a HyperPixel 4.0 Square high-res display. Pulls data from a local `node-sonos-http-api` instance, with optional Spotify and Last.fm integration.

## Running

```bash
# High-res HyperPixel display (primary/active version)
python3 go_sonos_highres.py

# E-ink display (legacy, no webhook support)
python3 go_sonos.py

# Last.fm display
python3 go_last.py
```

Requires `sonos_settings.py` (copy from `sonos_settings.py.example`). Also requires a running `node-sonos-http-api` instance (default: localhost:5005).

## Install

```bash
sudo apt install python3-tk python3-pil python3-pil.imagetk
pip3 install -r requirements.txt
```

## Architecture

The high-res path (`go_sonos_highres.py`) is the main, actively-maintained version. It runs an async event loop with:

- **`SonosData`** (`sonos_user_data.py`) - Fetches/stores speaker state from `node-sonos-http-api`. Handles both polling (1s interval) and webhook-driven updates (60s fallback poll). Parses radio stream metadata with artist/track splitting logic for `artist_and_album_newlook` mode.
- **`SonosWebhook`** (`webhook_handler.py`) - aiohttp web server on port 8080. Receives POST webhooks from `node-sonos-http-api` and exposes REST endpoints: `GET /status`, `POST /set-room`, `POST /show-detail`.
- **`DisplayController`** (`display_controller.py`) - Tkinter fullscreen GUI (720x720). Manages three stacked frames: album art, detail view (track/artist/album text + play state), and curtain (black screen for idle). Controls HyperPixel backlight via GPIO.
- **`async_demaster`** (`async_demaster.py`) - Strips "Remastered", "Live at", etc. from track names. Online API at `demaster.hankapi.com` with offline regex fallback.
- **`Backlight`** (`hyperpixel_backlight.py`) - GPIO pin 19 control for HyperPixel backlight. Gracefully degrades if RPi.GPIO unavailable.
- **`ShazamIdentifier`** (`async_shazam.py`) - Identifies songs on radio streams when metadata is missing. Resolves Sonos URIs to stream URLs (TuneIn OPML → Radio-Browser.info fallback), captures audio via ffmpeg subprocess, identifies via shazamio. Runs as a background `asyncio.Task` with 30s throttle. Conditionally loaded when `shazam_enabled = True`.

The e-ink path (`go_sonos.py`) is legacy: synchronous polling loop using `sonos_user_data_legacy.py`, `demaster.py` (sync version), and `ink_printer.py`.

## Key Design Patterns

- Settings are read from `sonos_settings.py` via `getattr()` with defaults, so new settings don't break existing installs
- Spotify integration (spotipy) is conditionally imported only when `show_spotify_code` or `show_spotify_albumart` is enabled
- Shazam integration is conditionally imported only when `shazam_enabled` is True
- Display dynamically adjusts thumbnail size and font size based on track/artist/album text length
- Webhook mode is auto-detected: switches from polling to webhook on first received webhook, falls back after 130s timeout

## Shazam Integration Notes

- Stream URL resolution: Sonos radio URIs (`x-sonosapi-stream:`, `x-sonosapi-radio:`) are opaque. TuneIn OPML API often returns placeholder "nostream" URLs. Radio-Browser.info (free API, lookup by station name) is the reliable fallback.
- ShazamIO's `recognize()` requires a file path, not bytes — uses a temp .wav file with immediate cleanup in a `finally` block.
- shazamio's internal audio decoder emits noisy warnings ("skipping junk", "invalid mpeg audio header") through the root logger. Suppressed via `_ShazamNoiseFilter` applied to all handlers after `setup_logging()`.
- Race condition: Sonos sometimes sends an empty-metadata update followed by a full-metadata update ~2s later. Shazam results are only applied if `needs_shazam` is still True (metadata still missing) when the result arrives.
- `needs_shazam` property on `SonosData`: True when `type == "radio"` and trackname or artist is empty and status is PLAYING.
- `shazam_resolved` flag on `SonosData`: gates the Spotify lookup to avoid redundant API calls when Shazam already provided album art. Reset on each `refresh()`.
- Cache stores resolved stream URLs only (not Shazam results), since radio content changes while the URI stays the same.
- Requires `ffmpeg`, `libopenblas-dev` (apt), and `shazamio` (pip) on the Pi.
