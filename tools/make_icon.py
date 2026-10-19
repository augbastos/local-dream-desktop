# Generates assets/icon.png and local-dream-desktop.ico (run once per output path).
# Original artwork made for this project, not derived from the Local Dream app icon.
# Usage: python tools/make_icon.py assets/icon.png
#        python tools/make_icon.py local-dream-desktop.ico
import sys
from PIL import Image, ImageDraw
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(29, 27, 32, 255))

def star(cx, cy, r, w, color):
    pts = [(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w), (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)]
    d.polygon(pts, fill=color)
star(118, 140, 82, 20, (207, 188, 255, 255))   # lavender primary
star(190, 70, 34, 9, (244, 201, 93, 255))      # gold sparkle
img.save(sys.argv[1], sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)])
