"""Pure logic: time parsing, areas, filtergraphs, the stream plan and the
shell text of commands. No ffmpeg run is needed (the encoder list is
replaced where it matters)."""

import shlex
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

def test_parse_time():
    for text, secs in [("75.5", 75.5), ("1:05.5", 65.5), ("1:00:00", 3600.0), (" 2:03 ", 123.0)]:
        assert bb.parse_time(text) == pytest.approx(secs)
    for bad in ["", "abc", "1:2:3:4", "-5"]:
        with pytest.raises(ValueError):
            bb.parse_time(bad)


def test_fmt_time():
    assert bb.fmt_time(3661.25) == "1:01:01.250"
    assert bb.fmt_time(59.9999) == "0:01:00.000"  # rounds before splitting minutes


# Areas ----------------------------------------------------------------------

def test_area_active_respects_ranges_and_pad():
    assert Area().active(123.4, pad=0)  # no ranges: always
    a = Area(ranges=[(1.0, 2.0), (5.0, 6.0)])
    assert a.active(1.0, 0) and a.active(2.0, 0) and not a.active(3.0, 0)
    # the pad widens both ends: a frame rounded slightly outside still counts
    assert a.active(0.99, 0.02) and not a.active(2.03, 0.02)


def test_area_clipped():
    assert Area(1800, 1000, 300, 200).clipped(1920, 1080) == (1800, 1000, 120, 80)
    assert Area(-10, -10, 50, 50).clipped(1920, 1080) == (0, 0, 50, 50)
    assert Area().clipped(1920, 1080) is None  # not drawn
    assert Area(1919, 10, 50, 50).clipped(1920, 1080) is None  # 1 px left
    # whole frame: covers everything, and keeps its rectangle for later
    a = Area(10, 20, 30, 40, full=True)
    assert a.clipped(640, 480) == (0, 0, 640, 480)
    a.full = False
    assert a.clipped(640, 480) == (10, 20, 30, 40)


def test_area_json_roundtrip_and_sanitising():
    a = Area(10, 20, 300, 400, "pixelate", 12, [(1.23456, 2.5)], full=True)
    b = Area.from_json(a.to_json())
    assert (b.x, b.y, b.w, b.h, b.mode, b.strength, b.full) == (10, 20, 300, 400, "pixelate", 12, True)
    assert b.ranges == [(1.2346, 2.5)]  # stored to 4 decimals
    c = Area.from_json({"effect": "sparkles", "strength": 0, "ranges": [[9, 10], [1, 2]]})
    assert (c.mode, c.strength, c.full) == ("black", 1, False)
    assert c.ranges == [(1.0, 2.0), (9.0, 10.0)]  # sorted


def test_area_label():
    assert Area(200, 100, 400, 150, "blur", 25, [(1, 2), (3, 4)]).label(0) == \
        "1.  Blur 25,  400×150 at 200,100,  2 ranges"
    assert Area().label(2) == "3.  Black box,  not drawn yet,  always"
    assert Area(full=True, ranges=[(1, 2)]).label(0) == "1.  Black box,  whole frame,  1 range"


# Filters --------------------------------------------------------------------

def test_effect_chains():
    assert vars_black(False) == "y=minval:u=(minval+maxval)/2:v=(minval+maxval)/2"
    assert vars_black(True).startswith("y=0:")  # full-range black is 0
    assert bb.effect_chain("blur", 100, 100, 7, False) == [("gblur", "sigma=7")]
    assert bb.effect_chain("pixelate", 300, 120, 16, False) == [
        ("scale", "18:7:flags=area"), ("scale", "300:120:flags=neighbor")]
    # a block bigger than the area still leaves one block, never zero
    assert bb.effect_chain("pixelate", 10, 10, 50, False)[0] == ("scale", "1:1:flags=area")


def vars_black(full_range: bool) -> str:
    (name, args), = bb.effect_chain("black", 100, 100, 20, full_range)
    assert name == "lutyuv"
    return args


def test_build_filter_single_area():
    fg = bb.build_filter([(Area(mode="blur", strength=9), (10, 20, 30, 40))], info())
    assert fg == ("[0:v]split[b0][s0];[s0]crop=30:40:10:20,gblur=sigma=9[f0];"
                  "[b0][f0]overlay=10:20:format=auto[o0];[o0]format=yuv420p[v]")


def test_build_filter_ranges_with_offset_and_half_frame_pad():
    a = Area(mode="black", ranges=[(1.0, 2.0), (5.0, 6.5)])
    fg = bb.build_filter([(a, (0, 0, 10, 10))], info(offset=0.5, fps=25.0))
    # pad = half a frame = 0.02 s; offset shifts into ffmpeg's file time
    assert ":enable='between(t,1.4800,2.5200)+between(t,5.4800,7.0200)'" in fg


def test_build_filter_chains_areas_and_keeps_pixel_format():
    items = [(Area(mode="black"), (0, 0, 10, 10)), (Area(mode="blur"), (5, 5, 10, 10))]
    fg = bb.build_filter(items, info(pix_fmt="yuv420p10le"))
    assert "[o0]split[b1][s1]" in fg and fg.count("overlay=") == 2
    assert fg.endswith("[o1]format=yuv420p10le[v]")
    assert bb.build_filter(items, info(pix_fmt="")).endswith("null[v]")


