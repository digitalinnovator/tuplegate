"""Render docs/demo.gif from a real run of examples/demo.py.

    uv run python examples/demo.py > demo.txt
    uv run --extra llamaindex python scripts/make_demo_gif.py demo.txt docs/demo.gif

Pillow comes in with the llamaindex extra. Needs the macOS Menlo font; elsewhere, pass a
monospace TTF as the third argument.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = {
    "bg": (13, 17, 23),
    "fg": (201, 209, 217),
    "dim": (139, 148, 158),
    "red": (248, 81, 73),
    "green": (63, 185, 80),
    "yellow": (210, 153, 34),
    "blue": (88, 166, 255),
}
NAMES = list(COLORS)
PALETTE = [v for n in NAMES for v in COLORS[n]] + [0] * (768 - 3 * len(NAMES))
W, H, PAD, LH = 900, 470, 20, 22
CMD = "$ uv run python examples/demo.py"


def colour(line: str) -> str:
    if "security only" in line:
        return "red"
    if "Handbook" in line:
        return "green"
    if line.startswith("revoke:"):
        return "yellow"
    if line.startswith("engine:"):
        return "dim"
    if "->" in line and "ms" in line:
        return "blue"
    return "fg"


def main() -> int:
    src, dest = sys.argv[1], sys.argv[2]
    font = ImageFont.truetype(
        sys.argv[3] if len(sys.argv) > 3 else "/System/Library/Fonts/Menlo.ttc", 15
    )
    lines = [CMD, *Path(src).read_text(encoding="utf-8").splitlines()]
    frames: list[Image.Image] = []
    durations: list[int] = []

    def frame(n: int, ms: int) -> None:
        im = Image.new("P", (W, H), NAMES.index("bg"))
        im.putpalette(PALETTE)
        d = ImageDraw.Draw(im)
        for i, line in enumerate(lines[:n]):
            d.text(
                (PAD, PAD + i * LH),
                line,
                font=font,
                fill=NAMES.index("blue" if i == 0 else colour(line)),
            )
        frames.append(im)
        durations.append(ms)

    frame(1, 900)
    for n in range(2, len(lines) + 1):
        line = lines[n - 1]
        pause = 1400 if line.startswith("revoke:") else (900 if "security only" in line else 220)
        frame(n, pause)
    durations[-1] = 4000
    frames[0].save(
        dest, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True
    )
    print(f"wrote {dest}: {len(frames)} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
