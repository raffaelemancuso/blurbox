"""The GUI, driven through its methods and real Tk events: one test per
feature, walking through its behaviour. Needs a display (skipped without
one) and ffmpeg for the test video."""

import json
import shutil
import time
import tkinter as tk

import pytest

import blurbox as bb
from conftest import FPS

pytestmark = [pytest.mark.gui, pytest.mark.media]

TOL = 1.5 / FPS  # a seek or drag lands on a whole frame near its target


@pytest.fixture
def dialogs(monkeypatch):
    """Replace every dialog; tests set the answers and read what was shown."""
    state = {"errors": [], "save_as": None, "open": None, "discard": False}
    monkeypatch.setattr(bb.messagebox, "showerror", lambda title, msg: state["errors"].append(msg))
    monkeypatch.setattr(bb.messagebox, "showwarning", lambda *a: None)
    monkeypatch.setattr(bb.messagebox, "showinfo", lambda *a: None)
    monkeypatch.setattr(bb.messagebox, "askyesnocancel", lambda *a: state["discard"])
    monkeypatch.setattr(bb.filedialog, "asksaveasfilename", lambda **k: state["save_as"])
    monkeypatch.setattr(bb.filedialog, "askopenfilename", lambda **k: state["open"])
    return state


@pytest.fixture(scope="session")
def tk_root():
    # One Tk interpreter for the whole run: creating many in one process
    # fails now and then on Windows ("Can't find a usable tk.tcl")
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display for Tk: {e}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(media, dialogs, tk_root):
    window = tk.Toplevel(tk_root)  # the app treats it as its root window
    window.geometry("1100x850")
    a = bb.App(window, str(media["mp4"]))
    wait_frame(a)
    yield a
    a.saved = a._snapshot()  # nothing to save on close
    a.close()


