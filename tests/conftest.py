"""Shared fixtures: small test videos built once per session with ffmpeg,
and helpers to render them and inspect the results."""

import json
import shutil
import subprocess
from pathlib import Path

import av
import pytest

import video_cover_region as vcr

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FPS = 30


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def ffprobe_json(path: Path) -> dict:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_chapters",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def render(src: Path, out: Path, areas: list, crf: int = 18) -> list[str]:
    """Render `src` to `out` exactly as the GUI does; return the notes."""
    info = vcr.probe(src)
    items = [(a, a.clipped(info.width, info.height)) for a in areas]
    args, notes = vcr.plan_streams(info, out, crf)
    cmd = vcr.build_render_cmd(src, out, vcr.build_filter(items, info), args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return notes


def sequential_frames(path: Path) -> list[tuple[float, "av.VideoFrame"]]:
    """Every frame with its time from the first frame, by plain decoding from
    the start: the reference that involves no seeking at all."""
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        t0 = (s.start_time or 0) * float(s.time_base)
        return [(f.time - t0, f.to_image()) for f in c.decode(s)]


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not on PATH")
    d = tmp_path_factory.mktemp("media")
    m = {}
    # testsrc2 draws a frame counter, so every frame differs from its
    # neighbours; a keyframe every second gives the reader real seeks to do
    m["mp4"] = d / "basic.mp4"
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}:duration=3",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", "libx264", "-g", FPS, "-c:a", "aac", "-shortest", m["mp4"])
    m["rot"] = d / "rot.mp4"
    ffmpeg("-display_rotation", 90, "-i", m["mp4"], "-c", "copy", m["rot"])
    # MPEG-TS: timestamps do not start at 0 and seeking is imprecise
    m["ts"] = d / "basic.ts"
    ffmpeg("-i", m["mp4"], "-c", "copy", m["ts"])

    srt = d / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n", encoding="utf-8")
    chapters = d / "chapters.txt"
    chapters.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=A\n"
                        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=B\n",
                        encoding="utf-8")
    m["mkv"] = d / "multi.mkv"
    ffmpeg("-i", m["mp4"], "-f", "lavfi", "-i", "sine=frequency=880:duration=3",
           "-i", srt, "-f", "ffmetadata", "-i", chapters,
           "-map", "0:v", "-map", "0:a", "-map", "1:a", "-map", "2:s", "-map_chapters", 3,
           "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "flac", "-c:s", "srt",
           "-metadata:s:a:1", "language=ita", m["mkv"])

    if "libx265" in vcr.available_encoders():
        m["hdr"] = d / "hdr10.mov"
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={FPS}:duration=2",
               "-f", "lavfi", "-i", "sine=duration=2",
               "-vf", "format=yuv420p10le,setparams=color_primaries=bt2020:"
                      "color_trc=smpte2084:colorspace=bt2020nc:range=tv",
               "-c:v", "libx265", "-x265-params", "log-level=error",
               "-c:a", "pcm_s16le", "-shortest", m["hdr"])
    return m
