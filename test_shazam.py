"""
Standalone test script for the Shazam audio identification pipeline.

Usage: python3 test_shazam.py [stream_url]

Tests the ShazamIdentifier pipeline independently of Sonos by exercising
each step directly: ffmpeg check, audio capture, ShazamIO recognition,
and URI resolution logic.
"""

import asyncio
import shutil
import sys

import aiohttp

from async_shazam import ShazamIdentifier

DEFAULT_STREAM_URL = "http://stream.live.vc.bbcmedia.co.uk/bbc_radio_two"

# Sample Sonos-style URIs to exercise _extract_stream_url
SAMPLE_URIS = [
    "x-rincon-mp3radio://stream.live.vc.bbcmedia.co.uk/bbc_radio_two",
    "x-sonosapi-radio:s24940?sid=254",
    "http://some-direct-stream.example.com/stream",
]


async def main():
    stream_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STREAM_URL

    print("=" * 60)
    print("Shazam pipeline test")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Step 1: ffmpeg availability
    # ------------------------------------------------------------------
    print("\n[1] Checking ffmpeg availability...")
    if shutil.which("ffmpeg") is not None:
        print("    OK — ffmpeg found at:", shutil.which("ffmpeg"))
    else:
        print("    FAIL — ffmpeg not found on PATH. Install ffmpeg to proceed.")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        identifier = ShazamIdentifier(session, interval=0, capture_duration=10)

        print(f"    ShazamIdentifier.available = {identifier.available}")

        # ------------------------------------------------------------------
        # Step 2: URI extraction logic
        # ------------------------------------------------------------------
        print("\n[2] Testing stream URL extraction from Sonos-style URIs...")
        for uri in SAMPLE_URIS:
            resolved = await identifier._extract_stream_url(uri, speaker_ip=None)
            status = "OK" if resolved else "UNRESOLVED"
            print(f"    [{status}]  {uri}")
            print(f"           -> {resolved}")

        # ------------------------------------------------------------------
        # Step 3: Audio capture from the target stream
        # ------------------------------------------------------------------
        print(f"\n[3] Capturing 10s of audio from: {stream_url}")
        print("    (this will take ~10 seconds...)")
        audio_bytes = await identifier._capture_from_stream(stream_url, duration=10)

        if audio_bytes:
            print(f"    OK — captured {len(audio_bytes):,} bytes of audio")
        else:
            print("    FAIL — ffmpeg returned no audio. Check the stream URL and network.")
            sys.exit(1)

        # ------------------------------------------------------------------
        # Step 4: ShazamIO recognition
        # ------------------------------------------------------------------
        print("\n[4] Running ShazamIO recognition on captured audio...")
        import os
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        try:
            os.write(fd, audio_bytes)
            os.close(fd)
            print(f"    Temp file: {tmp_path} ({os.path.getsize(tmp_path):,} bytes)")
            raw = await identifier._shazam.recognize(tmp_path)
        except Exception as err:
            print(f"    FAIL — ShazamIO raised: {err}")
            sys.exit(1)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # ------------------------------------------------------------------
        # Step 5: Parse and display result
        # ------------------------------------------------------------------
        print("\n[5] Parsing result...")
        result = identifier._parse_result(raw)

        if result:
            print("    Match found!")
            print(f"    Track:     {result['track']}")
            print(f"    Artist:    {result['artist']}")
            print(f"    Album:     {result['album'] or '(not available)'}")
            print(f"    Image URL: {result['image_url'] or '(not available)'}")
        else:
            print("    No match found (Shazam could not identify the track).")
            print("    Raw response keys:", list(raw.keys()) if raw else "empty")

    print("\n" + "=" * 60)
    print("Test complete.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
