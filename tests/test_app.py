"""The GUI, driven through its methods and real Tk events: areas, ranges,
keys, seeking and projects. Needs a display (skipped without one) and
ffmpeg for the test video."""

import json
import time
import tkinter as tk

import pytest

import video_cover_region as vcr
from conftest import FPS

pytestmark = [pytest.mark.gui, pytest.mark.media]


@pytest.fixture
def dialogs(monkeypatch):
    """Replace every dialog; tests set the answers and read what was shown."""
    state = {"errors": [], "save_as": None, "open": None, "discard": False}
    monkeypatch.setattr(vcr.messagebox, "showerror", lambda title, msg: state["errors"].append(msg))
    monkeypatch.setattr(vcr.messagebox, "showwarning", lambda *a: None)
    monkeypatch.setattr(vcr.messagebox, "showinfo", lambda *a: None)
    monkeypatch.setattr(vcr.messagebox, "askyesnocancel", lambda *a: state["discard"])
    monkeypatch.setattr(vcr.filedialog, "asksaveasfilename", lambda **k: state["save_as"])
    monkeypatch.setattr(vcr.filedialog, "askopenfilename", lambda **k: state["open"])
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
    a = vcr.App(window, str(media["mp4"]))
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


# Seeking --------------------------------------------------------------------

def test_opens_on_first_frame(app):
    assert app.frame is not None and app.shown_time == 0
    assert app.frame.size == (320, 240)


def test_frame_steps_land_on_frame_times(app):
    for n in range(1, 4):
        app.step(1, frame=True)
        wait_frame(app)
        assert app.pos.get() == pytest.approx(n / FPS, abs=1e-6)
    app.step(-1)  # one second back, clamped at 0
    wait_frame(app)
    assert app.pos.get() == 0


def test_seek_snaps_to_the_frame_shown(app):
    app.seek(1.01)  # between two frames
    wait_frame(app)
    assert app.pos.get() == pytest.approx(app.shown_time) == pytest.approx(31 / FPS)


def test_arrow_keys_seek(app):
    key(app, "Right")
    wait_frame(app)
    assert app.pos.get() == pytest.approx(1.0)
    key(app, "Shift-Right")
    wait_frame(app)
    assert app.pos.get() == pytest.approx(1 + 1 / FPS)


# Areas ----------------------------------------------------------------------

def drag(app, x0, y0, x1, y1):
    """Drag on the canvas between two points given in video pixels."""
    def to_canvas(x, y):
        return int(app.offx + x * app.scale), int(app.offy + y * app.scale)
    (a, b), (c, d) = to_canvas(x0, y0), to_canvas(x1, y1)
    app.canvas.event_generate("<ButtonPress-1>", x=a, y=b)
    app.canvas.event_generate("<B1-Motion>", x=c, y=d)
    app.canvas.event_generate("<ButtonRelease-1>", x=c, y=d)
    pump(app)


def test_drawing_creates_then_redraws_the_selected_area(app):
    drag(app, 20, 20, 120, 80)
    assert len(app.areas) == 1 and app.cur == 0
    a = app.areas[0]
    assert (a.x, a.y) == pytest.approx((20, 20), abs=2) and a.w == pytest.approx(100, abs=2)
    drag(app, 200, 150, 250, 200)  # empty space: redraws area 1, no new area
    assert len(app.areas) == 1 and app.areas[0].x == pytest.approx(200, abs=2)


def test_new_area_and_click_to_select_and_move(app):
    drag(app, 20, 20, 100, 80)
    app.new_area()
    drag(app, 200, 150, 280, 220)
    assert len(app.areas) == 2 and app.cur == 1
    drag(app, 50, 50, 70, 60)  # press inside area 1: selects it and moves it
    assert app.cur == 0
    assert (app.areas[0].x, app.areas[0].y) == pytest.approx((40, 30), abs=3)
    assert app.areas[1].x == pytest.approx(200, abs=2)  # untouched


def test_widgets_edit_the_selected_area(app):
    app.new_area()
    app.set_rect(10, 20, 30, 40)
    app.mode.set("pixelate")
    app.strength.set("7")
    a = app.areas[0]
    assert (a.x, a.y, a.w, a.h, a.mode, a.strength) == (10, 20, 30, 40, "pixelate", 7)
    app.strength.set("")  # half-typed: the last valid value stays
    assert a.strength == 7


