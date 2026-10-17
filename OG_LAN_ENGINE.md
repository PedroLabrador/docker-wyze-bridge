# LAN engine for Wyze Cam OG (`OG_LAN`)

This branch adds an alternative camera source: a pure-Python LAN client for
Wyze Cam OG instead of the bundled cloud/SDK session path.  It is selected with
`OG_LAN=1` and leaves the rest of the bridge (MediaMTX, RTSP/HLS/WebRTC, MQTT,
Home Assistant discovery, the web UI, snapshots, recording, auth) untouched.

## Why the integration is small

Whatever a camera source is, the bridge only asks it for two byte streams:

```python
# app/wyzebridge/wyze_stream.py
with Popen(ffmpeg_cmd, stdin=PIPE) as ffmpeg:
    for frame in sess.recv_bridge_data():   # raw H.264, no container
        ffmpeg.stdin.write(frame)
# and, on a second thread, raw G.711 mu-law into /tmp/<uri>_audio.pipe
```

Everything downstream of ffmpeg is transport-agnostic.  So the LAN engine is
adapted to that one interface rather than wired into `StreamManager`.

| Session member | Uses | Supplied by |
| --- | --- | --- |
| `camera` | 16 | the bridge's own `WyzeCamera` (cloud metadata) |
| `recv_bridge_data()` | 1 | `ogengine` H.264 |
| `recv_audio_pipe()` | 1 | `ogengine` mu-law |
| `enable_audio`, `identify_audio_codec()` | 3 | fixed: `mulaw` @ 16000 |
| `session_check()` | 2 | fixed: LAN |
| `should_stream()` | 5 | session liveness |
| `resolution`, `preferred_bitrate`, `preferred_frame_rate`, `update_frame_size_rate()` | 8 | informational; quality is chosen by the AvStreamCtl |
| `iotctrl_mux()` | 3 | not implemented (control channel) |

## Files

| Path | What it is |
| --- | --- |
| `app/ogengine/` | the LAN client: discovery, CALLING, KCP, RC5, H.264/mu-law |
| `app/wyzebridge/og_session.py` | the adapter (session surface above) |
| `app/wyzebridge/wyze_stream.py` | one target swap plus `start_og_stream()` |

`ogengine` depends on the standard library only (plus ffmpeg, already in the
image).  No cloud session, no bundled SDK, no third-party Python package.

## Configuration

| Variable | Meaning |
| --- | --- |
| `OG_LAN=1` | use this engine |
| `OG_CALLING_FILE` | JSON holding the CALLING frame, default `/config/og_calling.json` |
| `OG_BROADCAST` | optional discovery broadcast address, default `255.255.255.255` |
| `OG_CAMERAS` | optional `mac-or-nickname=address` list, for networks that drop broadcasts |

`OG_CALLING_FILE` accepts either shape:

```json
{"calling": "<128 bytes as hex>"}
{"devices": {"429728658691722": {"frames": {"calling": "<hex>"}}}}
```

The CALLING frame is session key material: it belongs in the config volume,
never in the repository.

`OG_CAMERAS` is only needed where the bridge cannot see the LAN's broadcasts --
a container behind Docker Desktop's userland NAT, for example.  There the reply
to a unicast probe still arrives, but with its source address rewritten, so the
camera's address has to be stated instead:

```bash
OG_CAMERAS="GW_GC1_000000000001=192.168.1.50,GW_GC1_000000000002=192.168.1.51"
```

Discovery then probes those addresses directly and attributes each reply to the
address it was sent to.  On a Linux host with real host networking the
broadcast probe works and this is unnecessary.

## Behaviour on this path

* **Discovery** is LAN only: the camera is found by broadcast probe each time
  the stream starts, because its P2P port is not stable.  The camera is matched
  by MAC (falling back to its cloud IP), so the cloud device list still decides
  the name, nickname and paths.
* **Video** is native H.264.  `definition=2` asks for 1920x1080; `SUBSTREAM` or
  `options.substream` asks for the 640x360 sub-stream.  Scaling stays with
  ffmpeg/MediaMTX.
* **Timing**: a raw pipe carries no timestamps, and the small probe budget the
  bridge uses (`-probesize 32 -analyzeduration 32`) also stops ffmpeg from
  inferring a frame rate.  A stream copy would therefore reach the muxer with no
  timeline at all, and MediaMTX has nothing to cut HLS segments or snapshots
  from.  The ffmpeg command for this source is built with `live=True`, which
  re-encodes (the decoder reads the frame rate from the stream itself) and
  forces an IDR every 2 s.  The cloud/TUTK path is unchanged and still copies.
* **Audio** is 16 kHz mu-law, and the bridge's own audio thread writes it to the
  FIFO.  The session pumps the camera's single socket once and fans the two
  streams out over queues, because the engine's video and audio readers must not
  run against the same socket concurrently.  ffmpeg re-encodes it to 8 kHz
  PCMU, which RTSP clients and MediaMTX's WebRTC player accept.  MediaMTX
  cannot put G.711 into HLS and skips that track, so HLS serves video only;
  `AUDIO_CODEC=aac` trades browser-WebRTC audio for audio in HLS.
* **Recovery**: 12 s without media triggers a fresh handshake inside the session
  (same process), so a camera that was busy or a Wi-Fi blip does not need a
  process restart.

## What is not available on this path

* The control channel: camera commands (power, notifications, timezone,
  motion-cmd) need `iotctrl_mux()`, which this engine does not implement.
* The cloud/SDK session itself: `wyzecam`'s TUTK client stays in the tree and is
  used by every camera that does not run with `OG_LAN=1`.
* The microphone/talk feature is **not** ported yet.  Talk is AMR-NB over the
  same P2P session (`p2p.CameraStream.open_talk` / `send_talk`); it needs a
  route in `frontend.py` plus a UI control, since the bridge's existing audio
  back-channel is the WebRTC path and is unrelated.

## Checking it

```bash
python3 -m py_compile app/ogengine/*.py app/wyzebridge/og_session.py
OG_LAN=1 python3 app/ogengine/discover.py     # must list each camera
```

With `OG_LAN=1` the log shows `LAN session to <name> at <ip>:<port> (1080p)`
before ffmpeg starts.  A viewer should then get:

* `rtsp://<host>:8554/<name>` — H.264 video plus PCMU audio;
* `http://<host>:8888/<name>/index.m3u8` — HLS that actually plays;
* `/snapshot/<name>.jpg` on the web UI port — a JPEG of the last keyframe.
