"""Real videos: probing, frame reading, and renders checked frame by frame
and stream by stream. Needs ffmpeg and ffprobe on PATH."""

import struct
import subprocess

import av
import pytest
from PIL import ImageChops, ImageStat

import blurbox as bb
from conftest import FPS, ffprobe_json, render, sequential_frames
from blurbox import Area

pytestmark = pytest.mark.media


def mean_diff(a, b) -> float:
    return max(ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB"))).mean)


def need(media, key):
    if key not in media:
        pytest.skip(f"test video {key!r} needs an encoder this ffmpeg lacks")
    return media[key]


# Probe ----------------------------------------------------------------------

def test_probe_basic(media):
    info = bb.probe(media["mp4"])
    assert (info.width, info.height, info.rotation) == (320, 240, 0)
    assert info.fps == pytest.approx(FPS)
    assert info.duration == pytest.approx(3, abs=0.1)
    assert (info.codec, info.pix_fmt, info.audio, info.subtitles) == ("h264", "yuv420p", ["aac"], [])
    assert not info.full_range


def test_probe_rotated_swaps_dimensions(media):
    info = bb.probe(media["rot"])
    assert info.rotation in (90, 270)
    assert (info.width, info.height) == (240, 320)


def test_probe_streams(media):
    info = bb.probe(media["mkv"])
    assert info.audio == ["aac", "flac"] and info.subtitles == ["subrip"]


def test_probe_hdr(media):
    info = bb.probe(need(media, "hdr"))
    assert (info.codec, info.pix_fmt) == ("hevc", "yuv420p10le")
    assert info.color == {"-color_primaries": "bt2020", "-color_trc": "smpte2084",
                          "-colorspace": "bt2020nc", "-color_range": "tv"}
    assert info.audio == ["pcm_s16le"]


def test_probe_rejects_non_video(tmp_path):
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"hello")
    with pytest.raises(RuntimeError):
        bb.probe(bad)


# Frame reader ---------------------------------------------------------------

# Forward, backward, frame steps, big jumps and the very end, in one sequence,
# so the reader's cache, continue-decoding and seek paths are all exercised
SEEKS = [0, 1.0, 1 + 1 / FPS, 1 + 2 / FPS, 1 + 1 / FPS, 0.5, 2.9, 0.2, 2.0, 1.97, 99]


@pytest.mark.parametrize("key", ["mp4", "ts", "rot"])
def test_frame_reader_matches_sequential_decode(media, key):
    path = media[key]
    info = bb.probe(path)
    reader = bb.FrameReader(path, info)
    ref = sequential_frames(path)
    try:
        for t in SEEKS:
            img, ft = reader.get(t)
            # the reference frame: the first at or after t, else the last one
            rt, rimg = next(((rt, ri) for rt, ri in ref if rt >= t - 0.25 / FPS), ref[-1])
            if info.rotation:
                rimg = rimg.transpose(reader.transpose)
            assert ft == pytest.approx(rt, abs=1e-6), f"seek to {t}"
            assert ImageChops.difference(img.convert("RGB"), rimg.convert("RGB")).getbbox() is None, \
                f"seek to {t}: different picture"
    finally:
        reader.close()


def test_frame_reader_rotates_like_ffmpeg(media):
    info = bb.probe(media["rot"])
    reader = bb.FrameReader(media["rot"], info)
    img, _ = reader.get(0)
    reader.close()
    assert img.size == (info.width, info.height) == (240, 320)


# Render: pixels -------------------------------------------------------------

AREAS = [
    Area(10, 10, 120, 60, "blur", 6, [(1.0, 2.0)]),
    Area(180, 100, 100, 100, "pixelate", 10, []),
    Area(200, 20, 80, 50, "black", 20, [(0.5, 1.5)]),
    Area(60, 30, 100, 60, "black", 20, [(1.5, 1.6)]),  # overlaps area 1
]
# Frames right on every range edge, one frame either side, and in between
TIMES = [0.2, 0.5 - 1 / FPS, 0.5, 1.0, 1.5, 1.5 + 1 / FPS, 1.6, 1.6 + 1 / FPS, 2.0,
         2.0 + 1 / FPS, 2.5]


@pytest.mark.parametrize("key", ["mp4", "ts"])
def test_render_matches_live_preview(media, tmp_path, key):
    """What "Show effect" draws is what the render produces, frame by frame,
    including the first and last frame of each range and overlapping areas.
    The .ts source checks the start-time offset between preview and filter."""
    src = media[key]
    out = tmp_path / "out.mp4"
    render(src, out, AREAS, crf=0)  # lossless, so only colour conversion differs
    info = bb.probe(src)
    src_reader, out_reader = bb.FrameReader(src, info), bb.FrameReader(out, bb.probe(out))
    pad = 0.5 / info.fps
    try:
        for t in TIMES:
            raw, ft = src_reader.get(t)
            items = [(a, a.clipped(info.width, info.height)) for a in AREAS if a.active(ft, pad)]
            rendered, _ = out_reader.get(t)
            assert mean_diff(bb.apply_effects(raw, items), rendered) < 1.0, f"t={ft:.4f}"
    finally:
        src_reader.close()
        out_reader.close()


