#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["av>=18", "darkdetect>=0.8", "pillow>=10", "ttkbootstrap>=2.2.3"]
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
double-click a range (in the list or on the timeline) to edit it. The
timeline shows the selected area's ranges in red and the other areas' in
grey; while a range is edited, drag its edges on the timeline to change
them (the video follows the edge) or its middle to move it. Update range
(Enter) applies the edit, Cancel (Esc) discards it.

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
import re
import shlex
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
import darkdetect
import ttkbootstrap as tb
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
THEMES = {False: "bootstrap-light", True: "bootstrap-dark"}  # by "dark theme" on/off
VIDEO_BG = "#18191c"  # around the frame, dark in either theme like a video player's
SIDEBAR_WRAP = 300  # wrap width (px) of the notes in the sidebar
SHORTCUTS = [
    ("← / →", "Back / forward 1 s"),
    ("Shift+← / Shift+→", "Back / forward 1 frame"),
    ("I / O", "Current time into Start / End"),
    ("Enter", "Add the range (Update range while editing)"),
    ("Esc", "Cancel the range edit"),
    ("E", "Show effect on/off"),
    ("Ctrl+O", "Open project"),
    ("Ctrl+S / Ctrl+Shift+S", "Save project / Save project as"),
]
# Mouse cursor per resize handle: l/r = left/right edge, t/b = top/bottom
RESIZE_CURSORS = {
    "l": "sb_h_double_arrow", "r": "sb_h_double_arrow",
    "t": "sb_v_double_arrow", "b": "sb_v_double_arrow",
    "lt": "top_left_corner", "rb": "bottom_right_corner",
    "rt": "top_right_corner", "lb": "bottom_left_corner",
}
# Window icon: docs/logo.svg as 16/32/64 px PNGs, written by tools/make_logo.py
ICON_PNGS = (
    # 16 px
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAABxElEQVR42qWTO2hTURzGf+fc"
    "e8mjaUzSSlBLHdQY0lJcWlzERSiCFhzcfAxOHZ3F0cXBSal0tYuLoOIgOEhFEBGRDqZDglWw"
    "tPhITe69uc9zXHy03KRo+01nON/vfPz/5xMAh2rHLwDXgCr/pmXgRvP9qwXxy3yPnemiUdo7"
    "ch8Y3iGgZvaLPTuzwezZjZ6uuccF5h4VAKrmdvgHz4e58zDPF1ttAW9WAnDsYI4rJ8tMjn1i"
    "veVxeTLP248uiysBkUo+sgVwarzIzUsVLMtEGWuMCM2Z8UHGShqpIp6tJAny92EwbXD93Cip"
    "TAork0YaEqRESMn+vMH5qslQepsEJ44MkDUh8gNUFKMMBUqhlUJrTTmrOVrQ/QHlAUnY9dBK"
    "IaTEyIVYxQalYuPP5bun/24hAVht+fiOQxwEIASqtY/IHyJ0PXzHxbcdbr+DxTWzd4KXHzy+"
    "f3PIZ00QAh3HRGFE5HmEXY+2E/Dma6b/EO1Ac+tFG+eHjdfu4HVs/I6Nb7t4Tpf5uoUbiQRA"
    "TExN1zf/xsqemJnRLpWcj4pj6i2DJ6s5Gk6qZ6nExNT07sq0/rm5VD5wuAnU/qNUy8DVpddP"
    "F34Cxl6wxoPFpdYAAAAASUVORK5CYII=",
    # 32 px
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAADV0lEQVR42sWXTWxUVRTHf/e9"
    "edMpM9CpiQy1FUpaoDSxtCikLkzQmJDQDXtY6AIT4qIhunBpIq6NW1ho4gZcu6kbG4mR1MU0"
    "s2j9SE35qLYh0A4U2pl3z7ks3psPtTXtlJme5OUm9913z//8z/+ed4+hzvoGR3uBceAMMMyL"
    "tWlgEvhybub2fGXS1Dl/D/gCyNJcWwGuzM3c/hrAr3P+FZCi+ZYCzr/0cs+d5Qf3p01Me74F"
    "kW/ExIgX57zVzol9jnux4HbLziQaUXvh+nxD3oYu9f57athjly2xk483iGjbjO2IgQunOjie"
    "CzieC5rPwOu9acZOdDLyagqYb10Kjna18/FYD28c3otzDhWtvvvwnRyl1acA/PR7EYCrP6wC"
    "sG5fAIBjXe1c/2CAjkwSY0wEwEprRHj0QDvXLh0ju7cNPwgwnsGpQ0xYXeMnA7xEtMWpniQA"
    "n7wZ6eHTW+HOAHx0rpt9ewKM7+MFCfyEj1hBVXHNZuDkoTQnD6Ub2nBkf/SDPRIX9z9WGgAw"
    "9to+nFOcKk4EDS1OBKcOJy3QwHBPKnJsBYnFVy9CP14n5RC1kdwrp0M1Ggc7NWbA2z6A/WmD"
    "hAKEOOfwRGoARKsAMv3fk9lk48/OxWMjDEhYOcQOp4p6XhWAU21+Cv5aLvFKp4ui9wTjeWAM"
    "xABWZt9GrUVDQcIQCUNsOUTKlbHM348t4z+3NwYgf3eNXNrgRFHfwxgvuj06InGKoiLRsQwt"
    "EtpIL1YiYFaYeWQaZ2Dit2e82xfg+VH0/wGgioqisUMJLRKGVeciwo+LbY0DKCxa8vfWONGV"
    "xHgGY0wtBc7h1KEiOKlFLTETYi0zD2G2uLX/3KZn5Nov6xRXS9hSecOnlu/4ibXwZF345s+t"
    "X65N3+DoppX1cNZw9a0EmaSpS0HMgiiqUa3QuFg9KSmfF/Zw96m/dQBDp8/m/+9eeDAjXDyy"
    "zkDW/hOA1sSoIvxaDPh2Icv9teS2uiU/190/AIxutqJY9ri1mGR22UdFSBlLm4sE9+AZ5B8l"
    "uXmvg+8WO3hs/e2WgRtm6PTZXW1M/KWFuZVcd/8ScL7FAC4XpiYmfYClhbnpXHf/nbhJSbUg"
    "8suFqYlac1oH4mZdA3mgCe35DeD9wtTEZGXyOQhi4NMF+88LAAAAAElFTkSuQmCC",
    # 64 px
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAG70lEQVR42uVbS2yUVRT+7p1O"
    "n1Mo5VFKC1JpgWBSWwlNiajFaGqACLgCdUGjrtzJwlciEI3uXBtYwMaVC3BnbUJRwiONQqlS"
    "npWCIiKUFksp05n/HBf3/o/7z9DHMJ3nSf7MvfNP/8757jnf+e69cwWmYSvWtLYB2AqgSV8V"
    "yEwbAdCrr+8H+k8fm+oPxBSO7wKwB8ByZKcNAtg30H/60IwAWLGmtQnAQT3auWC9ADoG+k/3"
    "+m8E4ji/DcDhLB71eLYYwI7KhbWXhu/8dfGxEeBxPpdt+0D/6SMxAOiw785ggksmUW6000F6"
    "bhzMA+ehfTxod6SH7ZuQP9akfXYiYA/yz/YAgNAipxv5aRulVnj5altlnuV+DBcUpAqAq+dP"
    "Gf36Z9ZnwvObCtJZ+voODCblOY3vJSxaKyTy3ArS+c+fYOSSZmLFmlbO5wiQqc75ZOV9sp6f"
    "lhR4a91cp33mxkPj3oXbkdyNgLwiweqKIF5aWY7mZaUIFUk01xZDrVDlOABbmuZhZ+sCNFQV"
    "A2AwMZgZTJTbEbC2LoS9byzDkopCAAADADNYahCEu/j0/stVTjv8YMx4zonL943+F90PjP6j"
    "aIYBUF4cwO7NtXj9ufmAEBDaUWZWADCDBYFFDnJAeXEA+99didU1ZRBSKAD0KhtDA0AMEmKq"
    "FfjsA8BwPiAhpITwRQAzA4IgAWQmAyQIQHlxAPvfaXCclzKgQIgDAEOAfPU2UBh063CB+RXW"
    "1RYa/Y/WB43+3uOR9AOwe1MNVi0phZBCjXxAQupXAwCLQACEToeciIC1y0PY0lwJCOGQnhDC"
    "BUGqsWbSzjNDCGFUgawG4LNttRCAchoeAIQdDe5mk+288JBjVgOw+dkKLKkIpvQLNi8ygWvw"
    "Ld9cGUkhADtaKsGsRI4iOHbJzqf2mMi8B87IGJg2ANVzg2hYVKQ0HruX46Dl5ryXBJ1ymO0k"
    "+GJDSI+ylre6DUFOqbNz3iiDFqlooCwHoGlpsSfUlbwlIRyRI8DxdQCRig4iZy/emnBrOUVN"
    "cU+WKZnIN4laM498HCBTA0CoUABk63qhtb1H5LCa8MSTws0f/6Kfsly/XgYAnPxwJdK9xjj9"
    "CKgpAhFBCqFG31F3DGKGIPHYyRCQsfsC0wfAyXfhjrrKe0/uC7faO9PhScgvVP8jQjN09PNN"
    "vv4Tri5PGwA1+vA5z+oioWaDELAFn/KbM5b8EogAinFesgTbao9mHgFD5zaALEuTpAWyCGxZ"
    "IMsCRUnf032LQFG7beHN7tLUAnD2z3E0Ly1RICglpHKflc4X9pzfRMABIF5OuiWSwBa7bWIw"
    "k1l1iJz3+odjmT9RTpk2AKOPFPLScVw64e+Qn3/Cox04/kGd6QipskhkOSXSLZfsgmC/71Ob"
    "YxGZ+hQ4dzOM558qArEn99l1fNII8I+kDwg7BWwQ3EjwAaEXWC+MyNTvC5wYDOs81floWTon"
    "vW396m3H5K/+vJPzsZFAHhAoDhC/DBWkPgJuPyBcvRNB/fwCPaICQkgIYjXwdvibZUBHgK0M"
    "SYkpIpBHJXojwXQ8NmKuj0rcfSTTszN0+PdxZwTZw8hTR4Dl+xvF9mxZigssDxBWbCp4o+CH"
    "m8H0bY11DUzg1n8RM6xjHI6qyw+IN10sHxDkAcmTAv5UuDMOHP+3ML17g1+fHPc5Mf0IMD9P"
    "Hk7wVAbDecsAYf+VkvQvif1220LX1Qm88nRQsz+0Aoy/3KF0ACtKsCsBk7Fl5md8v/NkEX7+"
    "J4gL9wsyY1n8wJkI6uYI1M2TCgB7FujHgD2zQoaxgAK7vmvHiczy54y8Rbg+KvDtYPJHP+Ht"
    "8bEI45NjYfxxL2rmftSf8y4XcDwy9PAC25LYMtNgcFTgy/MhPIyKWQNgJDEQgE9/imBgaJJ8"
    "j0eEfj1gmX0vUNdn2XkAIwVlobm9ANoSfcJXZxlvNzzCC9URxMsBe1boagE2ucAnke32yXul"
    "+O7vSohiibLZmwz2Bqpq6psAtCb6hAgJ/Ho3iAvDAayeE0FJgBzRYxCdl/A864Sm9GUMhSW+"
    "ubYQR+/OQXT2t5SPiMaW9jYk8cfSG6rCeLV6HMvKosa6gB0BxkqyhwxvjAVx9E4Ip4ZDSKFt"
    "FADQ2NJ+LdlnhBYUWWiuDGNV+QRKAoRV5WGjElwaLcTDqMDl0SL03i/B0ETKf6812NfTWWf/"
    "133wnKJIht0NB9B1qxRdt0qRobbPODPU2NJ+No9+Od7b19PZ7NcBHYmWRGTfoamOGCHU19PZ"
    "672Rw9ahfQXgOzh5++bAxaqa+nMAXgNQnIMjv7Ovp/MIJjs5qkHo1NpgcQ4dnd3e19N5DDM5"
    "PN3Y0r4LOXB4uq+n8xASOT3uAaINWXh8Pt6I++1/MrJ1mwLYVQkAAAAASUVORK5CYII=",
)
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


