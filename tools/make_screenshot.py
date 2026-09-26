"""Take docs/screenshot.png: blurbox on a frame of Big Buck Bunny with three
areas, one per effect, the blurred one selected.

Needs docs/bbb_clip.mp4 (git-ignored): 30 s of the film from 1:45, so the
frame at 0:15 shows the three rodents. To make it, download and unzip
https://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_720p_h264.mov.zip
and run
    ffmpeg -ss 105 -t 30 -i big_buck_bunny_720p_h264.mov -map 0:v:0 -map 0:a:0
           -c:v libx264 -crf 20 -preset slow -pix_fmt yuv420p -c:a aac -b:a 128k
           -movflags +faststart docs/bbb_clip.mp4

The window is captured from the screen, so this runs on Windows, at 100 %
display scaling for a 1280x860 image, in the light theme.

Usage:
    uv run python tools/make_screenshot.py
"""

import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import blurbox as bb  # noqa: E402

CLIP = ROOT / "docs" / "bbb_clip.mp4"
# (mode, strength, rectangle x, y, w, h, ranges in seconds); none = always
AREAS = [
    ("pixelate", 22, (220, 270, 290, 200), [(10, 20)]),  # the squirrel's face
    ("blur", 25, (470, 55, 245, 205), [(3, 8), (12, 22)]),  # Frank's head
    ("black", 20, (745, 328, 170, 52), []),  # the chinchilla's eyes
]
SELECTED = 1  # the blurred one: its ranges show in red on the timeline


def pump(root: tk.Tk, seconds: float):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def main():
    if not CLIP.is_file():
        sys.exit(f"{CLIP} is missing: see the top of {Path(__file__).name} to make it.")
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # as blurbox.main does
    root = tk.Tk()
    app = bb.App(root, str(CLIP))
    app.dark.set(False)
    for mode, strength, rect, ranges in AREAS:
        app.new_area()
        app.set_rect(*rect)
        app.mode.set(mode)
        app.strength.set(str(strength))
        for s, e in ranges:
            app.start_text.set(str(s))
            app.end_text.set(str(e))
            app.add_range()
    app.select_area(SELECTED)
    app.seek(15)
    app.status.set("")
    root.attributes("-topmost", True)  # nothing may cover the captured region
    pump(root, 2)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    image = ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height()),
                           all_screens=True)
    image.save(ROOT / "docs" / "screenshot.png", optimize=True)
    print(f"docs/screenshot.png: {image.width}x{image.height}")
    app.saved = app._snapshot()  # nothing to save on close
    app.close()


if __name__ == "__main__":
    main()