def pump(app, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.root.update()
        time.sleep(0.002)


def wait_frame(app, limit=10):
    end = time.time() + limit
    while app.shown_gen != app.gen and time.time() < end:
        app.root.update()
        time.sleep(0.002)
    pump(app)
    assert app.shown_gen == app.gen, "frame never arrived"


def key(app, keysym):
    app.canvas.focus_force()
    pump(app)
    app.canvas.event_generate(f"<{keysym}>")
    pump(app)


# Frame canvas helpers (points in video pixels)

def to_canvas(app, x, y):
    return int(app.offx + x * app.scale), int(app.offy + y * app.scale)


def drag(app, start, end):
    (a, b), (c, d) = to_canvas(app, *start), to_canvas(app, *end)
    app.canvas.event_generate("<ButtonPress-1>", x=a, y=b)
    app.canvas.event_generate("<B1-Motion>", x=c, y=d)
    app.canvas.event_generate("<ButtonRelease-1>", x=c, y=d)
    pump(app)


def hover(widget, x, y):
    widget.event_generate("<Motion>", x=x, y=y)
    widget.update()
    return widget.cget("cursor")


def rect(app, i=None):
    a = app.areas[app.cur if i is None else i]
    return a.x, a.y, a.w, a.h


# Timeline helpers (points in seconds)

BAND_Y = 20  # inside the row of the selected area's ranges
SAVED = [(1.0, 2.0), (2.5, 2.8)]


@pytest.fixture
def ranged(app):
    """One area with the ranges in SAVED (the test video lasts 3 s)."""
    app.new_area()
    app.set_rect(10, 10, 50, 50)
    for s, e in SAVED:
        app.start_text.set(str(s))
        app.end_text.set(str(e))
        app.add_range()
    pump(app)
    return app


def tl_drag(app, t0, t1, y=BAND_Y):
    tl = app.timeline
    x0, x1 = int(round(tl.x_of(t0))), int(round(tl.x_of(t1)))
    tl.event_generate("<ButtonPress-1>", x=x0, y=y)
    for i in range(1, 6):  # in steps, so it counts as a drag
        tl.event_generate("<B1-Motion>", x=x0 + (x1 - x0) * i // 5, y=y)
        pump(app, 0.01)
    tl.event_generate("<ButtonRelease-1>", x=x1, y=y)
    wait_frame(app)


def fields(app):
    return bb.parse_time(app.start_text.get()), bb.parse_time(app.end_text.get())


# Seeking --------------------------------------------------------------------

def test_seeking_lands_on_frame_times(app):
    assert app.frame.size == (320, 240) and app.shown_time == 0
    app.step(1, frame=True)
    wait_frame(app)
    assert app.pos.get() == pytest.approx(1 / FPS, abs=1e-6)
    app.seek(1.01)  # between two frames: snaps to the one shown
    wait_frame(app)
    assert app.pos.get() == pytest.approx(app.shown_time) == pytest.approx(31 / FPS)
    key(app, "Shift-Right")
    wait_frame(app)
    assert app.pos.get() == pytest.approx(32 / FPS)
    key(app, "Left")
    wait_frame(app)
    assert app.pos.get() == pytest.approx(2 / FPS)


# Areas ----------------------------------------------------------------------

def test_draw_select_and_move_areas(app):
    drag(app, (20, 20), (120, 80))  # no area yet: creates area 1
    assert len(app.areas) == 1 and rect(app) == pytest.approx((20, 20, 100, 60), abs=2)
    drag(app, (200, 150), (250, 200))  # empty space: redraws the selected area
    assert len(app.areas) == 1 and rect(app) == pytest.approx((200, 150, 50, 50), abs=2)
    app.new_area()
    drag(app, (20, 20), (100, 80))
    assert len(app.areas) == 2 and app.cur == 1
    drag(app, (220, 170), (230, 180))  # inside area 1: selects and moves it
    assert app.cur == 0 and rect(app, 0) == pytest.approx((210, 160, 50, 50), abs=3)
    assert rect(app, 1) == pytest.approx((20, 20, 80, 60), abs=2)


def test_resize_and_cursor(app):
    app.new_area()
    app.set_rect(100, 60, 80, 60)
    pump(app)
    cursors = {(180, 120): "bottom_right_corner", (100, 60): "top_left_corner",
               (180, 90): "sb_h_double_arrow", (140, 60): "sb_v_double_arrow",
               (140, 90): "fleur", (250, 200): "crosshair"}
    for point, cursor in cursors.items():
        assert hover(app.canvas, *to_canvas(app, *point)) == cursor, point
    drag(app, (180, 120), (220, 150))  # bottom-right corner
    assert rect(app) == pytest.approx((100, 60, 120, 90), abs=2)
    drag(app, (100, 90), (60, 90))  # left edge: height untouched
    assert rect(app) == pytest.approx((60, 60, 160, 90), abs=2)
    drag(app, (220, 100), (0, 100))  # right edge past the left one: stops, no flip
    assert rect(app)[2] == bb.MIN_AREA_PX
    x = rect(app)[0] + 1
    drag(app, (x, 150), (x, 5000))  # bottom edge beyond the picture: the frame border
    assert rect(app)[1] + rect(app)[3] == app.info.height
    assert len(app.areas) == 1  # resizing never creates an area


def test_widgets_edit_the_selected_area(app):
    app.new_area()
    app.set_rect(10, 20, 30, 40)
    app.mode.set("pixelate")
    app.strength.set("7")
    a = app.areas[0]
    assert (a.x, a.y, a.w, a.h, a.mode, a.strength) == (10, 20, 30, 40, "pixelate", 7)
    app.strength.set("")  # half-typed: the last valid value stays
    assert a.strength == 7
    app.duplicate_area()
    assert len(app.areas) == 2 and (rect(app)[:2], app.areas[1].mode) == ((30, 40), "pixelate")
    app.delete_area()
    assert len(app.areas) == 1 and app.cur == 0


def test_new_area_does_not_pile_up_undrawn_areas(app):
    app.new_area()
    app.new_area()  # area 1 still empty: stays selected, nothing added
    assert len(app.areas) == 1 and "not drawn yet" in app.status.get()
    app.full.set(True)  # a whole-frame area needs no drawing
    app.new_area()
    assert len(app.areas) == 2 and app.cur == 1


def test_whole_frame(app, dialogs):
    app.new_area()
    app.set_rect(100, 60, 80, 60)
    app.full.set(True)
    a = app.areas[0]
    assert a.clipped(320, 240) == (0, 0, 320, 240) and "whole frame" in app.area_list.get(0)
    assert all(str(b.cget("state")) == "disabled" for b in app.rect_boxes)
    pump(app)
    # no resize handles, and clicks go through it: a drag draws a new area
    assert hover(app.canvas, *to_canvas(app, 180, 120)) == "crosshair"
    drag(app, (20, 20), (60, 50))
    assert len(app.areas) == 2 and app.cur == 1 and not app.full.get()
    app.select_area(0)
    assert app.full.get()
    app.full.set(False)  # the rectangle comes back
    assert rect(app, 0) == (100, 60, 80, 60)
    app.full.set(True)
    app.delete_area()  # leaves the drawn area 2
    dialogs["save_as"] = None  # Render: stop at the file dialog, validation passed
    app.new_area()
    app.full.set(True)  # never drawn, yet renderable
    app.render()
    assert dialogs["errors"] == []


# Time ranges ----------------------------------------------------------------

def test_mark_keys_and_enter_add_a_range(app, dialogs):
    app.new_area()
    app.seek(0.5)
    wait_frame(app)
    key(app, "Key-i")
    app.seek(2.0)
    wait_frame(app)
    key(app, "Key-o")
    key(app, "Return")
    assert app.areas[0].ranges == [(0.5, 2.0)] and app.start_text.get() == ""
    for start, end in [("2", "1"), ("abc", "1")]:  # refused
        app.start_text.set(start)
        app.end_text.set(end)
        app.add_range()
    assert len(dialogs["errors"]) == 2
    app.start_text.set("2.5")
    app.end_text.set("500")  # clamped to the video's end
    app.add_range()
    assert app.areas[0].ranges == [(0.5, 2.0), (2.5, app.info.duration)]


def test_range_edit_update_and_cancel(ranged):
    assert not ranged.cancel_edit_btn.winfo_ismapped()
    ranged.edit_range(0)
    pump(ranged)
    assert ranged.add_btn.cget("text") == "Update range" and ranged.cancel_edit_btn.winfo_ismapped()
    ranged.end_text.set("2.2")
    ranged.cancel_edit_btn.invoke()  # discards, and clears the fields
    pump(ranged)
    assert ranged.areas[0].ranges == SAVED and ranged.start_text.get() == ""
    assert not ranged.cancel_edit_btn.winfo_ismapped()
    ranged.edit_range(1)
    key(ranged, "Escape")  # Esc cancels too
    assert ranged.editing is None
    ranged.edit_range(0)
    ranged.end_text.set("2.2")
    ranged.add_btn.invoke()  # Update range
    assert ranged.areas[0].ranges == [(1.0, 2.2), (2.5, 2.8)] and ranged.editing is None


def test_timeline_only_seeks_outside_edit_mode(ranged):
    tl = ranged.timeline
    for t in (1.0, 1.5):  # an edge and a middle: normal cursor
        assert hover(tl, int(round(tl.x_of(t))), BAND_Y) == ""
    tl_drag(ranged, 1.5, 1.2)
    assert ranged.areas[0].ranges == SAVED and ranged.pos.get() == pytest.approx(1.2, abs=TOL)


def test_double_click_on_timeline_edits_the_range(ranged):
    tl = ranged.timeline
    for t, index in [(1.5, 0), (2.65, 1), (0.4, None)]:
        ranged.cancel_edit()
        x = int(round(tl.x_of(t)))
        for _ in range(2):  # a real double-click: Tk sees two quick presses
            tl.event_generate("<ButtonPress-1>", x=x, y=BAND_Y)
            tl.event_generate("<ButtonRelease-1>", x=x, y=BAND_Y)
        pump(ranged)
        assert ranged.editing == index, t
    assert ranged.areas[0].ranges == SAVED


def test_drag_in_edit_mode_changes_start_end(ranged):
    ranged.edit_range(0)
    pump(ranged)
    tl = ranged.timeline
    assert hover(tl, int(round(tl.x_of(2.0))), BAND_Y) == "sb_h_double_arrow"
    assert hover(tl, int(round(tl.x_of(2.65))), BAND_Y) == ""  # not the edited range
    tl_drag(ranged, 2.0, 2.3)  # end edge
    s, e = fields(ranged)
    assert s == 1.0 and e == pytest.approx(2.3, abs=TOL)
    assert ranged.pos.get() == pytest.approx(e, abs=TOL)  # the video followed the edge
    assert ranged.areas[0].ranges == SAVED  # not applied yet
    tl_drag(ranged, 1.5, 1.2)  # middle: moves it keeping its length
    s2, e2 = fields(ranged)
    assert e2 - s2 == pytest.approx(e - s, abs=1e-3)
    tl_drag(ranged, 2.65, 2.2)  # the other range: just seeks
    assert fields(ranged) == (s2, e2)
    ranged.add_range()  # Update range applies it
    assert ranged.areas[0].ranges[0] == (s2, e2)


def test_drag_is_clamped_and_cancel_discards_it(ranged):
    ranged.edit_range(0)
    pump(ranged)
    tl_drag(ranged, 1.5, -5)  # moved far left: stops at 0
    assert fields(ranged) == pytest.approx((0.0, 1.0), abs=1e-3)
    tl_drag(ranged, 1.0, -5)  # end edge past the start: one frame long
    s, e = fields(ranged)
    assert e - s == pytest.approx(1 / FPS, abs=1e-3)
    ranged.cancel_edit()
    assert ranged.areas[0].ranges == SAVED


# ffmpeg window --------------------------------------------------------------

def test_ffmpeg_window(app):
    app.new_area()  # not drawn yet
    app.show_ffmpeg()
    win = app.ffmpeg_window
    assert "Not ready: Area 1 has not been drawn" in win.text.get("1.0", "end")
    assert str(win.copy_btn.cget("state")) == "disabled"
    app.set_rect(10, 10, 50, 50)
    app.show_ffmpeg()  # brings the same window forward, refreshed
    text = win.text.get("1.0", "end")
    assert bb.ffmpeg_version() in text
    cmd, _ = app.render_command(app.default_output(".mp4"))
    assert win.command == bb.shell_command(cmd) and win.command in text
    win.suffix.set(".webm")
    win.refresh()
    assert "libvpx-vp9" in win.command
    win.copy()
    assert app.root.clipboard_get() == win.command
    win.destroy()


# Projects -------------------------------------------------------------------

def test_project_roundtrip(app, dialogs, tmp_path, media):
    app.new_area()
    app.set_rect(10, 20, 100, 50)
    app.mode.set("blur")
    app.start_text.set("0.5")
    app.end_text.set("1.5")
    app.add_range()
    app.full.set(False)
    app.new_area()
    app.full.set(True)
    app.crf.set("22")
    app.seek(1.0)
    wait_frame(app)
    assert app._dirty()
    project = tmp_path / "p.json"
    dialogs["save_as"] = str(project)
    assert app.save_project() and not app._dirty()
    data = json.loads(project.read_text(encoding="utf-8"))
    assert data["crf"] == 22 and data["position"] == 1.0
    app.open_video(str(media["mp4"]))  # start over, then reload
    app.open_project(str(project))
    wait_frame(app)
    assert [a.to_json() for a in app.areas] == data["areas"]
    assert app.crf.get() == "22" and app.pos.get() == pytest.approx(1.0) and not app._dirty()


def test_project_finds_the_video_next_to_it(app, dialogs, tmp_path, media):
    folder = tmp_path / "a"
    folder.mkdir()
    shutil.copy(media["mp4"], folder / "clip.mp4")
    app.open_video(str(folder / "clip.mp4"))
    app.new_area()
    app.set_rect(1, 2, 30, 40)
    dialogs["save_as"] = str(folder / "p.json")
    app.save_project()
    # A copy elsewhere (the app holds the original open, so Windows would
    # refuse a rename) must use the video next to it, not the original
    moved = tmp_path / "b"
    shutil.copytree(folder, moved)
    app.open_project(str(moved / "p.json"))
    wait_frame(app)
    assert app.video == moved / "clip.mp4" and not dialogs["errors"]


def test_project_file_without_areas_is_rejected(app, dialogs, tmp_path):
    bad = tmp_path / "old.json"
    bad.write_text(json.dumps({"app": "blurbox", "area": {"x": 1}}), encoding="utf-8")
    app.open_project(str(bad))
    assert dialogs["errors"] == ["Not a blurbox project file."]


def test_unsaved_changes_prompt(app, dialogs, tmp_path):
    app.new_area()
    app.set_rect(1, 2, 30, 40)
    dialogs["discard"] = None  # Cancel
    assert not app._confirm_discard()
    dialogs["discard"] = False  # Don't save
    assert app._confirm_discard()
    dialogs["discard"] = True  # Save
    dialogs["save_as"] = str(tmp_path / "p.json")
    assert app._confirm_discard() and (tmp_path / "p.json").exists()


def test_render_refuses_undrawn_areas(app, dialogs):
    app.new_area()
    app.render()
    assert dialogs["errors"] == ["Area 1 has not been drawn: draw or delete it."]
