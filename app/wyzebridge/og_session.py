"""LAN P2P camera source for Wyze Cam OG (GW_GC1).

The bridge only ever asks a session object for two byte streams:

    recv_bridge_data()   raw H.264 frames, written straight to ffmpeg's stdin
    recv_audio_pipe()    raw mu-law audio, written to /tmp/<uri>_audio.pipe

This module supplies both from the pure-Python engine in ``ogengine``
(LAN discovery -> CALLING -> KCP -> RC5), so StreamManager, MediaMTX, MQTT and
the web UI are untouched.  It replaces the cloud/SDK session path for these
cameras; ``start_og_stream()`` in wyze_stream.py does the wiring.

Scope: LAN only, one session per camera, H.264 video and 16 kHz G.711 mu-law
audio.  The device control channel is not implemented, so camera commands
(power, notifications, timezone, ...) are unavailable for these cameras.

Why a queue fan-out
-------------------
WyzeStream drives video on the main thread and audio on a second one, but the
engine's raw_h264()/raw_audio() generators both read the *same* UDP socket and
must not run at once.  So this session pumps the socket once, in a single
reader thread, and hands the two streams to their consumers through separate
queues.  Do not call the engine's generators directly from here.

Configuration
-------------
OG_CALLING_FILE
    JSON holding the CALLING frame (session key material), in either shape::

        {"devices": {"<id>": {"frames": {"calling": "<hex>"}}}}
        {"calling": "<hex>"}

    Never commit this file; it is per-install session material.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import queue
import threading
import time
from contextlib import contextmanager

from wyzebridge.logging import logger

from ogengine import kcpav, p2p
from ogengine.discover import BROADCAST, subnet_broadcast

# The camera's audio is G.711 mu-law, 16 kHz, one byte per sample with no
# container of its own: ffmpeg reads it as `-f mulaw -ar 16000 -ac 1`.
AUDIO_CODEC = "mulaw"
AUDIO_RATE = 16000
VIDEO_CODEC = "h264"
NET_MODE_LAN = 2  # check_net_mode()'s "LAN" branch
CALLING_BYTES = 128
DISCOVERY_SECONDS = 3.0
DISCOVERY_ATTEMPTS = 3
CALLING_FILE = os.environ.get("OG_CALLING_FILE", "/config/og_calling.json")
QUEUE_DEPTH = 64
MEDIA_TIMEOUT = 12.0  # no media for this long -> re-handshake
RECONNECT_DELAY = 2.0


class _SessionCheck:
    """The only field the bridge reads from a session check: the net mode."""

    mode = NET_MODE_LAN


def _nonblocking(handle) -> None:
    flags = fcntl.fcntl(handle, fcntl.F_GETFL)
    fcntl.fcntl(handle, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _calling_strings(doc) -> list[str]:
    """Every CALLING-shaped hex string in a config file, in no order."""
    if isinstance(doc, str):
        return [doc]
    if not isinstance(doc, dict):
        return []
    out = [v for k, v in doc.items() if k == "calling" and isinstance(v, str)]
    for rec in doc.get("devices", {}).values():
        if isinstance(rec, dict):
            out += _calling_strings(rec.get("frames", {}))
    return out


class OGSession:
    """A camera session speaking the camera's own LAN protocol."""

    def __init__(self, stream, state):
        self.camera = stream.camera
        self.options = stream.options
        self.state = state
        self.pipe_name = self.camera.name_uri
        self.enable_audio = bool(self.options.audio)
        # Reported to ffmpeg before the first frame arrives; the engine asks
        # the camera for its 1080p profile in the AvStreamCtl.
        self.resolution = "1920x1080"
        self.preferred_bitrate = 0
        self.preferred_frame_rate = 0
        self._client = None
        self._stop = threading.Event()
        self._video: queue.Queue = queue.Queue(QUEUE_DEPTH)
        self._audio: queue.Queue = queue.Queue(QUEUE_DEPTH)
        self._reader: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------
    def __enter__(self):
        self._connect()
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def close(self) -> None:
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=2)
        if self._client:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _connect(self) -> None:
        self._client = self._open()
        self._reader = threading.Thread(target=self._pump, name=self.pipe_name)
        self._reader.start()

    def _open(self):
        """Find the camera on the LAN and run one fresh handshake."""
        dev = self._find_camera()
        definition = kcpav.DEF_SD if self.options.substream else kcpav.DEF_HD
        client = p2p.CameraStream(
            dev["ip"],
            dev["p2p_port"],
            dev["tid"],
            self._calling(),
            # A socket per camera: the advertised 48171 would collide between
            # cameras, and the camera replies to the UDP source port anyway.
            local_port=0,
            timeout=MEDIA_TIMEOUT,
            on_log=lambda line: logger.debug("[%s] %s", self.pipe_name, line),
            definition=definition,
        )
        client.start()
        logger.info(
            "LAN session to %s at %s:%s (%s)",
            self.camera.name_uri, dev["ip"], dev["p2p_port"],
            "sub-stream" if self.options.substream else "1080p",
        )
        return client

    def _configured_ip(self) -> str:
        """This camera's LAN address, from OG_CAMERAS when set.

        A container whose network drops broadcasts cannot learn the address
        from a reply, so it can be supplied instead:

            OG_CAMERAS="GW_GC1_000000000001=192.168.1.50,other=192.168.1.51"

        The left side matches the camera's MAC or its nickname; the right side
        is its address on the LAN.
        """
        keys = {(self.camera.mac or "").upper(), (self.camera.nickname or "").upper()}
        for item in os.environ.get("OG_CAMERAS", "").split(","):
            name, _, ip = item.partition("=")
            if name.strip().upper() in keys and ip.strip().count(".") == 3:
                return ip.strip()
        return ""

    def _targets(self) -> list:
        """Probe targets for this camera: its own address first, then its /24.

        A unicast probe is answered just like a broadcast one, and it is the
        only form that survives NAT -- relevant when the bridge runs in a
        container whose network drops broadcasts.
        """
        ip = self.camera.ip or self._configured_ip()
        targets = [ip] if ip.count(".") == 3 else []
        subnet = subnet_broadcast(ip) if targets else BROADCAST
        return targets + [subnet] if subnet not in targets else targets

    def _find_camera(self) -> dict:
        """Locate the camera on the LAN.  Its P2P port is not stable."""
        want_mac = (self.camera.mac or "").upper()
        want_ip = self.camera.ip or self._configured_ip()
        found: dict = {}
        for attempt in range(1, DISCOVERY_ATTEMPTS + 1):
            found = p2p.discover(DISCOVERY_SECONDS, broadcast=self._targets())
            if found:
                break
            logger.info("LAN discovery attempt %d found nothing", attempt)
        for ip, dev in found.items():
            got = (dev.get("wyze_id") or dev.get("mac") or "").upper()
            if (want_mac and got == want_mac) or (want_ip and ip == want_ip):
                return dev | {"ip": ip}
        logger.info(
            "%s: %d device(s) answered discovery, none matched %s",
            self.camera.name_uri, len(found), want_mac or want_ip or "?",
        )
        raise RuntimeError(
            "camera %s not on the LAN (%d device(s) answered)"
            % (self.camera.name_uri, len(found))
        )

    @staticmethod
    def _calling() -> bytes:
        try:
            with open(CALLING_FILE) as handle:
                doc = json.load(handle)
        except OSError as ex:
            raise RuntimeError(f"cannot read OG_CALLING_FILE: {ex}") from ex
        for hexs in _calling_strings(doc):
            with contextlib.suppress(ValueError):
                raw = bytes.fromhex(hexs.strip())
                if len(raw) == CALLING_BYTES:
                    return raw
        raise RuntimeError(
            f"{CALLING_FILE} holds no {CALLING_BYTES}-byte CALLING frame"
        )

    def _pump(self) -> None:
        """Single owner of the socket; splits the two streams into queues."""
        last_media = time.time()
        while not self._stop.is_set():
            try:
                self._client._pump(0.5, self._convs())
                if self._drain():
                    last_media = time.time()
                elif time.time() - last_media > MEDIA_TIMEOUT:
                    raise TimeoutError(f"no media for {MEDIA_TIMEOUT:.0f}s")
            except Exception as ex:
                logger.warning("[%s] session dropped: %s", self.pipe_name, ex)
                if not self._reconnect():
                    return
                last_media = time.time()

    def _convs(self) -> list:
        return [
            (self._client.kcp_lo.conv, self._client.kcp_lo),
            (self._client.kcp_hi.conv, self._client.kcp_hi),
        ]

    def _drain(self) -> bool:
        """Move whatever the engine decoded into the consumer queues."""
        got = False
        for src, dst in ((self._client._buf, self._video),
                         (self._client._abuf, self._audio)):
            if src:
                chunk = bytes(src)
                del src[:]
                got = True
                with contextlib.suppress(queue.Full):
                    dst.put_nowait(chunk)
        return got

    def _reconnect(self) -> bool:
        """One more handshake.  False ends the stream for the supervisor."""
        with contextlib.suppress(Exception):
            self._client.close()
        time.sleep(RECONNECT_DELAY)
        if self._stop.is_set():
            return False
        try:
            self._client = self._open()
        except Exception as ex:
            logger.warning("[%s] cannot reconnect: %s", self.pipe_name, ex)
            self._stop.set()
            return False
        return True

    # -- what the bridge reads ------------------------------------------
    def should_stream(self, sleep: float = 0.01) -> bool:
        time.sleep(sleep)
        return not self._stop.is_set()

    def session_check(self):
        return _SessionCheck()

    def identify_audio_codec(self) -> tuple[str, int]:
        return AUDIO_CODEC, AUDIO_RATE

    def update_frame_size_rate(self, bitrate: int | None = None, fps: int = 0) -> None:
        """Record the requested quality.

        The engine picks the camera profile in the AvStreamCtl (1080p or the
        640x360 sub-stream) rather than through the SDK's frame-size ioctl, so
        this only records what was asked for.
        """
        if bitrate:
            self.preferred_bitrate = bitrate
        if fps:
            self.preferred_frame_rate = fps

    @contextmanager
    def iotctrl_mux(self):
        """The SDK's control-channel multiplexer -- not implemented.

        This engine carries no control channel, so anything that needs one
        (power, notifications, timezone) stays unavailable for these cameras.
        """
        raise RuntimeError("ogengine has no control channel")
        yield  # pragma: no cover

    def recv_bridge_data(self):
        """Yield raw H.264, Annex-B frames, no container -- as ffmpeg wants."""
        while self.should_stream(0.005):
            try:
                yield self._video.get(timeout=0.5)
            except queue.Empty:
                continue

    def recv_audio_pipe(self) -> None:
        """Write mu-law audio into the FIFO the bridge's ffmpeg reads."""
        fifo = f"/tmp/{self.pipe_name}_audio.pipe"
        with contextlib.suppress(FileExistsError):
            os.mkfifo(fifo)
        try:
            with open(fifo, "wb", buffering=0) as pipe:
                _nonblocking(pipe)
                while self.should_stream(0.005):
                    try:
                        chunk = self._audio.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    with contextlib.suppress(BlockingIOError):
                        pipe.write(chunk)
        except OSError as ex:
            if ex.errno != errno.EPIPE:  # a reader that went away
                logger.warning("[%s] audio pipe: %s", self.pipe_name, ex)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(fifo)
