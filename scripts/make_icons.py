"""Generate the PWA icons in app/static/icons/ from the SVG below.

    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w python:3.13-slim \
      sh -c "pip install -q --user pymupdf==1.26.5 && python scripts/make_icons.py"
"""
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "app/static/icons"
BG = "#2f6fde"  # same as --accent in index.html

# One entry point fanning out to three backends (ingress -> services), drawn in a 512x512 box.
# Content is kept inside the central 80% so the same drawing also works as a maskable icon.
CONTENT = """
  <g fill="none" stroke="#fff" stroke-width="26" stroke-linecap="round" stroke-linejoin="round">
    <path d="M176 256H236"/>
    <path d="M236 256C276 256 276 160 316 160H330"/>
    <path d="M236 256H330"/>
    <path d="M236 256C276 256 276 352 316 352H330"/>
  </g>
  <circle cx="148" cy="256" r="44" fill="#fff"/>
  <circle cx="148" cy="256" r="18" fill="BG"/>
  <rect x="326" y="128" width="64" height="64" rx="16" fill="#fff"/>
  <rect x="326" y="224" width="64" height="64" rx="16" fill="#fff"/>
  <rect x="326" y="320" width="64" height="64" rx="16" fill="#9fe3b8"/>
""".replace("BG", BG)


def svg(rounded: bool, scale: float = 1.0) -> str:
    bg = f'<rect width="512" height="512" rx="112" fill="{BG}"/>' if rounded \
        else f'<rect width="512" height="512" fill="{BG}"/>'
    off = 256 * (1 - scale)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">{bg}'
            f'<g transform="translate({off} {off}) scale({scale})">{CONTENT}</g></svg>')


def render(svg_text: str, size: int, path: Path):
    doc = pymupdf.open(stream=svg_text.encode(), filetype="svg")
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(size / 512, size / 512), alpha=True)
    pix.save(path)
    print("wrote", path, pix.width, "x", pix.height)


OUT.mkdir(parents=True, exist_ok=True)
(OUT / "icon.svg").write_text(svg(rounded=True))
render(svg(rounded=True), 192, OUT / "icon-192.png")
render(svg(rounded=True), 512, OUT / "icon-512.png")
render(svg(rounded=False, scale=0.8), 512, OUT / "icon-maskable-512.png")
# iOS applies its own rounded mask, so the touch icon is full-bleed.
render(svg(rounded=False, scale=0.9), 180, OUT / "apple-touch-icon.png")
