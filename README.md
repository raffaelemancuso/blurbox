# blurbox

A small desktop tool to hide parts of a video: draw one or more rectangles on
the frame and cover each with a black box, a blur or pixelation, all the
time or only during chosen time ranges. It is a GUI around ffmpeg and
runs on Windows and Linux.

## Requirements

- Python 3.11 or newer
- ffmpeg and ffprobe on `PATH` (they are separate programs, not installed
  with blurbox); a full build is recommended, so the encoders used to keep
  the source codec (libx265, libsvtav1, libvpx-vp9, libopus) are available.
  On Windows, e.g. the "full" build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/);
  on Linux, your distribution's `ffmpeg` package
- Tk: included with Python on Windows; on Linux `pacman -S tk` (Arch) or
  `apt install python3-tk` (Debian/Ubuntu)

## Installing

With [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/), which
put the `blurbox` command on your `PATH` in an environment of its own:

```sh
uv tool install blurbox
# or
pipx install blurbox
```

Then start it, optionally with a video or a project file to open:

```sh
blurbox [video | project.json]
```

On Windows the command starts without a console window.

## Running from a source checkout

```sh
uv run --script blurbox.py [video | project.json]
```

On Windows, `blurbox_shortcut.ps1` creates `blurbox.lnk`,
a shortcut that starts the tool without a console window; add `-Desktop` for
a copy on the Desktop. Drop a video or a project onto the shortcut to open
it. Rerun the script after reinstalling uv or moving this folder, since a
shortcut stores absolute paths.

## Using it

- **Areas**: drag on the frame to draw the selected area, drag inside any
  area to select and move it, drag an edge or corner of the selected area
  (it shows white handles) to resize it, "New area" to add another. The
  mouse cursor shows which of these a drag will do. Each area has its own
  effect, strength and time ranges; with no ranges it is on all the time
  (listed as "always").
- **Whole frame**: tick it to make the selected area cover the entire
  picture (e.g. blur everything from 0:12 to 0:15); untick it to get its
  rectangle back.
- **Seeking**: click or drag the timeline, use the step buttons (hold them
  to repeat), or the keys ←/→ (1 s) and Shift+←/→ (one frame).
- **Time ranges**: I and O put the current time in Start and End, Enter adds
  the range, a double-click on a range edits it (Cancel or Esc discards the
  edit). The timeline shows the selected area's ranges in red, the others'
  in grey. Drag an edge of a red range to change that end, with the video
  following the edge so you can place it on the exact frame, or drag its
  middle to move the whole range; a plain click still seeks.
- **Show effect** (E) draws every active area's effect on the frame, using
  the same ffmpeg filters as the render.
- **Projects**: Ctrl+S saves the areas, quality and position as JSON,
  Ctrl+O opens a project. The video is found relative to the project file
  first, so a folder with both can be moved to another PC.

## Output

The render keeps the source's codec family (H.264, HEVC, VP9, AV1), bit depth
and colour metadata (HDR included), every audio track, subtitles and chapters.
Anything the chosen container cannot hold is converted (losslessly where
possible) or left out, and a dialog lists it before rendering starts.

The quality setting is x264/x265-style CRF: lower means better and larger.
The same number looks better with HEVC than with H.264, so 22–24 is usually
enough for HEVC sources.

## Development

```sh
uv sync            # creates .venv with the dependencies and pytest
uv run pytest      # the test suite, about 20 s
```

`pyproject.toml` repeats the dependencies of the script's inline metadata
(which `uv run --script` and the shortcut use); keep the two in sync.

The tests are in three files:

- `tests/test_logic.py`: time parsing, areas, filtergraphs and the stream
  plan, with no ffmpeg run
- `tests/test_media.py`: builds small videos with ffmpeg once per session,
  then checks probing, frame reading against a plain sequential decode, the
  live preview against the real render frame by frame, 10-bit and HDR
  preservation, and the streams in rendered files
- `tests/test_app.py`: drives the GUI through real Tk events (areas, ranges,
  keys, seeking, projects); needs a display

Tests that need ffmpeg or a display are skipped when these are missing;
`-m "not gui"` or `-m "not media"` deselects them explicitly.
