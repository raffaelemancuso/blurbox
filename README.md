# video_cover_region

A small desktop tool to hide parts of a video: draw one or more rectangles on
the frame and cover each with a black box, a blur or pixelation, for the
whole video or only during chosen time ranges. It is a GUI around ffmpeg and
runs on Windows and Linux.

## Requirements

- [uv](https://docs.astral.sh/uv/), which installs the Python dependencies
  (PyAV, Pillow) on first run
- ffmpeg and ffprobe on `PATH`; a full build is recommended, so the encoders
  used to keep the source codec (libx265, libsvtav1, libvpx-vp9, libopus)
  are available
- Tk: included with Python on Windows; on Linux `pacman -S tk` (Arch) or
  `apt install python3-tk` (Debian/Ubuntu)

## Running

```sh
uv run --script video_cover_region.py [video | project.json]
```

On Windows, `video_cover_region_shortcut.ps1` creates `video_cover_region.lnk`,
a shortcut that starts the tool without a console window; add `-Desktop` for
a copy on the Desktop. Drop a video or a project onto the shortcut to open
it. Rerun the script after reinstalling uv or moving this folder, since a
shortcut stores absolute paths.

## Using it

- **Areas**: drag on the frame to draw the selected area, drag inside any
  area to select and move it, "New area" to add another. Each area has its
  own effect, strength and time ranges; with no ranges it covers the whole
  video.
- **Seeking**: click or drag the timeline, use the step buttons (hold them
  to repeat), or the keys ←/→ (1 s) and Shift+←/→ (one frame).
- **Time ranges**: I and O put the current time in Start and End, Enter adds
  the range, a double-click on a range edits it, Esc cancels the edit. The
  timeline shows the selected area's ranges in red, the others' in grey.
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
