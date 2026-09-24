#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["av>=18", "pillow>=10"]
# ///
"""Cover fixed areas of a video with a black box, a blur or pixelation, each
all the time or only during chosen time ranges. GUI wrapper around
ffmpeg; works on Windows and Linux (needs ffmpeg and ffprobe on PATH, and
Tk: on Arch `pacman -S tk`, on Debian/Ubuntu `apt install python3-tk`).

Usage:
    uv run --script blurbox.py [video | project.json]

Areas: drag on the frame to draw the selected area, drag inside any area to
select and move it, drag an edge or corner of the selected area to resize
it; "New area" adds another. Each area has its own effect and time ranges
(none = always).

Seeking: the timeline (click or drag), the step buttons (hold to repeat) or
the keyboard: Left/Right 1 s, Shift+Left/Right one frame.

Ranges: I and O put the current time in Start and End, Enter adds the range;
double-click a range (in the list or on the timeline) to edit it, Esc
cancels. The timeline shows the selected area's ranges in red and the other
areas' in grey; drag an edge of a red range to change that end (the video
follows the edge), or its middle to move the whole range.

"Show effect" (E) draws every area's effect on the frame as it will be
rendered, so only the areas active at that time are covered.

The render keeps the source's codec family (H.264, HEVC, VP9, AV1), bit
depth and colour metadata (HDR included), all audio tracks, subtitles and
chapters, converting whatever the chosen container cannot hold.

A project (.json) saves the video path, areas, quality and position;
Ctrl+S saves, Ctrl+O opens. The video is looked up relative to the project
file first, then by its absolute path.
"""