def test_duplicate_and_delete(app):
    app.new_area()
    app.set_rect(10, 10, 50, 50)
    app.mode.set("blur")
    app.duplicate_area()
    assert len(app.areas) == 2 and app.cur == 1
    b = app.areas[1]
    assert (b.x, b.y, b.mode) == (30, 30, "blur")
    app.delete_area()
    assert len(app.areas) == 1 and app.cur == 0
    app.delete_area()
    assert app.areas == [] and app.cur is None


# Ranges ---------------------------------------------------------------------

def test_mark_keys_and_enter_add_a_range(app):
    app.new_area()
    app.seek(0.5)
    wait_frame(app)
    key(app, "Key-i")
    app.seek(2.0)
    wait_frame(app)
    key(app, "Key-o")
    key(app, "Return")
    assert app.areas[0].ranges == [(0.5, 2.0)]
    assert app.start_text.get() == app.end_text.get() == ""


def test_edit_and_cancel_range(app):
    app.new_area()
    app.start_text.set("1")
    app.end_text.set("2")
    app.add_range()
    app.range_list.selection_set(0)
    app.edit_range()
    assert app.add_btn.cget("text") == "Update range"
    app.end_text.set("2.5")
    app.add_range()
    assert app.areas[0].ranges == [(1.0, 2.5)] and app.editing is None
    app.range_list.selection_set(0)
    app.edit_range()
    app.cancel_edit()
    assert app.add_btn.cget("text") == "Add range" and app.areas[0].ranges == [(1.0, 2.5)]


def test_invalid_ranges_are_refused(app, dialogs):
    app.new_area()
    for start, end in [("2", "1"), ("abc", "1"), ("1", "1")]:
        app.start_text.set(start)
        app.end_text.set(end)
        app.add_range()
    assert app.areas[0].ranges == [] and len(dialogs["errors"]) == 3


def test_range_end_is_clamped_to_duration(app):
    app.new_area()
    app.start_text.set("1")
    app.end_text.set("500")
    app.add_range()
    assert app.areas[0].ranges == [(1.0, app.info.duration)]


# Projects -------------------------------------------------------------------

def test_project_roundtrip(app, dialogs, tmp_path, media):
    app.new_area()
    app.set_rect(10, 20, 100, 50)
    app.mode.set("blur")
    app.strength.set("12")
    app.start_text.set("0.5")
    app.end_text.set("1.5")
    app.add_range()
    app.new_area()
    app.set_rect(150, 100, 60, 60)
    app.crf.set("22")
    app.seek(1.0)
    wait_frame(app)
    assert app._dirty()

    project = tmp_path / "p.json"
    dialogs["save_as"] = str(project)
    assert app.save_project()
    assert not app._dirty() and app.root.title().startswith("p.json")
    data = json.loads(project.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["crf"] == 22 and data["position"] == 1.0
    assert data["video_absolute"] == str(media["mp4"].resolve())

    app.open_video(str(media["mp4"]))  # start over, then reload
    assert app.areas == []
    app.open_project(str(project))
    wait_frame(app)
    assert [a.to_json() for a in app.areas] == data["areas"]
    assert app.crf.get() == "22" and app.pos.get() == pytest.approx(1.0)
    assert not app._dirty()


def test_project_finds_video_next_to_it_after_a_move(app, dialogs, tmp_path, media):
    import shutil
    folder = tmp_path / "a"
    folder.mkdir()
    shutil.copy(media["mp4"], folder / "clip.mp4")
    app.open_video(str(folder / "clip.mp4"))
    wait_frame(app)
    app.new_area()
    app.set_rect(1, 2, 30, 40)
    dialogs["save_as"] = str(folder / "p.json")
    app.save_project()
    # A copy elsewhere (the app holds the original open, so Windows would
    # refuse a rename): its project must pick the video next to it, not the
    # original its absolute path still names
    moved = tmp_path / "b"
    shutil.copytree(folder, moved)
    app.open_project(str(moved / "p.json"))
    wait_frame(app)
    assert app.video == moved / "clip.mp4" and app.areas[0].w == 30 and not dialogs["errors"]


def test_project_file_without_areas_is_rejected(app, dialogs, tmp_path):
    bad = tmp_path / "old.json"
    bad.write_text(json.dumps({"app": "video_cover_region", "area": {"x": 1}}), encoding="utf-8")
    app.open_project(str(bad))
    assert dialogs["errors"] == ["Not a video_cover_region project file."]


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