def test_black_box_is_black_and_stays_10_bit(media, tmp_path):
    src = need(media, "hdr")
    out = tmp_path / "out.mp4"
    render(src, out, [Area(40, 40, 64, 32, "black")])
    with av.open(str(out)) as c:
        frame = next(c.decode(video=0))
    assert frame.format.name == "yuv420p10le"
    y_plane = frame.planes[0]
    row = bytes(y_plane)[50 * y_plane.line_size:][:y_plane.line_size]
    lumas = struct.unpack(f"<{frame.width}H", row[:2 * frame.width])
    # 10-bit limited-range black is 64 (8-bit black would be 16); HEVC
    # compression may nudge a few values by one or two
    assert all(64 <= y <= 66 for y in lumas[45:95]) and 64 in lumas[45:95]


def raw_frame(src, filtergraph=None) -> bytes:
    """The first frame as raw yuv420p10le bytes, optionally after a graph."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(src)]
    if filtergraph:
        cmd += ["-filter_complex", filtergraph, "-map", "[v]"]
    cmd += ["-frames:v", "1", "-pix_fmt", "yuv420p10le", "-f", "rawvideo", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def test_filters_leave_other_pixels_untouched_at_full_bit_depth(media):
    """Outside the areas the graph must pass 10-bit pixels through exactly:
    any hidden trip through 8 bits (as overlay makes by default) changes them."""
    src = need(media, "hdr")
    info = bb.probe(src)
    areas = [Area(40, 120, 64, 32, m, 8) for m in ("black", "blur", "pixelate")]
    for i, a in enumerate(areas):
        a.x += 90 * i
    fg = bb.build_filter([(a, a.clipped(info.width, info.height)) for a in areas], info)
    before, after = raw_frame(src), raw_frame(src, fg)
    # Luma rows 0-99 lie above every area (they start at y=120)
    rows = 100 * info.width * 2
    assert len(after) == len(before) and after[:rows] == before[:rows]


# Render: streams ------------------------------------------------------------

def streams_by_type(path):
    data = ffprobe_json(path)
    by = {}
    for s in data["streams"]:
        by.setdefault(s["codec_type"], []).append(s)
    return by, len(data.get("chapters", []))


def test_render_hdr_keeps_codec_depth_and_colour(media, tmp_path):
    src = need(media, "hdr")
    out = tmp_path / "out.mp4"
    notes = render(src, out, [Area(10, 10, 100, 60, "blur", 8), Area(150, 50, 80, 80, "pixelate", 8)])
    by, _ = streams_by_type(out)
    v = by["video"][0]
    assert (v["codec_name"], v["pix_fmt"], v["codec_tag_string"]) == ("hevc", "yuv420p10le", "hvc1")
    assert (v["color_primaries"], v["color_transfer"], v["color_space"]) == \
        ("bt2020", "smpte2084", "bt2020nc")
    assert [a["codec_name"] for a in by["audio"]] == ["alac"]  # PCM does not fit in MP4
    assert notes == ["Audio track 1 (pcm_s16le) converted to ALAC (lossless)."]


def test_render_mkv_to_mp4_keeps_tracks_and_chapters(media, tmp_path):
    out = tmp_path / "out.mp4"
    assert render(media["mkv"], out, [Area(10, 10, 50, 50)]) == []
    by, chapters = streams_by_type(out)
    assert [a["codec_name"] for a in by["audio"]] == ["aac", "flac"]
    assert by["audio"][1]["tags"]["language"] == "ita"
    assert [s["codec_name"] for s in by["subtitle"]] == ["mov_text"]
    assert chapters == 2


def test_render_mkv_to_mkv_copies_everything(media, tmp_path):
    out = tmp_path / "out.mkv"
    render(media["mkv"], out, [Area(10, 10, 50, 50)])
    by, chapters = streams_by_type(out)
    assert [a["codec_name"] for a in by["audio"]] == ["aac", "flac"]
    assert [s["codec_name"] for s in by["subtitle"]] == ["subrip"]
    assert chapters == 2


def test_render_webm_converts_what_it_must(media, tmp_path):
    if not {"libvpx-vp9", "libopus"} <= bb.available_encoders():
        pytest.skip("needs libvpx-vp9 and libopus")
    out = tmp_path / "out.webm"
    notes = render(media["mkv"], out, [Area(10, 10, 50, 50)])
    by, _ = streams_by_type(out)
    assert by["video"][0]["codec_name"] == "vp9"
    assert [a["codec_name"] for a in by["audio"]] == ["opus", "opus"]
    assert [s["codec_name"] for s in by["subtitle"]] == ["webvtt"]
    assert len(notes) == 3  # video re-encode + two audio conversions


def test_render_rotated_video_comes_out_upright(media, tmp_path):
    out = tmp_path / "out.mp4"
    render(media["rot"], out, [Area(10, 10, 50, 50)])
    v = streams_by_type(out)[0]["video"][0]
    assert (v["width"], v["height"]) == (240, 320)
    assert not any("rotation" in s for s in v.get("side_data_list", []))
