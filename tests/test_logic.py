"""Pure logic: time parsing, areas, filtergraphs and the stream plan. No
ffmpeg run is needed (the encoder list is replaced where it matters)."""

from pathlib import Path

import pytest

import blurbox as bb
from blurbox import Area, VideoInfo

ALL_ENCODERS = frozenset({"libx264", "libx265", "libsvtav1", "libvpx-vp9", "libopus",
                          "aac", "alac", "flac", "mov_text", "webvtt"})


def info(**kw) -> VideoInfo:
    base = dict(width=1920, height=1080, duration=60.0, offset=0.0, fps=25.0, rotation=0,
                codec="h264", pix_fmt="yuv420p")
    return VideoInfo(**{**base, **kw})


def value_after(args: list[str], opt: str) -> str:
    return args[args.index(opt) + 1]


@pytest.fixture
def encoders(monkeypatch):
    """Pretend exactly these encoders exist; returns a setter."""
    def use(names=ALL_ENCODERS):
        monkeypatch.setattr(bb, "available_encoders", lambda: frozenset(names))
    use()
    return use


# Time -----------------------------------------------------------------------

@pytest.mark.parametrize("text, secs", [
    ("75.5", 75.5), ("0", 0.0), ("1:05.5", 65.5), ("1:00:00", 3600.0),
    ("0:00:12.345", 12.345), (" 2:03 ", 123.0),
])
def test_parse_time(text, secs):
    assert bb.parse_time(text) == pytest.approx(secs)