@functools.cache
def ffmpeg_version() -> str:
    """ffmpeg's version line, e.g. "ffmpeg version 9.0.2-full_build-..."."""
    res = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    # The line goes on with " Copyright (c) 2000-... the FFmpeg developers"
    return (res.stdout.splitlines() or ["unknown"])[0].split(" Copyright")[0].strip()


# Characters that need no quoting in either shell
_PLAIN_ARG = re.compile(r"[A-Za-z0-9_\-.:/\\=+,@%]+")


def shell_command(cmd: list[str], windows: bool = sys.platform == "win32") -> str:
    """`cmd` as text to paste into a terminal: PowerShell syntax on Windows
    (filtergraphs hold ; and ' that PowerShell would otherwise act on),
    POSIX shell syntax elsewhere."""
    if not windows:
        return shlex.join(cmd)

    def quote(arg: str) -> str:
        # Single quotes are literal in PowerShell; a ' inside is doubled
        return arg if _PLAIN_ARG.fullmatch(arg) else "'" + arg.replace("'", "''") + "'"

    # The call operator & runs a program named by a (quoted) path
    return "& " + " ".join(quote(a) for a in cmd)


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
    # Metadata and chapters are copied by default; -map_metadata is explicit.
    # -fps_mode passthrough keeps every frame at its source time: without it
    # ffmpeg 6.1 (not 9.0) repeats the first frame when the audio starts before
    # the video (common in MPEG-TS), so the whole video lands a frame late
    return [FFMPEG, "-y", "-v", "error", "-i", str(src),
            "-filter_complex", filtergraph, "-map", "[v]", "-fps_mode", "passthrough",
            *stream_args,
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
    the other areas' as a thin grey strip. Click or drag to seek. Double-
    click a red range to edit it; while it is being edited, drag its edges
    or middle to change the Start/End of the edit (Update range applies
    them, Cancel discards them)."""

    HEIGHT, MARGIN = 34, 8
    BAND_TOP, BAND_BOTTOM = 15, 26  # the selected area's ranges
    EDGE_PX = 5  # how close to a range edge (screen pixels) grabs the edge
    DRAG_PX = 3  # movement that turns a click on a range into a drag

    def __init__(self, master, app: "App"):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         background=app.style.colors.bg)
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

    def _range_at(self, x: float, y: float, any_range: bool = False) -> tuple[int, str] | None:
        """The selected area's range under a point, and which part: its
        "start" or "end" edge, or its middle ("move"). Only the range being
        edited counts, unless `any_range`. Edges win, so a short range can
        still be stretched."""
        app = self.app
        cur = app.current() if app.info else None
        if not cur or not cur.ranges or not self.BAND_TOP - 3 <= y <= self.BAND_BOTTOM + 3:
            return None
        if any_range:
            shown = list(enumerate(cur.ranges))
        elif app.editing is not None:
            shown = [(app.editing, app.pending_range())]
        else:
            return None
        edges = []
        for j, (s, e) in shown:
            xs, xe = self.x_of(s), max(self.x_of(e), self.x_of(s) + 2)
            edges += [(abs(x - xs), j, "start"), (abs(x - xe), j, "end")]
        dist, j, part = min(edges)
        if dist <= self.EDGE_PX:
            return j, part
        return next(((j, "move") for j, (s, e) in shown if self.x_of(s) < x < self.x_of(e)), None)

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
            self.grab = (j, part, e.x, self.app.pending_range())
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
        # The drag edits Start/End, like typing them: Update range applies
        self.app.set_pending_range(s, en)
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
        s, en = self.app.pending_range()
        self.app.status.set(f"Range {grab[0] + 1} → {fmt_time(s)} – {fmt_time(en)}: "
                            f"Update range (Enter) applies it, Cancel (Esc) discards it.")
        self.app._scrub_end()

    def _double(self, e):
        """Double-click on a range: edit it, as a double-click in the list.
        (Tk sends this instead of the second press, so the first click has
        already seeked, as any click does.)"""
        hit = self._range_at(e.x, e.y, any_range=True)
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
        c = app.style.colors  # the active theme's, so a theme switch just redraws
        a, b = self._span()
        self.create_rectangle(a, 9, b, 27, fill=c.inputbg, outline=c.border)
        for i, area in enumerate(app.areas):
            if i != app.cur:
                for s, e in area.ranges or [(0, app.info.duration)]:
                    self.create_rectangle(self.x_of(s), 10, self.x_of(e), 14,
                                          fill=c.secondary, width=0)
        cur = app.current()
        if cur:
            always = c.make_transparent(0.35, c.danger, c.inputbg)
            for j, (s, e) in enumerate(cur.ranges or [(0, app.info.duration)]):
                editing = cur.ranges and j == app.editing
                if editing:  # show the edit in progress, not the saved range
                    s, e = app.pending_range()
                self.create_rectangle(self.x_of(s), 15, max(self.x_of(e), self.x_of(s) + 2), 26,
                                      fill=always if not cur.ranges else c.danger,
                                      outline=c.primary if editing else "",
                                      width=2 if editing else 0)
        self.draw_playhead()

    def draw_playhead(self):
        self.delete("ph")
        if not self.app.info:
            return
        fg = self.app.style.colors.fg
        x = self.x_of(self.app.pos.get())
        self.create_line(x, 4, x, 31, fill=fg, width=2, tags="ph")
        self.create_polygon(x - 5, 2, x + 5, 2, x, 8, fill=fg, tags="ph")


class FfmpegWindow(tk.Toplevel):
    """What blurbox runs: the ffmpeg in use and its encoders, the decoder
    behind the preview, and the exact command Render would run for the
    current areas, ready to copy."""

    ENCODERS = [
        ("libx264", "H.264 video (also the fallback)"), ("libx265", "HEVC video"),
        ("libsvtav1", "AV1 video"), ("libvpx-vp9", "VP9 video"),
        ("aac", "AAC audio"), ("alac", "ALAC audio, lossless"), ("libopus", "Opus audio (WebM)"),
        ("mov_text", "MP4/MOV subtitles"), ("webvtt", "WebM subtitles"),
    ]
    CONTAINERS = [".mp4", ".mkv", ".mov", ".webm"]

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("ffmpeg")
        self.geometry("960x680")
        self.transient(app.root)
        self.command = ""  # the render command as shell text, for Copy

        bar = tb.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        tb.Label(bar, text="Output container").pack(side="left")
        default = app.default_output().suffix if app.video else ".mp4"
        self.suffix = tk.StringVar(value=".mp4" if default == ".m4v" else default)
        box = tb.Combobox(bar, textvariable=self.suffix, values=self.CONTAINERS, width=7,
                          state="readonly")
        box.pack(side="left", padx=(4, 12))
        box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        tb.Button(bar, text="Refresh", icon="arrow-clockwise",
                  command=self.refresh).pack(side="left")
        self.copy_btn = tb.Button(bar, text="Copy command", icon="copy", bootstyle="primary",
                                  command=self.copy)
        self.copy_btn.pack(side="left", padx=4)
        self.note = tb.Label(bar, text="", bootstyle="success")
        self.note.pack(side="left", padx=8)
        tb.Button(bar, text="Close", command=self.destroy).pack(side="right")

        body = tb.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.text = tb.Text(body, wrap="word", font="TkFixedFont", padx=10, pady=8,
                            relief="flat", highlightthickness=0)
        scroll = tb.Scrollbar(body, command=self.text.yview)
        self.text.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_config("head", font=("TkDefaultFont", 10, "bold"),
                             foreground=app.style.colors.primary)
        self.bind("<Escape>", lambda e: self.destroy())
        self.refresh()

    def refresh(self):
        app, info = self.app, self.app.info
        parts: list[tuple[str, str]] = []  # (text, tag)

        def head(title):
            parts.append((title + "\n", "head"))

        def line(s=""):
            parts.append((s + "\n", ""))

        head("ffmpeg (used to render)")
        line(f"  program   {FFMPEG}")
        line(f"  ffprobe   {FFPROBE}")
        line(f"  version   {ffmpeg_version()}")
        line()
        head("Encoders blurbox can use")
        enc = available_encoders()
        for name, what in self.ENCODERS:
            line(f"  {'yes' if name in enc else 'NO '}  {name:<11} {what}")
        line()
        head("Preview decoding")
        line(f"  PyAV {av.__version__} with its own built-in FFmpeg "
             f"{getattr(av, 'ffmpeg_version_info', '?')}: decodes the frames you see and runs")
        line("  the preview's blur/pixelate filters. Renders always use the ffmpeg above.")
        line()
        if info:
            head("Source")
            line(f"  file      {app.video}")
            line(f"  video     {info.codec}, {info.pix_fmt}, {info.width}×{info.height}, "
                 f"{info.fps:.3f} fps, {fmt_time(info.duration)}")
            if info.rotation:
                line(f"  rotation  {info.rotation}° (applied, so frames are {info.width}×{info.height})")
            if info.color:
                line("  colour    " + ", ".join(f"{k.lstrip('-')}={v}" for k, v in info.color.items()))
            line(f"  audio     {', '.join(info.audio) or 'none'}")
            line(f"  subtitles {', '.join(info.subtitles) or 'none'}")
            line()

        head("Render command")
        self.command = ""
        try:
            if not app.video:
                raise ValueError("Open a video first.")
            out = app.default_output(self.suffix.get())
            cmd, notes = app.render_command(out)
            self.command = shell_command(cmd)
            line(f"  Exactly what Render runs for the current areas, writing to the default file")
            line(f"  (Render asks where to save; the command then uses the file you choose):")
            line(f"  {out}")
            line("  -progress pipe:1 -nostats feed the progress bar; without them ffmpeg prints")
            line("  its usual status line instead.")
            for n in notes:
                line(f"  • {n}")
            line()
            shell = "PowerShell" if sys.platform == "win32" else "shell"
            head(f"As {shell} text (Copy command copies this)")
            line(self.command)
        except ValueError as e:
            line(f"  Not ready: {e}")

        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for s, tag in parts:
            self.text.insert("end", s, tag or ())
        self.text.config(state="disabled")
        self.copy_btn.config(state="normal" if self.command else "disabled")
        self.note.config(text="")

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.command)
        self.note.config(text="Copied.")


class App:
    def __init__(self, root: tk.Tk, path: str | None = None):
        self.root = root
        root.title("Blurbox")
        # Kept on self: Tk drops an image once Python frees it
        self.icons = [tk.PhotoImage(data=png, format="png") for png in ICON_PNGS]
        root.iconphoto(True, *self.icons)
        root.geometry("1280x860")
        root.minsize(960, 640)
        # Light or dark as the system is (darkdetect: None where it cannot tell)
        self.dark = tk.BooleanVar(value=bool(darkdetect.isDark()))
        self.style = tb.Style(THEMES[self.dark.get()])

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
        self.dark.trace_add("write", lambda *_: self._set_theme())
        for v in (*self.rect_vars.values(), self.mode, self.strength, self.full):
            v.trace_add("write", lambda *_: self._area_edited())
        self.show_effect.trace_add("write", lambda *_: self.schedule_redraw())
        self.pos.trace_add("write", lambda *_: self.timeline.draw_playhead())
        # While a range is edited, the timeline shows Start/End as they change
        for v in (self.start_text, self.end_text):
            v.trace_add("write", lambda *_: self.editing is not None and self.timeline.redraw())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_after = root.after(POLL_MS, self._poll)
        if path and path.lower().endswith(".json"):
            self.open_project(path)
        elif path:
            self.open_video(path)

    # UI layout ------------------------------------------------------------

    def _build(self):
        r = self.root
        self.font_head = tb.nametofont("TkDefaultFont").copy()
        self.font_head.configure(weight="bold")
        self._build_menu()

        # Toolbar: files on the left, output on the right
        top = tb.Frame(r, padding=(8, 6))
        top.pack(fill="x")
        for text, icon, command, tip in [
                ("Open video", "film", self.choose_video, "Open a video to cover"),
                ("Open project", "folder2-open", self.open_project, "Open a project (Ctrl+O)"),
                ("Save", "save", self.save_project, "Save the project (Ctrl+S)")]:
            b = tb.Button(top, text=text, icon=icon, bootstyle="ghost", command=command)
            b.pack(side="left", padx=(0, 2))
            tb.ToolTip(b, text=tip)
        tb.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8, pady=4)
        tb.Checkbutton(top, text="Show effect (E)", variable=self.show_effect,
                       bootstyle="round-toggle").pack(side="left", padx=4)

        self.render_btn = tb.Button(top, text="Render…", icon="box-arrow-up-right",
                                    bootstyle="primary", command=self.render)
        self.render_btn.pack(side="right")
        tb.ToolTip(self.render_btn, text="Write the covered video with ffmpeg")
        crf = tb.Spinbox(top, from_=0, to=63, width=4, textvariable=self.crf)
        crf.pack(side="right", padx=(4, 10))
        tb.ToolTip(crf, text="CRF: lower = better quality and a bigger file")
        tb.Label(top, text="Quality").pack(side="right")
        tb.Separator(top, orient="vertical").pack(side="right", fill="y", padx=8, pady=4)
        for icon, command, tip in [("terminal", self.show_ffmpeg, "ffmpeg and the render command"),
                                   ("moon-stars", self._toggle_theme, "Dark theme on/off")]:
            b = tb.Button(top, icon=icon, icon_only=True, bootstyle="ghost", command=command)
            b.pack(side="right", padx=(2, 0))
            tb.ToolTip(b, text=tip)
        tb.Separator(r).pack(fill="x")

        # Status bar, packed before the body so a small window shrinks the body
        status = tb.Frame(r, padding=(10, 4))
        status.pack(side="bottom", fill="x")
        tb.Label(status, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.file_label = tb.Label(status, text="", bootstyle="secondary")
        self.file_label.pack(side="right")
        # Shown only while rendering (see _set_busy)
        self.cancel_btn = tb.Button(status, text="Cancel", icon="x-lg", bootstyle="danger-ghost",
                                    command=self.cancel, state="disabled")
        self.progress = tb.Progressbar(status, maximum=100, length=180, bootstyle="striped")
        tb.Separator(r).pack(side="bottom", fill="x")

        body = tb.Panedwindow(r, orient="horizontal")
        body.pack(fill="both", expand=True)

        # Left: the frame, and the seek bar under it
        player = tb.Frame(body)
        body.add(player, weight=1)
        self.canvas = tk.Canvas(player, background=VIDEO_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.render_canvas())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.canvas.bind("<Motion>", self._hover)

        seek = tb.Frame(player, padding=(6, 6))
        seek.pack(fill="x")
        for icon, step, frame, tip in [
                ("chevron-double-left", -1, False, "Back 1 s (←), hold to repeat"),
                ("chevron-left", -1, True, "Back 1 frame (Shift+←), hold to repeat"),
                ("chevron-right", 1, True, "Forward 1 frame (Shift+→), hold to repeat"),
                ("chevron-double-right", 1, False, "Forward 1 s (→), hold to repeat")]:
            self._repeat_button(seek, icon, tip, lambda s=step, f=frame: self.step(s, f))
        # The time first, so a narrow window shrinks the timeline, not it
        tb.Label(seek, textvariable=self.time_text).pack(side="right")
        self.timeline = Timeline(seek, self)
        self.timeline.pack(side="left", fill="x", expand=True, padx=(8, 8))

        # Right: the areas and the selected one's settings, scrolling when the
        # window is too short for them (sized to them at the end of _build)
        side = tb.ScrolledFrame(body, padding=(14, 10, 14, 10), auto_hide=True)
        body.add(side.container, weight=0)

        sec = self._section(side, "Areas")
        self.area_list = tb.Listbox(sec, height=6, exportselection=False, activestyle="none")
        self.area_list.pack(fill="x")
        self.area_list.bind("<<ListboxSelect>>", self._area_selected)
        ab = tb.Frame(sec)
        ab.pack(fill="x", pady=(6, 0))
        tb.Button(ab, text="New area", icon="plus-lg", command=self.new_area).pack(side="left")
        tb.Button(ab, text="Duplicate", icon="files",
                  command=self.duplicate_area).pack(side="left", padx=4)
        tb.Button(ab, text="Delete", icon="trash3", bootstyle="danger-outline",
                  command=self.delete_area).pack(side="right")

        sec = self._section(side, "Selected area")
        sec.columnconfigure(1, weight=1)
        pad = {"pady": 3}
        tb.Label(sec, text="Position (px)").grid(row=0, column=0, sticky="nw", padx=(0, 10), **pad)
        pos = tb.Frame(sec)
        pos.grid(row=0, column=1, sticky="w", **pad)
        self.rect_boxes = []
        for i, k in enumerate("xywh"):
            tb.Label(pos, text=k.upper()).grid(row=i // 2, column=(i % 2) * 2, sticky="e",
                                               padx=(0 if i % 2 == 0 else 12, 4), pady=2)
            box = tb.Spinbox(pos, from_=0, to=99999, width=6, textvariable=self.rect_vars[k])
            box.grid(row=i // 2, column=(i % 2) * 2 + 1, pady=2)
            self.rect_boxes.append(box)
        tb.Checkbutton(sec, text="Whole frame", variable=self.full,
                       bootstyle="round-toggle").grid(row=1, column=1, sticky="w", **pad)

        tb.Label(sec, text="Effect").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(10, 3))
        eff = tb.Frame(sec)
        eff.grid(row=2, column=1, sticky="w", pady=(10, 3))
        icons = {"black": "square-fill", "blur": "droplet-half", "pixelate": "grid-3x3-gap-fill"}
        for value, label in MODES.items():
            tb.Radiobutton(eff, text=label, value=value, variable=self.mode, icon=icons[value],
                           bootstyle="primary-outline-toolbutton").pack(side="left")
        self.strength_label = tb.Label(sec, text="")
        self.strength_label.grid(row=3, column=0, sticky="w", padx=(0, 10), **pad)
        self.strength_box = tb.Spinbox(sec, from_=1, to=200, width=6, textvariable=self.strength)
        self.strength_box.grid(row=3, column=1, sticky="w", **pad)

        sec = self._section(side, "Time ranges")
        sec.columnconfigure(1, weight=1)
        for i, (label, var, key) in enumerate([("Start", self.start_text, "I"),
                                               ("End", self.end_text, "O")]):
            tb.Label(sec, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=2)
            entry = tb.Entry(sec, textvariable=var, width=12)
            entry.grid(row=i, column=1, sticky="ew", pady=2)
            entry.bind("<Return>", lambda e: self.add_range())
            b = tb.Button(sec, text=f"Current ({key})", icon="geo-alt", bootstyle="ghost",
                          command=lambda v=var: self.mark(v))
            b.grid(row=i, column=2, sticky="ew", padx=(4, 0), pady=2)
            tb.ToolTip(b, text=f"Put the current time in {label} ({key})")
        row = tb.Frame(sec)
        row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        self.add_btn = tb.Button(row, text="Add range", icon="plus-lg", bootstyle="primary",
                                 command=self.add_range)
        self.add_btn.pack(side="left")
        # Shown only while a range is being edited (see _show_editing)
        self.cancel_edit_btn = tb.Button(row, text="Cancel", command=self.cancel_edit)
        tb.Button(row, text="Remove", icon="trash3", bootstyle="danger-outline",
                  command=self.remove_range).pack(side="right")
        self.range_list = tb.Listbox(sec, height=4, exportselection=False, activestyle="none")
        self.range_list.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.range_list.bind("<<ListboxSelect>>", self._range_selected)
        self.range_list.bind("<Double-Button-1>", lambda e: self.edit_range())
        tb.Label(sec, text="No ranges = always (start to end). Double-click a range, here or on "
                           "the timeline, to edit it. Times: seconds, m:ss or h:mm:ss.fff",
                 bootstyle="secondary", wraplength=SIDEBAR_WRAP, justify="left",
                 ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        side.update_idletasks()
        side.container.config(width=side.winfo_reqwidth() + side._vbar.winfo_reqwidth())

        r.bind("<Control-s>", lambda e: self.save_project())
        r.bind("<Control-S>", lambda e: self.save_project(ask=True))
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
        self._theme_colors()

    def _build_menu(self):
        menu = tb.Menu(self.root)
        file = tb.Menu(menu, tearoff=False)
        file.add_command(label="Open video…", command=self.choose_video)
        file.add_command(label="Open project…", accelerator="Ctrl+O", command=self.open_project)
        file.add_command(label="Save project", accelerator="Ctrl+S", command=self.save_project)
        file.add_command(label="Save project as…", accelerator="Ctrl+Shift+S",
                         command=lambda: self.save_project(ask=True))
        file.add_separator()
        file.add_command(label="Render…", command=self.render)
        file.add_separator()
        file.add_command(label="Quit", command=self.close)
        menu.add_cascade(label="File", menu=file)
        area = tb.Menu(menu, tearoff=False)
        area.add_command(label="New area", command=self.new_area)
        area.add_command(label="Duplicate", command=self.duplicate_area)
        area.add_command(label="Delete", command=self.delete_area)
        menu.add_cascade(label="Area", menu=area)
        view = tb.Menu(menu, tearoff=False)
        view.add_checkbutton(label="Show effect", accelerator="E", variable=self.show_effect)
        view.add_checkbutton(label="Dark theme", variable=self.dark)
        menu.add_cascade(label="View", menu=view)
        tools = tb.Menu(menu, tearoff=False)
        tools.add_command(label="ffmpeg…", command=self.show_ffmpeg)
        menu.add_cascade(label="Tools", menu=tools)
        helpm = tb.Menu(menu, tearoff=False)
        helpm.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        menu.add_cascade(label="Help", menu=helpm)
        self.root.config(menu=menu)

    def _section(self, parent, title: str) -> tb.Frame:
        """A titled group of the sidebar; widgets go into the frame returned."""
        tb.Label(parent, text=title, font=self.font_head).pack(anchor="w")
        tb.Separator(parent).pack(fill="x", pady=(3, 8))
        frame = tb.Frame(parent)
        frame.pack(fill="x", pady=(0, 16))
        return frame

    def _toggle_theme(self):
        self.dark.set(not self.dark.get())

    def _set_theme(self):
        self.style.theme_use(THEMES[self.dark.get()])
        self._theme_colors()

    def _theme_colors(self):
        """Colours the theme does not set, or would set otherwise: a theme
        switch repaints the plain Tk widgets, so this runs after each."""
        c = self.style.colors
        self.canvas.config(background=VIDEO_BG)
        self.timeline.config(background=c.bg)
        for lb in (self.area_list, self.range_list):
            lb.config(selectbackground=c.primary, selectforeground=c.selectfg)
        self.timeline.redraw()

    def show_shortcuts(self):
        width = max(len(k) for k, _ in SHORTCUTS)
        messagebox.showinfo("Keyboard shortcuts",
                            "\n".join(f"{k:<{width}}   {what}" for k, what in SHORTCUTS))

    def _repeat_button(self, parent, icon: str, tip: str, action):
        """A button that acts on press and, held down, repeats after a delay.
        Each repeat waits for the previous frame to be on screen."""
        b = tb.Button(parent, icon=icon, icon_only=True, bootstyle="ghost")
        b.pack(side="left")
        tb.ToolTip(b, text=tip)

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

    def pending_range(self) -> tuple[float, float] | None:
        """The range being edited as Start/End now say (typed or dragged on
        the timeline), or its saved value while they do not parse."""
        if self.editing is None:
            return None
        try:
            s, e = parse_time(self.start_text.get()), parse_time(self.end_text.get())
            if s < e:
                return s, min(e, self.info.duration)
        except ValueError:
            pass
        return self.current().ranges[self.editing]

    def set_pending_range(self, s: float, e: float):
        self.start_text.set(fmt_time(s))
        self.end_text.set(fmt_time(e))

    def _range_selected(self, _):
        sel = self.range_list.curselection()
        a = self.current()
        if sel and a:
            self.seek(a.ranges[sel[0]][0])

    # Render ---------------------------------------------------------------

    def show_ffmpeg(self):
        """Open the ffmpeg window, or bring it forward up to date."""
        win = getattr(self, "ffmpeg_window", None)
        if win is not None and win.winfo_exists():
            win.refresh()
            win.lift()
        else:
            self.ffmpeg_window = FfmpegWindow(self)

    def check_renderable(self):
        """Raise ValueError, with a message for the user, if the current
        areas and settings cannot be rendered."""
        if not self.info:
            raise ValueError("Open a video first.")
        empty = [str(i + 1) for i, a in enumerate(self.areas)
                 if not a.clipped(self.info.width, self.info.height)]
        if empty:
            raise ValueError(f"Area {', '.join(empty)} has not been drawn: draw or delete it.")
        if not self.areas:
            raise ValueError("Draw an area to cover first.")
        try:
            int(self.crf.get())
        except ValueError:
            raise ValueError("The quality (CRF) must be a whole number.") from None

    def default_output(self, suffix: str | None = None) -> Path:
        """Where the render goes by default: next to the video, as
        <name>_covered, in the same container when blurbox can write it."""
        if suffix is None:
            suffix = self.video.suffix.lower()
            suffix = suffix if suffix in (".mp4", ".mkv", ".mov", ".webm", ".m4v") else ".mp4"
        return self.video.parent / f"{self.video.stem}_covered{suffix}"

    def render_command(self, out: Path) -> tuple[list[str], list[str]]:
        """The ffmpeg command Render runs to write `out`, and the notes on
        what gets converted or left out. Raises ValueError like
        check_renderable. The ffmpeg window shows exactly this."""
        self.check_renderable()
        items = [(a, a.clipped(self.info.width, self.info.height)) for a in self.areas]
        stream_args, notes = plan_streams(self.info, out, int(self.crf.get()))
        return build_render_cmd(self.video, out, build_filter(items, self.info), stream_args), notes

    def render(self):
        try:
            self.check_renderable()
        except ValueError as e:
            messagebox.showerror("Cannot render", str(e))
            return
        default = self.default_output()
        out = filedialog.asksaveasfilename(
            title="Save covered video as", initialdir=self.video.parent,
            initialfile=default.name, defaultextension=default.suffix, filetypes=OUTPUT_TYPES)
        if not out:
            return
        out = Path(out)
        if out.resolve() == self.video.resolve():
            messagebox.showerror("Cannot render", "Choose a file other than the input video.")
            return

        cmd, notes = self.render_command(out)
        if notes and not messagebox.askokcancel(
                "Render", "About this output:\n\n" + "\n".join(f"• {n}" for n in notes)
                + "\n\nRender anyway?"):
            return
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
        # Progress and Cancel sit in the status bar only while rendering
        if busy:
            self.progress.pack(side="left", padx=(8, 4), before=self.file_label)
            self.cancel_btn.pack(side="left", padx=(0, 12), before=self.file_label)
        else:
            self.progress.pack_forget()
            self.cancel_btn.pack_forget()

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