# Stream plan ----------------------------------------------------------------

@pytest.mark.parametrize("codec, ext, encoder", [
    ("h264", ".mp4", "libx264"),
    ("hevc", ".mp4", "libx265"),
    ("av1", ".mp4", "libsvtav1"),
    ("vp9", ".mov", "libx264"),  # no VP9 in QuickTime
    ("h264", ".webm", "libvpx-vp9"),
])
def test_video_encoder_follows_source(encoders, codec, ext, encoder):
    args, _ = bb.plan_streams(info(codec=codec), Path("out" + ext), 20)
    assert value_after(args, "-c:v") == encoder and value_after(args, "-crf") == "20"


def test_video_fallback_and_notes(encoders):
    _, notes = bb.plan_streams(info(codec="h264"), Path("o.mp4"), 18)
    assert not any("re-encoded" in n for n in notes)
    encoders(ALL_ENCODERS - {"libx265"})
    args, notes = bb.plan_streams(info(codec="hevc"), Path("o.mp4"), 18)
    assert value_after(args, "-c:v") == "libx264"
    assert any("re-encoded as H264" in n and "hevc" in n for n in notes)


def test_hevc_tag_and_colour_metadata(encoders):
    color = {"-color_primaries": "bt2020", "-color_trc": "smpte2084"}
    mp4, _ = bb.plan_streams(info(codec="hevc", color=color), Path("o.mp4"), 18)
    mkv, _ = bb.plan_streams(info(codec="hevc"), Path("o.mkv"), 18)
    assert value_after(mp4, "-tag:v") == "hvc1" and "-tag:v" not in mkv
    assert value_after(mp4, "-color_trc") == "smpte2084" and "+faststart" in mp4


@pytest.mark.parametrize("audio, ext, codec", [
    ("aac", ".mp4", "copy"),
    ("pcm_s16le", ".mp4", "alac"),  # lossless where the container allows
    ("pcm_s16le", ".mov", "copy"),
    ("dts", ".mp4", "aac"),
    ("dts", ".mkv", "copy"),
    ("aac", ".webm", "libopus"),
])
def test_audio_copied_or_converted(encoders, audio, ext, codec):
    args, notes = bb.plan_streams(info(audio=[audio]), Path("o" + ext), 18)
    assert value_after(args, "-map") == "0:a:0" and value_after(args, "-c:a:0") == codec
    assert any(n.startswith("Audio") for n in notes) == (codec != "copy")


def test_every_audio_track_is_mapped(encoders):
    args, _ = bb.plan_streams(info(audio=["aac", "pcm_s24le", "ac3"]), Path("o.mp4"), 18)
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:a:0", "0:a:1", "0:a:2"]


def test_subtitles_and_attachments(encoders):
    subs = ["hdmv_pgs_subtitle", "subrip"]
    mp4, notes = bb.plan_streams(info(subtitles=subs, attachments=1), Path("o.mp4"), 18)
    # picture subtitles cannot go into MP4: the text track becomes output track 0
    assert value_after(mp4, "-map") == "0:s:1" and value_after(mp4, "-c:s:0") == "mov_text"
    assert any("Subtitle track 1" in n for n in notes) and any("attachment" in n for n in notes)
    webm, _ = bb.plan_streams(info(subtitles=["subrip"]), Path("o.webm"), 18)
    assert value_after(webm, "-c:s:0") == "webvtt"
    mkv, notes = bb.plan_streams(info(subtitles=subs, attachments=1), Path("o.mkv"), 18)
    assert mkv.count("copy") >= 2 and "0:t" in mkv and not notes


# Shell text -----------------------------------------------------------------

TRICKY = ["C:\\Program Files\\ffmpeg.exe", "-i", "my clip.mp4", "-filter_complex",
          "[0:v]split[b][s];[b][s]overlay=0:0:enable='between(t,1,2)'[v]", "-crf", "18"]


def test_shell_command_posix_round_trips():
    assert shlex.split(bb.shell_command(TRICKY, windows=False)) == TRICKY


def test_shell_command_powershell_quoting():
    text = bb.shell_command(TRICKY, windows=True)
    assert text.startswith("& 'C:\\Program Files\\ffmpeg.exe' -i 'my clip.mp4' ")
    # ; and ' would break an unquoted PowerShell line: quoted, ' doubled
    assert "'[0:v]split[b][s];[b][s]overlay=0:0:enable=''between(t,1,2)''[v]'" in text
    assert text.endswith(" -crf 18")  # plain arguments stay bare


def test_available_encoders_parses_ffmpeg_listing(monkeypatch):
    listing = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC
 A....D aac                  AAC (Advanced Audio Coding)
"""
    class Result:
        stdout = listing
    monkeypatch.setattr(bb.subprocess, "run", lambda *a, **k: Result())
    bb.available_encoders.cache_clear()
    try:
        assert bb.available_encoders() == {"libx264", "aac"}  # not the legend's "="
    finally:
        bb.available_encoders.cache_clear()
