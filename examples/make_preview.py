"""Build docs/tabscroll-preview.webp: every theme stacked in one looping animation.

All themes are drawn from the same moment of the demo tab in every frame, so
they stay perfectly in sync (two separate animated images never would: each
one starts playing whenever it finishes loading).

    python examples/make_demo.py        # if examples/demo.gp doesn't exist yet
    python examples/make_preview.py
"""
import os
import subprocess
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tabscroll as T  # noqa: E402

START, SECONDS, FPS, WIDTH = 2.6, 7.0, 30, 960
MARGIN = 24                                   # around and between the strips, before scaling


def main():
    sc = T.load_score(os.path.join(ROOT, "examples", "demo.gp"))
    renderers = [T.THEMES[name](sc) for name in ("studio", "ink")]
    cw = max(r.W for r in renderers) + 2 * MARGIN
    ch = sum(r.H for r in renderers) + MARGIN * (len(renderers) + 1)
    top, bottom = np.array([51, 42, 38], np.float32), np.array([19, 15, 14], np.float32)   # BGR
    ramp = np.linspace(0, 1, ch, dtype=np.float32)[:, None, None]
    backdrop = top * (1 - ramp) + bottom * ramp
    backdrop = np.broadcast_to(backdrop, (ch, cw, 3)).copy()
    out_h = int(round(ch * WIDTH / cw / 2)) * 2
    out = os.path.join(ROOT, "docs", "tabscroll-preview.webp")
    ff = subprocess.Popen(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{out_h}", "-r", str(FPS), "-i", "-",
                           "-c:v", "libwebp_anim", "-lossless", "0", "-quality", "82", "-loop", "0", out],
                          stdin=subprocess.PIPE)
    for k in range(int(SECONDS * FPS)):
        t = START + k / FPS
        frame = backdrop.copy()
        y = MARGIN
        for r in renderers:
            bgra = r.render(t).astype(np.float32)
            a = bgra[..., 3:4] / 255.0
            region = frame[y:y + r.H, MARGIN:MARGIN + r.W]
            region[:] = bgra[..., :3] * a + region * (1 - a)
            y += r.H + MARGIN
        small = cv2.resize(frame, (WIDTH, out_h), interpolation=cv2.INTER_AREA)
        ff.stdin.write(np.clip(small + 0.5, 0, 255).astype(np.uint8).tobytes())
    ff.stdin.close()
    sys.exit(ff.wait())


if __name__ == "__main__":
    main()
