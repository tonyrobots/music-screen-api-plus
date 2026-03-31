# Music Screen API Plus

> Fork of [hankhank10/music-screen-api](https://github.com/hankhank10/music-screen-api), adding [Shazam-based audio identification](#automatic-song-identification-shazam) for radio streams that don't carry their own metadata. Active development — more features planned.

A Raspberry Pi application that displays currently-playing music information from your local Sonos system. Supports the Pimoroni HyperPixel 4.0 Square (high-res color album art) and the Pimoroni inky wHAT (e-ink display).

![sonos-music-api Examples of Display Modes](https://user-images.githubusercontent.com/42817877/153710473-2fbe9534-b7d6-423e-8fd3-20193611c99e.png)

![sonos-music-api Examples of Display Modes Show Play Settings](https://user-images.githubusercontent.com/42817877/209093576-1bf5e0c0-ef5d-473f-8d7a-9d06ddb2f1e0.png)

Works in real time with your local Sonos system. Optional integrations with Spotify (album art, Spotify Codes) and last.fm (play history). No authentication required for basic functionality.

# Required hardware

Raspberry Pi 3 or 4 recommended. The Pi 3 A+ fits behind the HyperPixel Square display nicely. Pi Zero W can be used with [some stipulations](#important-notice-on-pi-zero).

- [Pimoroni inky wHAT](https://shop.pimoroni.com/products/inky-what?variant=21214020436051)
- [Pimoroni HyperPixel 4.0 Square Non Touch](https://shop.pimoroni.com/products/hyperpixel-4-square?variant=30138251477075)

# Installation

Step-by-step guides for beginners:
- [e-INK version](https://www.hackster.io/mark-hank/currently-playing-music-on-e-ink-display-310645)
- [High-res version](https://www.hackster.io/mark-hank/sonos-album-art-on-raspberry-pi-screen-5b0012)

Before running, copy the example settings file and edit it:
```
cp sonos_settings.py.example sonos_settings.py
nano sonos_settings.py
```

## Quick install (dependencies only)

```
sudo apt install python3-tk python3-pil python3-pil.imagetk
pip3 install -r requirements.txt
```

# Webhook updates

Enabling webhook support in the `node-sonos-http-api` configuration is **strongly** recommended. Without this enabled, the script must repeatedly poll to check for updates.

Webhook support for `node-sonos-http-api` can be enabled by updating/creating the `settings.json` configuration file located in the base of the `node-sonos-http-api/` directory:
```
{
  "webhook": "http://localhost:8080/"
}
```
_Note_: This file does not exist by default and you may need to create it. Also note that the `settings.js` file is part of the `node-sonos-http-api` code and should **not** be modified.

The above configuration assumes that `node-sonos-http-api` is running on the same machine. If running on a different machine, replace `localhost` with the IP of the host running this script.

# Backlight control

Thanks to a pull request from [jjlawren](https://github.com/jjlawren) the backlight of the Hyperpixel will turn off when music is not playing to save power & the environment.

If running Raspbian / Raspberry Pi OS, this should work out of the box. If running a different distribution, you'll need to run the following commands:
```
sudo pip3 install RPi.GPIO
sudo gpasswd -a pi gpio

```

# Spotify Codes and Album Art

To display Spotify Codes or use Spotify album art, you need a [Spotify Developer account](https://developer.spotify.com/) and the [spotipy](https://pypi.org/project/spotipy/) package. Add your credentials to `sonos_settings.py`:

```
spotify_client_id = ""
spotify_client_secret = ""
spotify_market = None       # Optional: set to a country code (e.g. "US") to localize results

show_spotify_code = True       # Show Spotify Code graphic for the current track
show_spotify_albumart = True   # Use Spotify album art instead of Sonos-provided art
```

If the script fails after adding Spotify credentials, test them manually:
```
python3 spotipy_auth_search_test.py
```

# Automatic Song Identification (Shazam)

Radio stations often don't provide track metadata. When this happens, the script can capture a short audio clip from the stream and use Shazam to identify what's playing, displaying the track name, artist, album, and album art.

## Requirements

Install system dependencies:
```
sudo apt install ffmpeg libopenblas-dev
```

The `shazamio` Python package is included in `requirements.txt`. If you haven't already run `pip3 install -r requirements.txt`, install it with:
```
pip3 install shazamio
```

## Configuration

Add the following to `sonos_settings.py`:
```
shazam_enabled = True         # Enable Shazam identification (default: False)
shazam_interval = 30          # Minimum seconds between identification attempts
shazam_capture_duration = 10  # Seconds of audio to capture per attempt
```

Optional: set a longer detail display timeout for Shazam-identified tracks (useful since identification refreshes periodically):
```
shazam_show_details_timeout = 30  # Seconds to show track info (overrides show_details_timeout)
```

## How it works

When playing a radio station with missing metadata, the script:
1. Resolves the station's stream URL (tries TuneIn OPML API first, then [Radio-Browser.info](https://www.radio-browser.info/) lookup by station name)
2. Captures audio from the stream via `ffmpeg` in the background
3. Sends the audio to Shazam for identification
4. Updates the display with the identified track, artist, album, and album art

Identification runs every `shazam_interval` seconds to catch song changes. Expect results to lag ~15 seconds behind a new track starting. Songs not in Shazam's database (obscure tracks, talk segments, ads) will silently fail and retry on the next interval.

## Testing

A standalone test script is included:
```
python3 test_shazam.py [stream_url]
```
This exercises the full pipeline (ffmpeg capture, Shazam recognition) independently of Sonos.

# Autostart configuration

To start the script automatically on boot, add it to the LXDE autostart file:

```
sudo nano /etc/xdg/lxsession/LXDE-pi/autostart
```

Add the following line at the end:
```
@sh ~/music-screen-api/music-screen-api-startup.sh
```

The included `music-screen-api-startup.sh` script handles `cd`-ing into the project directory before launching, which is important for features like Spotify (spotipy's `.cache` file) to work correctly. Its contents:
```
cd ~/music-screen-api
python3 go_sonos_highres.py
```

**Note:** The script should run as your normal user, not with `sudo`. Running with `sudo` causes display access issues and requires system-wide pip installs. If you have an older autostart entry using `@sudo /usr/bin/python3 ...`, replace it with the startup script line above.

## Spotify autostart troubleshooting

If the script fails on startup after configuring Spotify, verify your API credentials work by running manually:
```
python3 spotipy_auth_search_test.py
```

# REST API

The script exposes some REST API endpoints to allow remote control and integration options.

| Method | Endpoint       | Payload | Notes |
| :----: | :------------: | ------- | ----- |
| `GET`  | `/state`       | None    | Provides current playing state in JSON format. |
| `POST` | `/set-room`    | `room`: name of room (`str`) | Change actively monitored speaker/room. |
| `POST` | `/show-detail` | `detail`: 0/1, true/false (`bool`, required)<br/><br/>`timeout`: seconds (`int`, optional)| Show/hide the detail view. Use `timeout` to revert to the full album view after a delay. Has no effect if paused/stopped. |

Examples:
```
curl http://<IP_OF_HOST>:8080/status
 -> {"room": "Bedroom", "status": "PLAYING", "trackname": "Living For The City", "artist": "Stevie Wonder", "album": "Innervisions", "duration": 442, "webhook_active": true}

curl --data "room=Kitchen" http://<IP_OF_HOST>:8080/set-room
 -> OK
 
curl --data "detail=true" --data "timeout=5" http://<IP_OF_HOST>:8080/show-detail
 -> OK
```

# Important notice on Pi Zero

### HyperPixel version

A Pi Zero W can run both `node-sonos-http-api` and the full color album art version as long as [webhooks](#webhook-updates) have been properly enabled. It updates slightly slower (1-2 seconds) than a Pi 3/4.

### E-ink version

_Note: The e-ink version has not been updated to use [webhooks](#webhook-updates) which the HyperPixel version uses and requires the performance tweaks below._

The e-ink script can be got running with a Pi Zero, however you will want to note two things:

1. Save yourself a headache and ensure you're getting a Pi Zero WH (ie wireless and with headers pre-soldered)

2. It runs pretty poorly on a Pi Zero due to the processing requirements. Actually this script runs fine, but it can struggle to do this and the http-sonos-api consistently. If you are set on running on a Pi Zero then either have the sonos-api running on a different local machine (and redirect to that IP address in sonos_settings.py) or set the Pi_Zero flag in sonos_settings.py to True (this slows down the frequency of requests)

(Thanks to reddit user u/Burulambie for helping me troubleshoot this)

# Demaster (track name cleanup)

The script uses [demaster](https://github.com/hankhank10/demaster) to strip noise from track names ("Remastered 2011", "Live at...", etc.) for cleaner display. Enabled by default.

- **Disable**: set `demaster = False` in `sonos_settings.py`
- **Privacy**: by default, track names are sent to a remote API for more accurate cleanup. No personal information is included. To use offline-only mode, set `demaster_query_cloud = False` in `sonos_settings.py` — this uses a local regex which is less thorough but avoids any network requests.