@pytest.mark.parametrize("text", ["", "   ", "abc", "1:2:3:4", "-5", "1:x"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        bb.parse_time(text)


@pytest.mark.parametrize("secs, text", [
    (0, "0:00:00.000"), (65.5, "0:01:05.500"), (3661.25, "1:01:01.250"),
    (59.9999, "0:01:00.000"), (3599.9996, "1:00:00.000"),
])
def test_fmt_time(secs, text):
    assert bb.fmt_time(secs) == text


@pytest.mark.parametrize("secs", [0, 1 / 30, 12.345, 3599.999, 7322.5])
def test_fmt_parse_roundtrip(secs):
    assert bb.parse_time(bb.fmt_time(secs)) == pytest.approx(secs, abs=5e-4)


# Areas ----------------------------------------------------------------------

def test_area_without_ranges_is_always_active():
    assert Area(ranges=[]).active(123.4, pad=0)


def test_area_active_respects_ranges_and_pad():
    a = Area(ranges=[(1.0, 2.0), (5.0, 6.0)])
    assert a.active(1.0, 0) and a.active(2.0, 0) and a.active(5.5, 0)
    assert not a.active(0.99, 0) and not a.active(3.0, 0)
    # the pad widens both ends: a frame rounded slightly outside still counts
    assert a.active(0.99, 0.02) and a.active(2.01, 0.02)
    assert not a.active(2.03, 0.02)


def test_area_clipped_to_frame():
    assert Area(100, 50, 300, 120).clipped(1920, 1080) == (100, 50, 300, 120)
    assert Area(1800, 1000, 300, 200).clipped(1920, 1080) == (1800, 1000, 120, 80)
    assert Area(-10, -10, 50, 50).clipped(1920, 1080) == (0, 0, 50, 50)


@pytest.mark.parametrize("area", [Area(), Area(10, 10, 1, 50), Area(1919, 10, 50, 50)])
def test_area_too_small_is_none(area):
    assert area.clipped(1920, 1080) is None


def test_area_json_roundtrip():
    a = Area(10, 20, 300, 400, "pixelate", 12, [(1.23456, 2.5), (7.0, 8.0)])
    b = Area.from_json(a.to_json())
    assert (b.x, b.y, b.w, b.h, b.mode, b.strength) == (10, 20, 300, 400, "pixelate", 12)
    assert b.ranges == [(1.2346, 2.5), (7.0, 8.0)]  # stored to 4 decimals


def test_area_from_json_sanitises():
    a = Area.from_json({"x": 5, "effect": "sparkles", "strength": 0,
                        "ranges": [[9, 10], [1, 2]]})
    assert a.mode == "black" and a.strength == 1
    assert a.ranges == [(1.0, 2.0), (9.0, 10.0)]  # sorted


def test_whole_frame_area_covers_the_frame_and_keeps_its_rectangle():
    a = Area(10, 20, 30, 40, full=True)
    assert a.clipped(1920, 1080) == (0, 0, 1920, 1080)
    assert Area(full=True).clipped(640, 480) == (0, 0, 640, 480)  # never drawn: still fine
    a.full = False
    assert a.clipped(1920, 1080) == (10, 20, 30, 40)


def test_whole_frame_survives_json():
    a = Area(10, 20, 30, 40, "blur", 5, [(1, 2)], full=True)
    d = a.to_json()
    assert d["full"] is True
    b = Area.from_json(d)
    assert b.full and (b.x, b.y, b.w, b.h) == (10, 20, 30, 40)
    assert Area.from_json({"x": 1}).full is False  # missing key: a rectangle


def test_whole_frame_filter():
    fg = bb.build_filter([(Area(mode="blur", strength=4, full=True), (0, 0, 1920, 1080))], info())
    assert "crop=1920:1080:0:0,gblur=sigma=4" in fg and "overlay=0:0" in fg


def test_area_label_whole_frame():
    assert Area(full=True, ranges=[(1, 2)]).label(0) == "1.  Black box,  whole frame,  1 range"


def test_area_label():
    assert Area(200, 100, 400, 150, "blur", 25, [(1, 2), (3, 4)]).label(0) == \
        "1.  Blur 25,  400×150 at 200,100,  2 ranges"
    assert Area(0, 0, 10, 10, "black", 20, [(1, 2)]).label(1) == \
        "2.  Black box,  10×10 at 0,0,  1 range"
    assert Area().label(2) == "3.  Black box,  not drawn yet,  always"


# Video info -----------------------------------------------------------------

@pytest.mark.parametrize("pix_fmt, color, full", [
    ("yuv420p", {}, False),
    ("yuvj420p", {}, True),
    ("yuv420p", {"-color_range": "pc"}, True),
    ("yuv420p10le", {"-color_range": "tv"}, False),
])
def test_full_range(pix_fmt, color, full):
    assert info(pix_fmt=pix_fmt, color=color).full_range is full


# Filters --------------------------------------------------------------------

def test_effect_chain_black_uses_range_appropriate_black():
    limited = bb.effect_chain("black", 100, 100, 20, full_range=False)
    full = bb.effect_chain("black", 100, 100, 20, full_range=True)
    assert limited == [("lutyuv", "y=minval:u=(minval+maxval)/2:v=(minval+maxval)/2")]
    assert full[0][1].startswith("y=0:")


def test_effect_chain_blur_and_pixelate():
    assert bb.effect_chain("blur", 100, 100, 7, False) == [("gblur", "sigma=7")]
    assert bb.effect_chain("pixelate", 300, 120, 16, False) == [
        ("scale", "18:7:flags=area"), ("scale", "300:120:flags=neighbor")]
    # a block bigger than the area still leaves one block, never zero
    assert bb.effect_chain("pixelate", 10, 10, 50, False)[0] == ("scale", "1:1:flags=area")


def test_build_filter_single_area_whole_video():
    fg = bb.build_filter([(Area(mode="blur", strength=9), (10, 20, 30, 40))], info())
    assert fg == ("[0:v]split[b0][s0];[s0]crop=30:40:10:20,gblur=sigma=9[f0];"
                  "[b0][f0]overlay=10:20:format=auto[o0];[o0]format=yuv420p[v]")


def test_build_filter_ranges_with_offset_and_half_frame_pad():
    a = Area(mode="black", ranges=[(1.0, 2.0), (5.0, 6.5)])
    fg = bb.build_filter([(a, (0, 0, 10, 10))], info(offset=0.5, fps=25.0))
    # pad = half a frame = 0.02 s; offset shifts into ffmpeg's file time
    assert ":enable='between(t,1.4800,2.5200)+between(t,5.4800,7.0200)'" in fg


def test_build_filter_chains_areas_in_order():
    items = [(Area(mode="black"), (0, 0, 10, 10)), (Area(mode="blur"), (5, 5, 10, 10)),
             (Area(mode="pixelate", strength=5), (20, 20, 10, 10))]
    fg = bb.build_filter(items, info())
    parts = fg.split(";")
    assert parts[0].startswith("[0:v]split[b0][s0]")
    assert "[o0]split[b1][s1]" in fg and "[o1]split[b2][s2]" in fg
    assert parts[-1] == "[o2]format=yuv420p[v]"
    assert fg.count("overlay=") == 3 and "format=auto" in fg


def test_build_filter_keeps_source_pixel_format_or_none():
    fg10 = bb.build_filter([(Area(), (0, 0, 10, 10))], info(pix_fmt="yuv420p10le"))
    assert fg10.endswith("format=yuv420p10le[v]")
    assert bb.build_filter([(Area(), (0, 0, 10, 10))], info(pix_fmt="")).endswith("null[v]")


def test_build_filter_full_range_black():
    fg = bb.build_filter([(Area(mode="black"), (0, 0, 10, 10))], info(pix_fmt="yuvj420p"))
    assert "lutyuv=y=0:" in fg


# Stream plan ----------------------------------------------------------------

@pytest.mark.parametrize("codec, ext, encoder", [
    ("h264", ".mp4", "libx264"),
    ("hevc", ".mp4", "libx265"),
    ("hevc", ".mkv", "libx265"),
    ("av1", ".mp4", "libsvtav1"),
    ("vp9", ".mkv", "libvpx-vp9"),
    ("vp9", ".mov", "libx264"),  # no VP9 in QuickTime
    ("h264", ".webm", "libvpx-vp9"),
    ("av1", ".webm", "libsvtav1"),
    ("prores", ".mov", "libx264"),
])
def test_video_encoder_follows_source(encoders, codec, ext, encoder):
    args, _ = bb.plan_streams(info(codec=codec), Path("out" + ext), 20)
    assert value_after(args, "-c:v") == encoder
    assert value_after(args, "-crf") == "20"


def test_video_reencode_is_noted(encoders):
    _, notes = bb.plan_streams(info(codec="prores"), Path("o.mov"), 18)
    assert any("re-encoded as H264" in n for n in notes)
    _, notes = bb.plan_streams(info(codec="h264"), Path("o.mp4"), 18)
    assert not any("re-encoded" in n for n in notes)


def test_hevc_falls_back_to_h264_without_x265(encoders):
    encoders(ALL_ENCODERS - {"libx265"})
    args, notes = bb.plan_streams(info(codec="hevc"), Path("o.mp4"), 18)
    assert value_after(args, "-c:v") == "libx264"
    assert any("H264" in n and "hevc" in n for n in notes)


def test_hevc_in_mp4_gets_apple_tag(encoders):
    mp4, _ = bb.plan_streams(info(codec="hevc"), Path("o.mp4"), 18)
    mkv, _ = bb.plan_streams(info(codec="hevc"), Path("o.mkv"), 18)
    assert value_after(mp4, "-tag:v") == "hvc1" and "-tag:v" not in mkv


def test_colour_metadata_is_passed_on(encoders):
    color = {"-color_primaries": "bt2020", "-color_trc": "smpte2084",
             "-colorspace": "bt2020nc", "-color_range": "tv"}
    args, _ = bb.plan_streams(info(codec="hevc", color=color), Path("o.mp4"), 18)
    for opt, val in color.items():
        assert value_after(args, opt) == val


@pytest.mark.parametrize("audio, ext, expected", [
    ("aac", ".mp4", ["copy"]),
    ("flac", ".mp4", ["copy"]),
    ("pcm_s16le", ".mp4", ["alac"]),
    ("pcm_s16le", ".mov", ["copy"]),
    ("truehd", ".mp4", ["alac"]),
    ("dts", ".mp4", ["aac", "256k"]),
    ("dts", ".mkv", ["copy"]),
    ("aac", ".webm", ["libopus", "192k"]),
    ("opus", ".webm", ["copy"]),
])
def test_audio_copied_or_converted(encoders, audio, ext, expected):
    args, notes = bb.plan_streams(info(audio=[audio]), Path("o" + ext), 18)
    assert value_after(args, "-map") == "0:a:0"
    assert value_after(args, "-c:a:0") == expected[0]
    if len(expected) > 1:
        assert value_after(args, "-b:a:0") == expected[1]
    audio_notes = [n for n in notes if n.startswith("Audio")]
    assert bool(audio_notes) == (expected[0] != "copy")


def test_every_audio_track_is_mapped(encoders):
    args, _ = bb.plan_streams(info(audio=["aac", "pcm_s24le", "ac3"]), Path("o.mp4"), 18)
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:a:0", "0:a:1", "0:a:2"]
    assert value_after(args, "-c:a:1") == "alac"


@pytest.mark.parametrize("sub, ext, codec", [
    ("subrip", ".mp4", "mov_text"), ("ass", ".mov", "mov_text"),
    ("subrip", ".webm", "webvtt"), ("hdmv_pgs_subtitle", ".mkv", "copy"),
    ("ass", ".mkv", "copy"),
])
def test_subtitles_kept(encoders, sub, ext, codec):
    args, _ = bb.plan_streams(info(subtitles=[sub]), Path("o" + ext), 18)
    assert value_after(args, "-map") == "0:s:0" and value_after(args, "-c:s:0") == codec


def test_picture_subtitles_left_out_of_mp4_with_note(encoders):
    args, notes = bb.plan_streams(info(subtitles=["hdmv_pgs_subtitle", "subrip"]),
                                   Path("o.mp4"), 18)
    # the second track becomes the first output subtitle stream
    assert value_after(args, "-map") == "0:s:1" and value_after(args, "-c:s:0") == "mov_text"
    assert any("Subtitle track 1" in n and "left out" in n for n in notes)


def test_attachments_only_kept_in_mkv(encoders):
    mkv, mkv_notes = bb.plan_streams(info(attachments=2), Path("o.mkv"), 18)
    mp4, mp4_notes = bb.plan_streams(info(attachments=2), Path("o.mp4"), 18)
    assert "0:t" in mkv and not mkv_notes
    assert "0:t" not in mp4 and any("attachment" in n for n in mp4_notes)


def test_data_streams_noted(encoders):
    _, notes = bb.plan_streams(info(data_streams=1), Path("o.mkv"), 18)
    assert any("extra stream" in n for n in notes)


def test_faststart_only_for_mp4_and_mov(encoders):
    for ext, has in [(".mp4", True), (".mov", True), (".m4v", True), (".mkv", False)]:
        args, _ = bb.plan_streams(info(), Path("o" + ext), 18)
        assert ("+faststart" in args) is has


def test_render_cmd_shape():
    cmd = bb.build_render_cmd(Path("in.mp4"), Path("out.mp4"), "[0:v]null[v]", ["-c:v", "libx264"])
    assert cmd[cmd.index("-i") + 1] == "in.mp4" and cmd[-1] == "out.mp4"
    assert value_after(cmd, "-filter_complex") == "[0:v]null[v]"
    assert value_after(cmd, "-map") == "[v]" and value_after(cmd, "-progress") == "pipe:1"


def test_available_encoders_parses_ffmpeg_listing(monkeypatch):
    listing = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC
 A....D aac                  AAC (Advanced Audio Coding)
 S..... mov_text             3GPP Timed Text subtitle
"""
    class Result:
        stdout = listing
    monkeypatch.setattr(bb.subprocess, "run", lambda *a, **k: Result())
    bb.available_encoders.cache_clear()
    try:
        assert bb.available_encoders() == {"libx264", "aac", "mov_text"}
    finally:
        bb.available_encoders.cache_clear()