import copy
import functools
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import av
from PIL import Image, ImageTk

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
# Keep ffmpeg from flashing a console window on Windows
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
POLL_MS = 15  # how often the GUI picks up results from worker threads
LOADING_MS = 80  # show "Loading…" if a frame takes longer than this
REPEAT_DELAY_MS = 400  # hold a step button this long before it repeats
REPEAT_MS = 50  # then step at most this often (and never before the frame shows)
GRAB_PX = 7  # how close (screen pixels) to an edge of the selected area resizes it
MIN_AREA_PX = 2  # smallest width/height (video pixels) a resize can leave
# Mouse cursor per resize handle: l/r = left/right edge, t/b = top/bottom
RESIZE_CURSORS = {
    "l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
    "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
    "lt": "top_left_corner", "rb": "bottom_right_corner",
    "rt": "top_right_corner", "lb": "bottom_left_corner",
}
VIDEO_TYPES = [
    ("Video", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.wmv *.flv *.ts"),
    ("All files", "*.*"),
]
PROJECT_TYPES = [("Blurbox project", "*.json"), ("All files", "*.*")]
OUTPUT_TYPES = [("MP4", "*.mp4"), ("Matroska", "*.mkv"), ("QuickTime", "*.mov"), ("WebM", "*.webm")]
MODES = {"black": "Black box", "blur": "Blur", "pixelate": "Pixelate"}

# What each output container can hold without conversion (None = anything)
AUDIO_OK = {
    ".mp4": {"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac", "mp2"},
    ".mov": {"aac", "mp3", "ac3", "eac3", "alac", "mp2"},  # plus any pcm_*
    ".mkv": None,
    ".webm": {"opus", "vorbis"},
}
LOSSLESS_AUDIO = {"flac", "alac", "truehd", "mlp", "wavpack", "tta", "ape"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    # Video stream start minus file start: ffmpeg's filter time `t` counts
    # from the file start, the slider from the first video frame
    offset: float
    fps: float
    rotation: int  # clockwise degrees applied for display
    codec: str = ""
    pix_fmt: str = ""
    color: dict = field(default_factory=dict)  # ffmpeg option -> value
    audio: list = field(default_factory=list)  # codec name per audio stream
    subtitles: list = field(default_factory=list)  # codec name per subtitle stream
    attachments: int = 0
    data_streams: int = 0

    @property
    def full_range(self) -> bool:
        return self.color.get("-color_range") == "pc" or self.pix_fmt.startswith("yuvj")


@dataclass
class Area:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    mode: str = "black"
    strength: int = 20
    ranges: list = field(default_factory=list)  # (start, end) s; empty = always
    # Cover the entire frame; x/y/w/h are kept, so turning it off restores them
    full: bool = False

    def active(self, t: float, pad: float) -> bool:
        return not self.ranges or any(s - pad <= t <= e + pad for s, e in self.ranges)

    def clipped(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        """The rectangle clipped to the frame, or None if nothing is left."""
        if self.full:
            return 0, 0, width, height
        x, y = min(max(self.x, 0), width - 1), min(max(self.y, 0), height - 1)
        w, h = min(self.w, width - x), min(self.h, height - y)
        return (x, y, w, h) if w >= 2 and h >= 2 else None

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "full": self.full,
                "effect": self.mode, "strength": self.strength,
                "ranges": [[round(s, 4), round(e, 4)] for s, e in self.ranges]}

    @classmethod
    def from_json(cls, d: dict) -> "Area":
        mode = d.get("effect", "black")
        return cls(int(d.get("x", 0)), int(d.get("y", 0)), int(d.get("w", 0)), int(d.get("h", 0)),
                   mode if mode in MODES else "black", max(1, int(d.get("strength", 20))),
                   sorted((float(s), float(e)) for s, e in d.get("ranges", [])),
                   bool(d.get("full", False)))

    def label(self, i: int) -> str:
        eff = MODES[self.mode] + ("" if self.mode == "black" else f" {self.strength}")
        when = f"{len(self.ranges)} range{'s' * (len(self.ranges) != 1)}" if self.ranges else "always"
        if self.full:
            size = "whole frame"
        else:
            size = f"{self.w}×{self.h} at {self.x},{self.y}" if self.w and self.h else "not drawn yet"
        return f"{i + 1}.  {eff},  {size},  {when}"


def fmt_time(t: float) -> str:
    # Round first, so 59.9999 becomes 0:01:00.000 rather than 0:00:60.000
    h, rem = divmod(round(t, 3), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def parse_time(text: str) -> float:
    """Accept seconds (`75.5`), `m:ss` or `h:mm:ss.ff`."""
    parts = text.strip().split(":")
    if not text.strip() or len(parts) > 3:
        raise ValueError(f"Not a valid time: {text!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    if secs < 0:
        raise ValueError(f"Negative time: {text!r}")
    return secs


def probe(path: Path) -> VideoInfo:
    res = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    # Skip cover art, which is stored as a one-picture "video" stream
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise RuntimeError("No video stream found")
    fmt = data.get("format", {})

    width, height = int(video["width"]), int(video["height"])
    # Phone videos store a rotation that ffmpeg applies on decode, so the
    # frames (and the filter coordinates) may have width and height swapped.
    # The old `rotate` tag is clockwise, the display matrix counter-clockwise
    rotation = int(float(video.get("tags", {}).get("rotate", 0)))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = -int(float(side["rotation"]))
    rotation %= 360
    if rotation in (90, 270):
        width, height = height, width

    duration = float(fmt.get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Could not read the video duration")
    num, _, den = video.get("avg_frame_rate", "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    offset = float(video.get("start_time") or 0) - float(fmt.get("start_time") or 0)
    color = {opt: video[key] for key, opt in [
        ("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"), ("color_range", "-color_range")]
        if video.get(key) not in (None, "unknown", "unspecified", "reserved")}
    others = [s for s in streams if s is not video]
    kinds = [s.get("codec_type") for s in others]
    return VideoInfo(
        width, height, duration, offset, fps or 25, rotation,
        codec=video.get("codec_name", ""), pix_fmt=video.get("pix_fmt", ""), color=color,
        audio=[s.get("codec_name", "") for s in others if s.get("codec_type") == "audio"],
        subtitles=[s.get("codec_name", "") for s in others if s.get("codec_type") == "subtitle"],
        attachments=kinds.count("attachment"),
        data_streams=kinds.count("data") + kinds.count("video"))


@functools.cache
def available_encoders() -> frozenset[str]:
    res = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    # The encoder list follows a legend (" V..... = Video") and a "------" line
    _, _, listing = res.stdout.partition("------")
    names = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def effect_chain(mode: str, w: int, h: int, strength: int, full_range: bool) -> list[tuple[str, str]]:
    """ffmpeg filters (name, args) turning a w×h crop into its covered version.
    All of them work at any bit depth, so 10-bit video stays 10-bit."""
    if mode == "black":
        # lutyuv's minval is limited-range black at any bit depth
        y = "0" if full_range else "minval"
        return [("lutyuv", f"y={y}:u=(minval+maxval)/2:v=(minval+maxval)/2")]
    if mode == "blur":
        return [("gblur", f"sigma={strength}")]
    # Pixelate: shrink the area, then blow it back up without smoothing
    return [("scale", f"{max(1, w // strength)}:{max(1, h // strength)}:flags=area"),
            ("scale", f"{w}:{h}:flags=neighbor")]


def apply_effects(img: Image.Image, items: list[tuple["Area", tuple[int, int, int, int]]]) -> Image.Image:
    """Apply areas' effects to one frame, in order, for the on-screen preview.
    Blur and pixelate run the render's own ffmpeg filters in-process."""
    out = img.convert("RGB")
    for area, (x, y, w, h) in items:
        if area.mode == "black":
            out.paste((0, 0, 0), (x, y, x + w, y + h))
            continue
        frame = av.VideoFrame.from_image(out).reformat(format="yuv420p")
        frame.pts = 0
        graph = av.filter.Graph()
        node = graph.add_buffer(width=frame.width, height=frame.height, format="yuv420p",
                                time_base=Fraction(1, 25))
        chain = effect_chain(area.mode, w, h, area.strength, False)
        for name, args in [("crop", f"{w}:{h}:{x}:{y}"), *chain, ("buffersink", None)]:
            nxt = graph.add(name, args)
            node.link_to(nxt)
            node = nxt
        graph.configure()
        graph.push(frame)
        out.paste(graph.pull().to_image(), (x, y))
    return out


def build_filter(items: list[tuple[Area, tuple[int, int, int, int]]], info: VideoInfo) -> str:
    """Filtergraph whose output pad is [v]: each area in turn is cropped,
    covered and laid back over the picture, only during its time ranges."""
    pad = 0.5 / info.fps  # frames exactly at a rounded start/end count as inside
    parts, cur = [], "0:v"
    for i, (area, (x, y, w, h)) in enumerate(items):
        enable = ""
        if area.ranges:
            expr = "+".join(f"between(t,{s + info.offset - pad:.4f},{e + info.offset + pad:.4f})"
                            for s, e in area.ranges)
            enable = f":enable='{expr}'"
        chain = ",".join(f"{n}={a}" for n, a in effect_chain(area.mode, w, h, area.strength,
                                                             info.full_range))
        # format=auto: overlay would otherwise convert 10-bit video to 8-bit
        parts.append(f"[{cur}]split[b{i}][s{i}];[s{i}]crop={w}:{h}:{x}:{y},{chain}[f{i}];"
                     f"[b{i}][f{i}]overlay={x}:{y}:format=auto{enable}[o{i}]")
        cur = f"o{i}"
    # Back to the source pixel format (overlay adds an alpha plane)
    tail = f"format={info.pix_fmt}" if info.pix_fmt else "null"
    parts.append(f"[{cur}]{tail}[v]")
    return ";".join(parts)


def plan_streams(info: VideoInfo, out: Path, crf: int) -> tuple[list[str], list[str]]:
    """Output options that keep as much of the source as `out`'s container
    allows, and notes on anything converted or left out."""
    ext = out.suffix.lower()
    ext = ".mp4" if ext == ".m4v" else ext
    enc = available_encoders()
    notes = []

    # Video: stay in the source's codec family when there is an encoder for it
    if ext == ".webm":
        vcodec = "libsvtav1" if info.codec == "av1" and "libsvtav1" in enc else "libvpx-vp9"
    elif info.codec == "hevc" and "libx265" in enc:
        vcodec = "libx265"
    elif info.codec == "av1" and "libsvtav1" in enc:
        vcodec = "libsvtav1"
    elif info.codec == "vp9" and ext != ".mov" and "libvpx-vp9" in enc:
        vcodec = "libvpx-vp9"
    else:
        vcodec = "libx264"
    family = {"libx264": "h264", "libx265": "hevc", "libsvtav1": "av1", "libvpx-vp9": "vp9"}[vcodec]
    if family != info.codec:
        notes.append(f"Video re-encoded as {family.upper()} (source: {info.codec}).")
    args = ["-c:v", vcodec, "-crf", str(crf)]
    if vcodec == "libx264":
        args += ["-preset", "medium"]
    elif vcodec == "libx265":
        args += ["-preset", "medium", "-x265-params", "log-level=error"]
        if ext in (".mp4", ".mov"):
            args += ["-tag:v", "hvc1"]  # the tag Apple players require for HEVC
    elif vcodec == "libsvtav1":
        args += ["-preset", "6"]
    else:
        args += ["-b:v", "0", "-row-mt", "1"]
    for opt, value in info.color.items():
        args += [opt, value]

    # Audio: copy every track the container accepts, convert the rest
    ok = AUDIO_OK.get(ext)
    for i, codec in enumerate(info.audio):
        args += ["-map", f"0:a:{i}"]
        if ok is None or codec in ok or (ext == ".mov" and codec.startswith("pcm_")):
            args += [f"-c:a:{i}", "copy"]
        elif ext == ".webm":
            args += [f"-c:a:{i}", "libopus", f"-b:a:{i}", "192k"]
            notes.append(f"Audio track {i + 1} ({codec}) converted to Opus 192 kb/s.")
        elif codec.startswith("pcm_") or codec in LOSSLESS_AUDIO:
            args += [f"-c:a:{i}", "alac"]
            notes.append(f"Audio track {i + 1} ({codec}) converted to ALAC (lossless).")
        else:
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", "256k"]
            notes.append(f"Audio track {i + 1} ({codec}) converted to AAC 256 kb/s.")

    # Subtitles: text subtitles fit every container in some form, picture
    # subtitles (DVD, Blu-ray) only Matroska
    k = 0
    for i, codec in enumerate(info.subtitles):
        if ext == ".mkv":
            target = "copy"
        elif codec in TEXT_SUBS:
            target = "webvtt" if ext == ".webm" else "mov_text"
        else:
            notes.append(f"Subtitle track {i + 1} ({codec}, picture-based) left out: "
                         f"only MKV can hold it.")
            continue
        args += ["-map", f"0:s:{i}", f"-c:s:{k}", target]
        k += 1

    if info.attachments:
        if ext == ".mkv":
            args += ["-map", "0:t", "-c:t", "copy"]
        else:
            notes.append(f"{info.attachments} attachment(s) (e.g. subtitle fonts) left out: "
                         f"only MKV can hold them.")
    if info.data_streams:
        notes.append(f"{info.data_streams} extra stream(s) (e.g. GPS, timecode, cover art) left out.")
    if ext in (".mp4", ".mov"):
        args += ["-movflags", "+faststart"]
    return args, notes


def build_render_cmd(src: Path, out: Path, filtergraph: str, stream_args: list[str]) -> list[str]:
    # Metadata and chapters are copied by default; -map_metadata is explicit
    return [FFMPEG, "-y", "-v", "error", "-i", str(src),
            "-filter_complex", filtergraph, "-map", "[v]", *stream_args,
            "-map_metadata", "0", "-progress", "pipe:1", "-nostats", str(out)]


class FrameReader:
    """Decode frames in-process, keeping the file open between requests.
    A step forward continues decoding from where the last request stopped,
    and recently decoded frames are kept so a step back rarely needs a seek."""

    def __init__(self, path: Path, info: VideoInfo):
        self.path = path
        self._open()
        self.tb = float(self.stream.time_base)
        self.t0 = (self.stream.start_time or 0) * self.tb
        self.eps = 0.25 / info.fps
        # PyAV returns frames unrotated; ffmpeg (and the filter) rotate them
        self.transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                          270: Image.Transpose.ROTATE_90}.get(info.rotation)
        # Keep at most ~200 MB of decoded YUV 4:2:0 frames
        per_frame = info.width * info.height * 1.5
        self.recent: deque = deque(maxlen=int(min(64, max(2, 200e6 // per_frame))))
        self.frames = None  # decode iterator, positioned after recent[-1]

    def _open(self):
        self.container = av.open(str(self.path))
        self.stream = next(s for s in self.container.streams.video
                           if not s.disposition & av.stream.Disposition.attached_pic)
        self.stream.thread_type = "AUTO"

    def close(self):
        self.container.close()

    def _seek(self, target: float):
        """Position the decoder so its next frames lead up to `target`. Some
        formats (MPEG-TS above all) seek imprecisely and can land after the
        target or on a frame that cannot be decoded, so step further back
        until decoding starts on a keyframe at or before the target."""
        self.recent.clear()
        back = 0.0
        while target - back > self.t0:
            self.container.seek(int((target - back) / self.tb), stream=self.stream)
            self.frames = self.container.decode(self.stream)
            first = next((f for f in self.frames if f.time is not None), None)
            if first is not None and first.key_frame and first.time <= target + self.eps:
                self.recent.append(first)
                return
            back = back * 2 if back else 1.0
        # Decoding from the very beginning always works: reopen the file
        self.container.close()
        self._open()
        self.frames = self.container.decode(self.stream)

    def get(self, t: float) -> tuple[Image.Image, float] | None:
        """The first frame at or after `t` s from the first frame, with its time."""
        frame = self._find(self.t0 + t)
        if frame is None:
            return None
        img = frame.to_image()
        if self.transpose is not None:
            img = img.transpose(self.transpose)
        return img, frame.time - self.t0

    def _find(self, target: float):
        r = self.recent
        if r and r[0].time - self.eps <= target <= r[-1].time + self.eps:
            return next(f for f in r if f.time >= target - self.eps)
        # Decoding on is cheaper than seeking back to a keyframe, up to a point
        if self.frames is None or not r or target < r[-1].time or target > r[-1].time + 5:
            self._seek(target)
            if r and r[-1].time >= target - self.eps:
                return r[-1]
        for f in self.frames:
            if f.time is None:
                continue
            r.append(f)
            if f.time >= target - self.eps:
                return f
        self.frames = None  # past the end: show the last frame
        return r[-1] if r else None


class FrameWorker(threading.Thread):
    """Serve frame requests one at a time on a background thread. A new
    request replaces one still waiting, so scrubbing never builds a backlog."""

    def __init__(self, events: queue.Queue):
        super().__init__(daemon=True)
        self.events = events
        self.cond = threading.Condition()
        self.pending = None
        self.reader: FrameReader | None = None
        self.path: Path | None = None

    def submit(self, gen: int, path: Path, info: VideoInfo, t: float):
        with self.cond:
            self.pending = (gen, path, info, t)
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while self.pending is None:
                    self.cond.wait()
                gen, path, info, t = self.pending
                self.pending = None
            try:
                if path != self.path:
                    if self.reader:
                        self.reader.close()
                    self.reader, self.path = None, None
                    self.reader, self.path = FrameReader(path, info), path
                result = self.reader.get(t)
            except Exception as e:
                self.path = None  # reopen on the next request
                result = e
            self.events.put(("frame", gen, result))


class Timeline(tk.Canvas):
    """Seek bar that also shows the time ranges: the selected area's in red,
    the other areas' as a thin grey strip. Click or drag to seek; drag an
    edge of a red range to change that end, or its middle to move it."""

    HEIGHT, MARGIN = 34, 8
    BAND_TOP, BAND_BOTTOM = 15, 26  # the selected area's ranges
    EDGE_PX = 5  # how close to a range edge (screen pixels) grabs the edge
    DRAG_PX = 3  # movement that turns a click on a range into a drag

    def __init__(self, master, app: "App"):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0)
        self.app = app
        # A press on a range: (index, "start"/"end"/"move", press x, range
        # at the press); it becomes a drag once the mouse moves DRAG_PX
        self.grab = None
        self.dragging = False
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double)
        self.bind("<Motion>", self._hover)

    def _span(self) -> tuple[int, int]:
        return self.MARGIN, max(self.MARGIN + 1, self.winfo_width() - self.MARGIN)

    def x_of(self, t: float) -> float:
        a, b = self._span()
        return a + (b - a) * t / self.app.info.duration

    def t_of(self, x: float) -> float:
        a, b = self._span()
        return (x - a) / (b - a) * self.app.info.duration

    def _range_at(self, x: float, y: float) -> tuple[int, str] | None:
        """The selected area's range under a point, and which part: its
        "start" or "end" edge, or its middle ("move"). Edges win, so a short
        range can still be stretched."""
        cur = self.app.current() if self.app.info else None
        if not cur or not cur.ranges or not self.BAND_TOP - 3 <= y <= self.BAND_BOTTOM + 3:
            return None
        edges = []
        for j, (s, e) in enumerate(cur.ranges):
            xs, xe = self.x_of(s), max(self.x_of(e), self.x_of(s) + 2)
            edges += [(abs(x - xs), j, "start"), (abs(x - xe), j, "end")]
        dist, j, part = min(edges)
        if dist <= self.EDGE_PX:
            return j, part
        return next(((j, "move") for j, (s, e) in enumerate(cur.ranges)
                     if self.x_of(s) < x < self.x_of(e)), None)

    def _hover(self, e):
        hit = self._range_at(e.x, e.y)
        cursor = "" if not hit else "fleur" if hit[1] == "move" else "sb_h_double_arrow"
        if self.cget("cursor") != cursor:
            self.config(cursor=cursor)

    def _press(self, e):
        if not self.app.info:
            return
        hit = self._range_at(e.x, e.y)
        if hit:
            j, part = hit
            self.grab = (j, part, e.x, self.app.current().ranges[j])
            self.dragging = False
            return
        self.app.scrubbing = True
        self._seek_to(e.x)

    def _motion(self, e):
        if not self.grab:
            self._seek_to(e.x)
            return
        j, part, x0, (s0, e0) = self.grab
        if not self.dragging:
            if abs(e.x - x0) < self.DRAG_PX:
                return
            self.dragging = True
            self.app.cancel_edit()  # dragging is itself the edit
            self.app.scrubbing = True
        info = self.app.info
        frame = 1 / info.fps

        def snap(t):  # to whole frames, like the times the player shows
            return round(t / frame) * frame

        dt = self.t_of(e.x) - self.t_of(x0)
        if part == "start":
            s, en = min(max(snap(s0 + dt), 0.0), e0 - frame), e0
        elif part == "end":
            s, en = s0, max(min(snap(e0 + dt), info.duration), s0 + frame)
        else:  # keep the length, stay inside the video
            s = min(max(snap(s0 + dt), 0.0), info.duration - (e0 - s0))
            en = s + (e0 - s0)
        self.app.current().ranges[j] = (s, en)
        self.redraw()
        # Show the frame at the edge being dragged, to place it precisely
        self.app.seek(en if part == "end" else s)

    def _release(self, e):
        if not self.grab:
            self.app._scrub_end()
            return
        grab, dragging = self.grab, self.dragging
        self.grab, self.dragging = None, False
        if not dragging:  # a click on a range seeks, as anywhere else
            self.app.scrubbing = True
            self._seek_to(e.x)
            self.app._scrub_end()
            return
        self.app.range_dragged(grab[0])
        self.app._scrub_end()

    def _double(self, e):
        """Double-click on a range: edit it, as a double-click in the list.
        (Tk sends this instead of the second press, so the first click has
        already seeked, as any click does.)"""
        hit = self._range_at(e.x, e.y)
        if hit:
            self.app.edit_range(hit[0])

    def _seek_to(self, x: float):
        if self.app.info:
            self.app.seek(self.t_of(x))

    def redraw(self):
        self.delete("all")
        app = self.app
        if not app.info:
            return
        a, b = self._span()
        self.create_rectangle(a, 9, b, 27, fill="#e4e4e4", outline="#a8a8a8")
        for i, area in enumerate(app.areas):
            if i != app.cur:
                for s, e in area.ranges or [(0, app.info.duration)]:
                    self.create_rectangle(self.x_of(s), 10, self.x_of(e), 14,
                                          fill="#8c8c8c", width=0)
        cur = app.current()
        if cur:
            dragged = self.grab[0] if self.dragging else None
            for j, (s, e) in enumerate(cur.ranges or [(0, app.info.duration)]):
                editing = cur.ranges and j in (app.editing, dragged)
                self.create_rectangle(self.x_of(s), 15, max(self.x_of(e), self.x_of(s) + 2), 26,
                                      fill="#f2b8ad" if not cur.ranges else "#e0503c",
                                      outline="#1060d0" if editing else "", width=2 if editing else 0)
        self.draw_playhead()

    def draw_playhead(self):
        self.delete("ph")
        if not self.app.info:
            return
        x = self.x_of(self.app.pos.get())
        self.create_line(x, 4, x, 31, fill="#101010", width=2, tags="ph")
        self.create_polygon(x - 5, 2, x + 5, 2, x, 8, fill="#101010", tags="ph")


class App:
    def __init__(self, root: tk.Tk, path: str | None = None):
        self.root = root
        root.title("Blurbox")
        root.geometry("1150x900")
        root.minsize(900, 650)

        self.video: Path | None = None
        self.info: VideoInfo | None = None
        self.project: Path | None = None  # project file, once saved or opened
        self.saved: dict | None = None  # snapshot at the last save/load
        self.areas: list[Area] = []
        self.cur: int | None = None  # selected area
        self.editing: int | None = None  # index of the range being edited
        self.frame: Image.Image | None = None
        self.photo = None
        self.scale, self.offx, self.offy = 1.0, 0, 0
        self.gen = 0  # number of the latest frame request
        self.shown_gen = 0  # request whose frame is on screen
        self.shown_time: float | None = None
        self.loading = False
        self.loading_after = None
        self.repeat_after = None
        self.redraw_pending = False
        self.syncing = False  # filling the widgets from an area: ignore traces
        self.scrubbing = False  # timeline being dragged
        self.drag = None
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.events: queue.Queue = queue.Queue()
        self.worker = FrameWorker(self.events)
        self.worker.start()

        self.pos = tk.DoubleVar(value=0)
        self.time_text = tk.StringVar(value="")
        self.rect_vars = {k: tk.StringVar(value="0") for k in "xywh"}
        self.mode = tk.StringVar(value="black")
        self.strength = tk.StringVar(value="20")
        self.crf = tk.StringVar(value="18")
        self.start_text = tk.StringVar()
        self.end_text = tk.StringVar()
        self.show_effect = tk.BooleanVar(value=True)
        self.full = tk.BooleanVar(value=False)  # selected area covers the whole frame
        self.status = tk.StringVar(value="Open a video to start.")

        self._build()
        for v in (*self.rect_vars.values(), self.mode, self.strength, self.full):
            v.trace_add("write", lambda *_: self._area_edited())
        self.show_effect.trace_add("write", lambda *_: self.schedule_redraw())
        self.pos.trace_add("write", lambda *_: self.timeline.draw_playhead())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_after = root.after(POLL_MS, self._poll)
        if path and path.lower().endswith(".json"):
            self.open_project(path)
        elif path:
            self.open_video(path)

    # UI layout ------------------------------------------------------------

    def _build(self):
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="Open video…", command=self.choose_video).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="Open project…", command=self.open_project).pack(side="left")
        ttk.Button(top, text="Save project", command=self.save_project).pack(side="left", padx=2)
        ttk.Button(top, text="Save project as…",
                   command=lambda: self.save_project(ask=True)).pack(side="left")
        self.file_label = ttk.Label(top, text="")
        self.file_label.pack(side="left", padx=8)

        self.canvas = tk.Canvas(self.root, background="#202020", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, **pad)
        self.canvas.bind("<Configure>", lambda e: self.render_canvas())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<Motion>", self._hover)

        seek = ttk.Frame(self.root)
        seek.pack(fill="x", **pad)
        for text, step, frame in [("◀ 1 s", -1, False), ("◀ frame", -1, True),
                                  ("frame ▶", 1, True), ("1 s ▶", 1, False)]:
            self._repeat_button(seek, text, lambda s=step, f=frame: self.step(s, f))
        self.timeline = Timeline(seek, self)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Label(seek, textvariable=self.time_text, width=26, anchor="e").pack(side="left")

        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        ttk.Checkbutton(opts, text="Show effect (E)", variable=self.show_effect).pack(side="left")
        ttk.Label(opts, text="     Keys: ←/→ 1 s, Shift+←/→ 1 frame, I/O mark start/end, "
                             "Enter add range, Esc cancel edit").pack(side="left")

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)

        areas = ttk.LabelFrame(bottom, text="Areas")
        areas.pack(side="left", fill="y", padx=(0, 6))
        self.area_list = tk.Listbox(areas, height=7, width=46, exportselection=False)
        self.area_list.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self.area_list.bind("<<ListboxSelect>>", self._area_selected)
        ab = ttk.Frame(areas)
        ab.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(ab, text="New area", command=self.new_area).pack(side="left")
        ttk.Button(ab, text="Duplicate", command=self.duplicate_area).pack(side="left", padx=2)
        ttk.Button(ab, text="Delete", command=self.delete_area).pack(side="left")

        sel = ttk.LabelFrame(bottom, text="Selected area")
        sel.pack(side="left", fill="both", expand=True)
        sel.columnconfigure(1, weight=1)

        ttk.Label(sel, text="Position (px)").grid(row=0, column=0, sticky="w", **pad)
        area = ttk.Frame(sel)
        area.grid(row=0, column=1, sticky="w")
        self.rect_boxes = []
        for k in "xywh":
            ttk.Label(area, text=k).pack(side="left", padx=(8, 2))
            box = ttk.Spinbox(area, from_=0, to=99999, width=6, textvariable=self.rect_vars[k])
            box.pack(side="left")
            self.rect_boxes.append(box)
        ttk.Checkbutton(area, text="Whole frame", variable=self.full).pack(side="left", padx=(14, 0))

        ttk.Label(sel, text="Effect").grid(row=1, column=0, sticky="w", **pad)
        eff = ttk.Frame(sel)
        eff.grid(row=1, column=1, sticky="w")
        for value, label in MODES.items():
            ttk.Radiobutton(eff, text=label, value=value, variable=self.mode).pack(side="left", padx=4)
        self.strength_label = ttk.Label(eff, text="")
        self.strength_label.pack(side="left", padx=(16, 2))
        self.strength_box = ttk.Spinbox(eff, from_=1, to=200, width=5, textvariable=self.strength)
        self.strength_box.pack(side="left")

        ttk.Label(sel, text="Time ranges").grid(row=2, column=0, sticky="nw", **pad)
        tr = ttk.Frame(sel)
        tr.grid(row=2, column=1, sticky="w")
        row = ttk.Frame(tr)
        row.pack(fill="x")
        for label, var, key in [("Start", self.start_text, "I"), ("End", self.end_text, "O")]:
            ttk.Label(row, text=label).pack(side="left", padx=(0, 2))
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side="left")
            entry.bind("<Return>", lambda e: self.add_range())
            ttk.Button(row, text=f"← current ({key})", width=14,
                       command=lambda v=var: self.mark(v)).pack(side="left", padx=(2, 10))
        self.add_btn = ttk.Button(row, text="Add range", width=13, command=self.add_range)
        self.add_btn.pack(side="left")
        # Shown only while a range is being edited (see _show_editing)
        self.cancel_edit_btn = ttk.Button(row, text="Cancel", command=self.cancel_edit)
        ttk.Button(row, text="Remove", command=self.remove_range).pack(side="left", padx=4)
        row2 = ttk.Frame(tr)
        row2.pack(fill="x", pady=(3, 0))
        self.range_list = tk.Listbox(row2, height=4, width=34, exportselection=False)
        self.range_list.pack(side="left")
        self.range_list.bind("<<ListboxSelect>>", self._range_selected)
        self.range_list.bind("<Double-Button-1>", lambda e: self.edit_range())
        ttk.Label(row2, text="  No ranges = always (start to end).\n  Double-click a range to edit it.\n"
                             "  Times: seconds, m:ss or h:mm:ss.fff").pack(side="left", anchor="n")

        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        ttk.Label(act, text="Quality (CRF, lower = better)").pack(side="left", padx=(0, 2))
        ttk.Spinbox(act, from_=0, to=63, width=4, textvariable=self.crf).pack(side="left")
        self.render_btn = ttk.Button(act, text="Render…", command=self.render)
        self.render_btn.pack(side="left", padx=(12, 4))
        self.cancel_btn = ttk.Button(act, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")
        self.progress = ttk.Progressbar(act, maximum=100, length=200)
        self.progress.pack(side="left", padx=8)
        ttk.Label(act, textvariable=self.status).pack(side="left", fill="x", expand=True)

        r = self.root
        r.bind("<Control-s>", lambda e: self.save_project())
        r.bind("<Control-o>", lambda e: self.open_project())
        for key, step in [("Left", -1), ("Right", 1)]:
            r.bind(f"<{key}>", lambda e, s=step: self._key(e, lambda: self.step(s), arrows=True))
            r.bind(f"<Shift-{key}>", lambda e, s=step:
                   self._key(e, lambda: self.step(s, True), arrows=True))
        for key, action in [("i", lambda: self.mark(self.start_text)),
                            ("o", lambda: self.mark(self.end_text)),
                            ("e", lambda: self.show_effect.set(not self.show_effect.get()))]:
            r.bind(f"<Key-{key}>", lambda e, a=action: self._key(e, a))
            r.bind(f"<Key-{key.upper()}>", lambda e, a=action: self._key(e, a))
        r.bind("<Return>", lambda e: self._key(e, self.add_range))
        r.bind("<Escape>", lambda e: self.cancel_edit())
        self._mode_changed()

    def _repeat_button(self, parent, text: str, action):
        """A button that acts on press and, held down, repeats after a delay.
        Each repeat waits for the previous frame to be on screen."""
        b = ttk.Button(parent, text=text, width=8)
        b.pack(side="left", padx=(0, 2))

        def tick():
            if self.shown_gen == self.gen:
                action()
            self.repeat_after = self.root.after(REPEAT_MS, tick)

        def press(_):
            self._stop_repeat()
            action()
            self.repeat_after = self.root.after(REPEAT_DELAY_MS, tick)

        b.bind("<ButtonPress-1>", press, add="+")
        b.bind("<ButtonRelease-1>", lambda e: self._stop_repeat(), add="+")
        b.bind("<Leave>", lambda e: self._stop_repeat(), add="+")

    def _stop_repeat(self):
        if self.repeat_after:
            self.root.after_cancel(self.repeat_after)
            self.repeat_after = None

    def _key(self, event, action, arrows: bool = False):
        # Letters and Enter belong to the text fields; arrows also to lists
        typing = (tk.Entry, ttk.Entry, ttk.Spinbox) + ((tk.Listbox,) if arrows else ())
        if not isinstance(event.widget, typing) and self.info:
            action()

    def _mode_changed(self):
        mode = self.mode.get()
        self.strength_label.config(text={"black": "", "blur": "Blur strength",
                                         "pixelate": "Block size (px)"}[mode])
        self.strength_box.config(state="disabled" if mode == "black" else "normal")

    # Video and frames -----------------------------------------------------

    def choose_video(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(title="Open video", filetypes=VIDEO_TYPES)
        if path:
            self.open_video(path)

    def open_video(self, path: str) -> bool:
        try:
            info = probe(Path(path))
        except Exception as e:
            messagebox.showerror("Cannot open video", str(e))
            return False
        self.video, self.info = Path(path), info
        self.project = None
        self.frame = None
        self.shown_time = None
        self.cancel_edit()
        self.areas, self.cur = [], None
        self._refresh_areas()
        depth = "10-bit " if "10" in info.pix_fmt else "12-bit " if "12" in info.pix_fmt else ""
        hdr = " HDR" if info.color.get("-color_trc") in ("smpte2084", "arib-std-b67") else ""
        self.file_label.config(text=f"{self.video.name}   {info.width}×{info.height}, "
                                    f"{fmt_time(info.duration)}, {info.fps:.2f} fps, "
                                    f"{depth}{info.codec.upper()}{hdr}")
        self.status.set("Drag on the frame to draw the first area.")
        self.saved = self._snapshot()
        self._update_title()
        self.seek(0)
        return True

    # Projects -------------------------------------------------------------

    def _snapshot(self) -> dict:
        """Everything a project stores except the playback position."""
        return {"areas": [a.to_json() for a in self.areas], "crf": self.crf.get()}

    def _dirty(self) -> bool:
        return self.video is not None and self._snapshot() != self.saved

    def _confirm_discard(self) -> bool:
        """True if it is fine to replace the current work."""
        if not self._dirty():
            return True
        answer = messagebox.askyesnocancel("Unsaved changes", "Save the current project first?")
        if answer is None:
            return False
        return self.save_project() if answer else True

    def _update_title(self):
        name = self.project.name if self.project else (self.video.name if self.video else "")
        self.root.title(f"{name} – Blurbox" if name else "Blurbox")

    def save_project(self, ask: bool = False) -> bool:
        if not self.video:
            messagebox.showerror("Cannot save", "Open a video first.")
            return False
        path = self.project
        if ask or path is None:
            path = filedialog.asksaveasfilename(
                title="Save project", initialdir=self.video.parent,
                initialfile=f"{self.video.stem}_blurbox.json", defaultextension=".json",
                filetypes=PROJECT_TYPES)
            if not path:
                return False
            path = Path(path)
        try:
            crf = int(self.crf.get())
        except ValueError:
            crf = 18

        # The video is found relative to the project first, so a folder
        # holding both can be moved or copied to another PC
        try:
            rel = Path(os.path.relpath(self.video.resolve(), path.resolve().parent)).as_posix()
        except ValueError:  # different drive on Windows
            rel = None
        data = {
            "app": "blurbox",
            "version": 1,
            "video": rel,
            "video_absolute": str(self.video.resolve()),
            "areas": [a.to_json() for a in self.areas],
            "crf": crf,
            "position": round(self.pos.get(), 4),
        }
        try:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        except OSError as e:
            messagebox.showerror("Cannot save project", str(e))
            return False
        self.project = path
        self.saved = self._snapshot()
        self._update_title()
        self.status.set(f"Project saved: {path}")
        return True

    def open_project(self, path: str | None = None):
        if not self._confirm_discard():
            return
        if path is None:
            path = filedialog.askopenfilename(title="Open project", filetypes=PROJECT_TYPES)
            if not path:
                return
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("app") != "blurbox" or "areas" not in data:
                raise ValueError("Not a blurbox project file.")
            areas = [Area.from_json(d) for d in data["areas"]]
        except (OSError, ValueError, TypeError, KeyError) as e:
            messagebox.showerror("Cannot open project", str(e))
            return

        candidates = []
        if data.get("video"):
            candidates.append(path.parent / data["video"])
        if data.get("video_absolute"):
            candidates.append(Path(data["video_absolute"]))
        video = next((c for c in candidates if c.is_file()), None)
        if video is None:
            missing = candidates[-1].name if candidates else "the video"
            messagebox.showwarning("Video not found", f"Cannot find {missing}. Please locate it.")
            chosen = filedialog.askopenfilename(title=f"Locate {missing}", filetypes=VIDEO_TYPES)
            if not chosen:
                return
            video = Path(chosen)
        if not self.open_video(str(video)):
            return

        self.areas = areas
        self.cur = 0 if areas else None
        self.crf.set(str(data.get("crf", 18)))
        self._refresh_areas()
        self.project = path
        # A relocated video counts as a change, so the new path gets saved
        self.saved = self._snapshot() if video in candidates else None
        self._update_title()
        self.status.set(f"Project loaded: {path}")
        self.seek(float(data.get("position", 0)))

    # Seeking --------------------------------------------------------------

    def seek(self, t: float):
        if self.info:
            self.pos.set(min(max(t, 0), self.info.duration))
            self._show_time()
            self.request_frame()

    def _show_time(self):
        self.time_text.set(f"{fmt_time(self.pos.get())} / {fmt_time(self.info.duration)}")

    def _snap(self):
        """Move the playhead exactly onto the time of the frame on screen, so
        marks and frame steps use real frame timestamps."""
        if self.shown_time is not None and self.shown_gen == self.gen:
            self.pos.set(self.shown_time)
            self._show_time()

    def _scrub_end(self, _=None):
        self.scrubbing = False
        self._snap()
        if self.shown_gen != self.gen:
            self._arm_loading()

    def step(self, step: int, frame: bool = False):
        """Move `step` seconds, or `step` frames when `frame` is true."""
        if self.info:
            self.seek(self.pos.get() + step * (1 / self.info.fps if frame else 1))

    def request_frame(self):
        if not self.video:
            return
        self.gen += 1
        self.worker.submit(self.gen, self.video, self.info, self.pos.get())
        self._arm_loading()

    def _arm_loading(self):
        # A frame that takes a while is replaced by "Loading…" rather than
        # leaving a stale one on screen; quick ones just swap in
        if self.loading_after:
            self.root.after_cancel(self.loading_after)
        self.loading_after = self.root.after(LOADING_MS, self._show_loading)

    def _show_loading(self):
        self.loading_after = None
        # While scrubbing, intermediate frames are more useful than a placeholder
        if self.shown_gen != self.gen and not self.scrubbing and not self.loading:
            self.loading = True
            self.render_canvas()

    # Drawing --------------------------------------------------------------

    def schedule_redraw(self):
        """Redraw once when Tk is idle, however many edits come in first."""
        if not self.redraw_pending:
            self.redraw_pending = True
            self.root.after_idle(self._redraw)

    def _redraw(self):
        self.redraw_pending = False
        self.render_canvas()
        self.timeline.redraw()

    def _active_items(self, t: float) -> list[tuple[Area, tuple[int, int, int, int]]]:
        pad = 0.5 / self.info.fps
        items = []
        for a in self.areas:
            rect = a.clipped(self.info.width, self.info.height)
            if rect and a.active(t, pad):
                items.append((a, rect))
        return items

    def render_canvas(self):
        self.canvas.delete("img")
        self.canvas.delete("rect")
        if self.loading:
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                    text="Loading…", fill="#c0c0c0", font=("TkDefaultFont", 16),
                                    tags="img")
            return
        if self.frame is None:
            return
        img = self.frame
        if self.show_effect.get():
            t = self.shown_time if self.shown_time is not None else self.pos.get()
            try:
                img = apply_effects(img, self._active_items(t))
            except Exception as e:
                self.status.set(f"Preview error: {e}")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        iw, ih = img.size
        s = min(cw / iw, ch / ih)
        size = (max(1, int(iw * s)), max(1, int(ih * s)))
        self.photo = ImageTk.PhotoImage(img.resize(size, Image.Resampling.BILINEAR))
        self.scale = size[0] / iw
        self.offx, self.offy = (cw - size[0]) // 2, (ch - size[1]) // 2
        self.canvas.create_image(self.offx, self.offy, anchor="nw", image=self.photo, tags="img")
        self.canvas.tag_lower("img")
        self.draw_areas()

    def draw_areas(self):
        """Outlines: the selected area red, the others yellow; thin and marked
        "off" when the area is not active at the current time."""
        self.canvas.delete("rect")
        if self.frame is None or self.loading:
            return
        t = self.shown_time if self.shown_time is not None else self.pos.get()
        pad = 0.5 / self.info.fps
        order = [i for i in range(len(self.areas)) if i != self.cur]
        order += [self.cur] if self.cur is not None else []
        for i in order:  # selected area last, on top
            a = self.areas[i]
            if not a.full and (a.w <= 0 or a.h <= 0):
                continue
            x, y, w, h = (0, 0, self.info.width, self.info.height) if a.full else (a.x, a.y, a.w, a.h)
            c = [self.offx + x * self.scale, self.offy + y * self.scale,
                 self.offx + (x + w) * self.scale, self.offy + (y + h) * self.scale]
            color = "#ff3030" if i == self.cur else "#ffd000"
            # Thin outline and "off" when the area is not covering this frame
            # (Tk on Windows draws wide dashed lines as grey dots, so no dashes)
            on = a.active(t, pad)
            self.canvas.create_rectangle(*c, outline="black", width=4 if on else 3, tags="rect")
            self.canvas.create_rectangle(*c, outline=color, width=2 if on else 1, tags="rect")
            label = self.canvas.create_text(c[0] + 5, c[1] + 3, anchor="nw",
                                            text=str(i + 1) if on else f"{i + 1} off",
                                            fill=color, font=("TkDefaultFont", 11, "bold"),
                                            tags="rect")
            box = self.canvas.bbox(label)
            tag = self.canvas.create_rectangle(box[0] - 3, box[1] - 1, box[2] + 3, box[3] + 1,
                                               fill="black", outline="", tags="rect")
            self.canvas.tag_lower(tag, label)
            if i == self.cur and not a.full:  # resize handles: corners and edge midpoints
                mx, my = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2
                for hx, hy in [(c[0], c[1]), (mx, c[1]), (c[2], c[1]), (c[2], my),
                               (c[2], c[3]), (mx, c[3]), (c[0], c[3]), (c[0], my)]:
                    self.canvas.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="white",
                                                 outline="black", tags="rect")

    # Areas ----------------------------------------------------------------

    def current(self, create: bool = False) -> Area | None:
        if self.cur is None and create and self.info:
            self.areas.append(Area(mode=self.mode.get(), strength=self._strength_or(20)))
            self.cur = len(self.areas) - 1
            self._refresh_areas()
        return self.areas[self.cur] if self.cur is not None else None

    def _strength_or(self, default: int) -> int:
        try:
            return max(1, int(self.strength.get()))
        except ValueError:
            return default

    def _refresh_areas(self):
        self.area_list.delete(0, "end")
        for i, a in enumerate(self.areas):
            self.area_list.insert("end", a.label(i))
        if self.cur is not None:
            self.area_list.selection_set(self.cur)
            self.area_list.see(self.cur)
        self._sync_widgets()

    def _sync_widgets(self):
        """Show the selected area in the widgets on the right."""
        a = self.current()
        self.syncing = True
        try:
            for k in "xywh":
                self.rect_vars[k].set(str(getattr(a, k)) if a else "0")
            self.full.set(bool(a and a.full))
            if a:
                self.mode.set(a.mode)
                self.strength.set(str(a.strength))
        finally:
            self.syncing = False
        self._mode_changed()
        self._full_changed()
        self._refresh_ranges()

    def _full_changed(self):
        # A whole-frame area has no rectangle to type in
        state = "disabled" if self.full.get() else "normal"
        for box in self.rect_boxes:
            box.config(state=state)

    def _area_edited(self):
        """A widget of the selected area changed: store it in the area."""
        if self.syncing or not self.info:
            return
        self._mode_changed()
        self._full_changed()
        a = self.current(create=True)
        if a is None:
            return
        try:
            a.x, a.y, a.w, a.h = (max(0, int(float(self.rect_vars[k].get()))) for k in "xywh")
        except ValueError:
            pass  # half-typed number: keep the last valid one
        a.mode = self.mode.get()
        a.strength = self._strength_or(a.strength)
        a.full = self.full.get()
        self.area_list.delete(self.cur)
        self.area_list.insert(self.cur, a.label(self.cur))
        self.area_list.selection_set(self.cur)
        self.schedule_redraw()

    def _area_selected(self, _):
        sel = self.area_list.curselection()
        if sel and sel[0] != self.cur:
            self.select_area(sel[0])

    def select_area(self, i: int):
        self.cancel_edit()  # an edit belongs to the area it started in
        self.cur = i
        self._refresh_areas()
        self.schedule_redraw()

    def new_area(self):
        if not self.info:
            return
        a = self.current()
        if a and not a.full and (a.w <= 0 or a.h <= 0):
            # The selected area is still empty: draw that one rather than
            # piling up undrawn areas, which would then block the render
            self.status.set(f"Area {self.cur + 1} is not drawn yet: drag on the frame to draw it.")
            return
        self.areas.append(Area(mode=self.mode.get(), strength=self._strength_or(20)))
        self.select_area(len(self.areas) - 1)
        self.status.set(f"Drag on the frame to draw area {self.cur + 1}.")

    def duplicate_area(self):
        a = self.current()
        if not a:
            return
        b = copy.deepcopy(a)
        b.x = min(b.x + 20, max(0, self.info.width - b.w))
        b.y = min(b.y + 20, max(0, self.info.height - b.h))
        self.areas.insert(self.cur + 1, b)
        self.select_area(self.cur + 1)

    def delete_area(self):
        if self.cur is None:
            return
        self.cancel_edit()
        del self.areas[self.cur]
        self.cur = min(self.cur, len(self.areas) - 1) if self.areas else None
        self._refresh_areas()
        self.schedule_redraw()

    def set_rect(self, x, y, w, h):
        for k, v in zip("xywh", (x, y, w, h)):
            self.rect_vars[k].set(str(int(v)))

    def _to_video(self, cx, cy) -> tuple[int, int]:
        vx = round((cx - self.offx) / self.scale)
        vy = round((cy - self.offy) / self.scale)
        return min(max(vx, 0), self.info.width), min(max(vy, 0), self.info.height)

    def _area_at(self, vx, vy) -> int | None:
        """The area under the point: the selected one first, then the top one.
        Whole-frame areas are left out: they would catch every click."""
        def inside(a):
            return (not a.full and a.w > 0 and a.h > 0
                    and a.x <= vx <= a.x + a.w and a.y <= vy <= a.y + a.h)
        if self.cur is not None and inside(self.areas[self.cur]):
            return self.cur
        return next((i for i in reversed(range(len(self.areas))) if inside(self.areas[i])), None)

    def _handle_at(self, cx, cy) -> str | None:
        """The resize handle of the selected area under a screen point: which
        edges it moves, "l"/"r" then "t"/"b" (e.g. "rb" = bottom-right
        corner), or None. Works anywhere along an edge, not only on the
        drawn squares."""
        a = self.current()
        if self.frame is None or self.loading or not a or a.full or a.w <= 0 or a.h <= 0:
            return None
        left, top = self.offx + a.x * self.scale, self.offy + a.y * self.scale
        right, bottom = left + a.w * self.scale, top + a.h * self.scale
        in_x = left - GRAB_PX <= cx <= right + GRAB_PX
        in_y = top - GRAB_PX <= cy <= bottom + GRAB_PX
        handle = ""
        # On a tiny area both edges are in reach: take the nearer one
        dl, dr = abs(cx - left), abs(cx - right)
        if in_y and min(dl, dr) <= GRAB_PX:
            handle += "l" if dl < dr else "r"
        dt, db = abs(cy - top), abs(cy - bottom)
        if in_x and min(dt, db) <= GRAB_PX:
            handle += "t" if dt < db else "b"
        return handle or None

    def _hover(self, e):
        """Show with the cursor what a drag from here would do."""
        if self.frame is None or self.loading:
            cursor = ""
        elif handle := self._handle_at(e.x, e.y):
            cursor = RESIZE_CURSORS[handle]
        elif self._area_at(*self._to_video(e.x, e.y)) is not None:
            cursor = "fleur"
        else:
            cursor = "crosshair"
        if self.canvas.cget("cursor") != cursor:
            self.canvas.config(cursor=cursor)

    def _press(self, e):
        if self.frame is None or self.loading:
            return
        # An edge of the selected area resizes it, even where it overlaps
        # another area
        handle = self._handle_at(e.x, e.y)
        if handle:
            a = self.areas[self.cur]
            self.drag = ("resize", handle, (a.x, a.y, a.x + a.w, a.y + a.h))
            return
        vx, vy = self._to_video(e.x, e.y)
        hit = self._area_at(vx, vy)
        if hit is not None:
            if hit != self.cur:
                self.select_area(hit)
            a = self.areas[hit]
            self.drag = ("move", vx - a.x, vy - a.y)
        else:
            a = self.current(create=True)
            if a.full:  # it has no rectangle to redraw: start a new area
                self.new_area()
            self.drag = ("new", vx, vy)

    def _motion(self, e):
        if not self.drag or self.cur is None:
            return
        vx, vy = self._to_video(e.x, e.y)  # already clamped to the frame
        kind, a, b = self.drag
        if kind == "move":
            area = self.areas[self.cur]
            x = min(max(vx - a, 0), self.info.width - area.w)
            y = min(max(vy - b, 0), self.info.height - area.h)
            self.set_rect(x, y, area.w, area.h)
        elif kind == "resize":
            # Move only the grabbed edges; an edge stops short of the
            # opposite one instead of flipping the area over
            x0, y0, x1, y1 = b
            if "l" in a:
                x0 = min(vx, x1 - MIN_AREA_PX)
            if "r" in a:
                x1 = max(vx, x0 + MIN_AREA_PX)
            if "t" in a:
                y0 = min(vy, y1 - MIN_AREA_PX)
            if "b" in a:
                y1 = max(vy, y0 + MIN_AREA_PX)
            self.set_rect(x0, y0, x1 - x0, y1 - y0)
        else:
            self.set_rect(min(a, vx), min(b, vy), abs(vx - a), abs(vy - b))

    # Time ranges ----------------------------------------------------------

    def mark(self, var: tk.StringVar):
        if self.info:
            var.set(fmt_time(self.pos.get()))

    def add_range(self):
        """Add the Start/End range to the selected area, or update the range
        being edited."""
        if not self.info:
            return
        try:
            s, e = parse_time(self.start_text.get()), parse_time(self.end_text.get())
        except ValueError as err:
            messagebox.showerror("Invalid time", str(err))
            return
        e = min(e, self.info.duration)
        if s >= e:
            messagebox.showerror("Invalid range", "The start must come before the end.")
            return
        a = self.current(create=True)
        if self.editing is not None:
            a.ranges[self.editing] = (s, e)
        else:
            a.ranges.append((s, e))
        a.ranges.sort()
        self.editing = None
        self.start_text.set("")
        self.end_text.set("")
        self.status.set("")
        self._show_editing()
        self._refresh_areas()
        self.schedule_redraw()

    def remove_range(self):
        a = self.current()
        if not a:
            return
        for i in reversed(self.range_list.curselection()):
            del a.ranges[i]
        self.cancel_edit()
        self._refresh_areas()
        self.schedule_redraw()

    def edit_range(self, j: int | None = None):
        """Start editing range `j` of the selected area (default: the one
        selected in the list), also selecting it in the list."""
        a = self.current()
        if j is None:
            sel = self.range_list.curselection()
            j = sel[0] if sel else None
        if j is None or not a or not 0 <= j < len(a.ranges):
            return
        self.range_list.selection_clear(0, "end")
        self.range_list.selection_set(j)
        self.range_list.see(j)
        self.editing = j
        s, e = a.ranges[self.editing]
        self.start_text.set(fmt_time(s))
        self.end_text.set(fmt_time(e))
        self.status.set(f"Editing range {self.editing + 1}: change Start/End, then Update range "
                        f"(Enter), or Cancel (Esc).")
        self._show_editing()

    def cancel_edit(self):
        """Leave range editing, discarding the times being edited."""
        if self.editing is None:
            return
        self.editing = None
        self.start_text.set("")
        self.end_text.set("")
        self.status.set("")
        self._show_editing()

    def _show_editing(self):
        """Buttons and timeline for editing a range, or for adding one."""
        if self.editing is None:
            self.add_btn.config(text="Add range")
            self.cancel_edit_btn.pack_forget()
        else:
            self.add_btn.config(text="Update range")
            self.cancel_edit_btn.pack(side="left", padx=(4, 0), after=self.add_btn)
        self.timeline.redraw()

    def _refresh_ranges(self):
        self.range_list.delete(0, "end")
        a = self.current()
        for s, e in a.ranges if a else []:
            self.range_list.insert("end", f"{fmt_time(s)}  →  {fmt_time(e)}")
        if hasattr(self, "timeline"):
            self.timeline.redraw()

    def range_dragged(self, j: int):
        """A range was changed on the timeline: keep the list sorted and
        leave the dragged range selected."""
        a = self.current()
        moved = a.ranges[j]
        a.ranges.sort()
        self._refresh_areas()
        new = a.ranges.index(moved)
        self.range_list.selection_clear(0, "end")
        self.range_list.selection_set(new)
        self.range_list.see(new)
        s, e = moved
        self.status.set(f"Range {new + 1}: {fmt_time(s)} → {fmt_time(e)}")
        self.schedule_redraw()

    def _range_selected(self, _):
        sel = self.range_list.curselection()
        a = self.current()
        if sel and a:
            self.seek(a.ranges[sel[0]][0])

    # Render ---------------------------------------------------------------

    def render(self):
        if not self.info:
            return
        empty = [str(i + 1) for i, a in enumerate(self.areas)
                 if not a.clipped(self.info.width, self.info.height)]
        if not self.areas or empty:
            messagebox.showerror("Cannot render",
                                 f"Area {', '.join(empty)} has not been drawn: draw or delete it."
                                 if empty else "Draw an area to cover first.")
            return
        try:
            crf = int(self.crf.get())
        except ValueError:
            messagebox.showerror("Cannot render", "The quality (CRF) must be a whole number.")
            return
        suffix = self.video.suffix.lower()
        suffix = suffix if suffix in (".mp4", ".mkv", ".mov", ".webm", ".m4v") else ".mp4"
        out = filedialog.asksaveasfilename(
            title="Save covered video as", initialdir=self.video.parent,
            initialfile=f"{self.video.stem}_covered{suffix}", defaultextension=suffix,
            filetypes=OUTPUT_TYPES)
        if not out:
            return
        out = Path(out)
        if out.resolve() == self.video.resolve():
            messagebox.showerror("Cannot render", "Choose a file other than the input video.")
            return

        items = [(a, a.clipped(self.info.width, self.info.height)) for a in self.areas]
        stream_args, notes = plan_streams(self.info, out, crf)
        if notes and not messagebox.askokcancel(
                "Render", "About this output:\n\n" + "\n".join(f"• {n}" for n in notes)
                + "\n\nRender anyway?"):
            return
        cmd = build_render_cmd(self.video, out, build_filter(items, self.info), stream_args)
        self.cancelled = False
        self.progress["value"] = 0
        self.status.set("Rendering…")
        self._set_busy(True)
        threading.Thread(target=self._run_ffmpeg, args=(cmd, out, self.info.duration),
                         daemon=True).start()

    def _run_ffmpeg(self, cmd, out, duration):
        with tempfile.TemporaryFile() as err:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                         creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.isdigit():
                    self.events.put(("progress", min(100.0, int(value) / 1e6 / duration * 100)))
            rc = self.proc.wait()
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
        self.events.put(("done", rc, msg, out))

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.cancelled = True
            self.proc.terminate()

    def _set_busy(self, busy: bool):
        self.render_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")

    # Events from worker threads (Tk may only be touched from this thread) --

    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "frame":
                    self._frame_arrived(*ev[1:])
                elif ev[0] == "progress":
                    self.progress["value"] = ev[1]
                elif ev[0] == "done":
                    self._finished(*ev[1:])
        except queue.Empty:
            pass
        self.poll_after = self.root.after(POLL_MS, self._poll)

    def _frame_arrived(self, gen: int, result):
        # Only the latest request is shown, except while scrubbing, where
        # any frame newer than the one on screen beats waiting
        if gen <= self.shown_gen or (gen != self.gen and not self.scrubbing):
            return
        self.shown_gen = gen
        self.loading = False
        self.shown_time = None
        if isinstance(result, Exception):
            self.status.set(f"Frame error: {result}")
        elif result is not None:
            self.frame, self.shown_time = result
        self.render_canvas()
        if not self.scrubbing:
            self._snap()

    def _finished(self, rc, msg, out: Path):
        self._set_busy(False)
        self.proc = None
        if self.cancelled:
            out.unlink(missing_ok=True)
            self.progress["value"] = 0
            self.status.set("Cancelled.")
        elif rc == 0:
            self.progress["value"] = 100
            self.status.set(f"Saved {out}")
            messagebox.showinfo("Done", f"Saved:\n{out}")
        else:
            self.status.set("ffmpeg failed.")
            messagebox.showerror("ffmpeg failed", msg[-3000:] or f"Exit code {rc}")

    def close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Quit", "A render is running. Stop it and quit?"):
                return
            self.cancel()
        elif not self._confirm_discard():
            return
        for after_id in (self.poll_after, self.loading_after, self.repeat_after):
            if after_id:
                self.root.after_cancel(after_id)
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    if not FFMPEG or not FFPROBE:
        root.withdraw()
        messagebox.showerror("ffmpeg not found", "ffmpeg and ffprobe must be on PATH.")
        sys.exit(1)
    App(root, sys.argv[1] if len(sys.argv) > 1 else None)
    root.mainloop()


if __name__ == "__main__":
    main()
