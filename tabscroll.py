#!/usr/bin/env python3
"""
tabscroll -- render a Guitar Pro 7/8 (.gp) guitar track as a horizontally
scrolling, transparent tab overlay for playthrough videos.

  python tabscroll.py song.gp --still 12.5            # preview one frame
  python tabscroll.py song.gp --out overlay            # full render

Timing is time-proportional: the strip scrolls at a constant speed and every
note crosses the playhead exactly when it is played.
"""

import argparse
import bisect
import math
import os
import random
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import skia

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "fonts")

NOTE_VALUES = {"Whole": 4.0, "Half": 2.0, "Quarter": 1.0, "Eighth": 0.5,
               "16th": 0.25, "32nd": 0.125, "64th": 0.0625}
PITCH_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

# GPIF slide flags
SL_SHIFT, SL_LEGATO, SL_OUT_DOWN, SL_OUT_UP, SL_IN_BELOW, SL_IN_ABOVE = 1, 2, 4, 8, 16, 32


# --------------------------------------------------------------------------
# Score model
# --------------------------------------------------------------------------

class Note:
    def __init__(self, beat, string, fret):
        self.beat = beat
        self.string = string          # display row: 0 = top line (highest string)
        self.fret = fret
        self.tie_origin = self.tie_dest = False
        self.hopo_origin = self.hopo_dest = False
        self.slide = 0
        self.palm_mute = self.dead = self.let_ring = False
        self.hidden = False           # tie destination: drawn as sustain only
        self.root = self              # first note of a tie chain
        self.end = None               # sounding end (seconds), set on chain roots
        self.tail = False             # draw a sustain tail

    @property
    def label(self):
        return "×" if self.dead else str(self.fret)


class Beat:
    def __init__(self, bar, start_beats, dur_beats, value, dots):
        self.bar = bar
        self.start_b = start_beats    # absolute, in quarter notes
        self.dur_b = dur_beats
        self.value = value            # nominal note value in quarters (no dots)
        self.dots = dots
        self.notes = []
        self.t0 = self.t1 = 0.0       # seconds

    @property
    def rest(self):
        return not self.notes


class Score:
    pass


def _props(el):
    return {p.get("name"): p for p in el.findall("Properties/Property")}


def load_score(path, track=0, drop_strings=(), tuning=None):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("Content/score.gpif"))

    def idmap(tag):
        return {e.get("id"): e for e in root.find(tag)}

    bars_x, voices_x, beats_x = idmap("Bars"), idmap("Voices"), idmap("Beats")
    notes_x, rhythms_x = idmap("Notes"), idmap("Rhythms")

    tr = root.find("Tracks")[track]
    pitches = [int(p) for p in tr.find(".//Property[@name='Tuning']/Pitches").text.split()]
    kept = [i for i in range(len(pitches)) if i not in drop_strings]
    # display row 0 = highest kept string
    row_of = {gs: len(kept) - 1 - k for k, gs in enumerate(kept)}

    sc = Score()
    sc.title = (root.findtext("Score/Title") or "").strip()
    sc.track_name = tr.findtext("Name").strip()
    sc.tuning = [PITCH_NAMES[pitches[gs] % 12] for gs in reversed(kept)]  # top row first
    if tuning:
        if len(tuning) != len(kept):
            raise SystemExit(f"--tuning has {len(tuning)} names but {len(kept)} strings are shown")
        sc.tuning = list(reversed(tuning))
    sc.nstrings = len(kept)

    tempos = {}
    for a in root.findall("MasterTrack/Automations/Automation"):
        if a.findtext("Type") == "Tempo":
            tempos[int(a.findtext("Bar"))] = float(a.findtext("Value").split()[0])
    if not tempos:
        tempos[0] = 120.0

    sc.bars = []          # (start_beats, length_beats, t_start, bpm, time_sig)
    sc.beats = []
    dropped = 0
    pos_b, pos_t, bpm = 0.0, 0.0, tempos.get(0, 120.0)
    for mi, mb in enumerate(root.find("MasterBars")):
        bpm = tempos.get(mi, bpm)
        num, den = (int(v) for v in mb.findtext("Time").split("/"))
        length = num * 4.0 / den
        sc.bars.append((pos_b, length, pos_t, bpm, (num, den)))
        bar = bars_x[mb.findtext("Bars").split()[track]]
        vids = [v for v in bar.findtext("Voices").split() if v != "-1"]
        if vids:
            cursor = pos_b
            for bid in voices_x[vids[0]].findtext("Beats").split():
                bx = beats_x[bid]
                rh = rhythms_x[bx.find("Rhythm").get("ref")]
                value = NOTE_VALUES[rh.findtext("NoteValue")]
                dot_el = rh.find("AugmentationDot")
                dots = int(dot_el.get("count")) if dot_el is not None else 0
                dur = value * (2 - 0.5 ** dots)
                tup = rh.find("PrimaryTuplet")
                if tup is not None:
                    dur *= int(tup.get("den")) / int(tup.get("num"))
                b = Beat(mi, cursor, dur, value, dots)
                spb = 60.0 / bpm
                b.t0 = pos_t + (cursor - pos_b) * spb
                b.t1 = b.t0 + dur * spb
                for nid in (bx.findtext("Notes") or "").split():
                    nx = notes_x[nid]
                    P = _props(nx)
                    gs = int(P["String"].findtext("String"))
                    if gs not in row_of:
                        dropped += 1
                        continue
                    n = Note(b, row_of[gs], int(P["Fret"].findtext("Fret")))
                    tie = nx.find("Tie")
                    if tie is not None:
                        n.tie_origin = tie.get("origin") == "true"
                        n.tie_dest = tie.get("destination") == "true"
                    n.hopo_origin = "HopoOrigin" in P
                    n.hopo_dest = "HopoDestination" in P
                    n.slide = int(P["Slide"].findtext("Flags")) if "Slide" in P else 0
                    n.palm_mute = "PalmMuted" in P
                    n.dead = "Muted" in P
                    n.let_ring = nx.find("LetRing") is not None
                    b.notes.append(n)
                b.notes.sort(key=lambda n: n.string)
                sc.beats.append(b)
                cursor += dur
        pos_t += length * 60.0 / bpm
        pos_b += length
    sc.end_t = pos_t
    sc.dropped = dropped
    _derive(sc)
    return sc


def _derive(sc):
    """Tie chains, sustain ends, legato pairs, technique spans, beam groups."""
    beats = sc.beats
    last_on = {}
    for b in beats:
        for n in b.notes:
            n.ghosts = []
            prev = last_on.get(n.string)
            if n.tie_dest and prev is not None:
                n.hidden = True
                n.root = prev.root
                n.root.end = b.t1
                n.root.ghosts.append(n)
            else:
                n.end = b.t1
            last_on[n.string] = n

    # next note on the same string (for slides / hammer-ons / let ring)
    nxt = {}
    for b in reversed(beats):
        for n in b.notes:
            n.next_same = nxt.get(n.string)
        for n in b.notes:
            nxt[n.string] = n

    # let ring: ring until the next note on that string or the end of the group
    sc.lr_spans = _spans(beats, lambda b: any(n.let_ring for n in b.notes))
    for (i0, i1) in sc.lr_spans:
        g_end = beats[i1].t1
        for b in beats[i0:i1 + 1]:
            for n in b.notes:
                if n.let_ring and not n.hidden:
                    stop = n.next_same.beat.t0 if n.next_same else g_end
                    n.end = max(n.end, min(stop, g_end))
                    n.tail = True
    sc.pm_spans = _spans(beats, lambda b: any(n.palm_mute for n in b.notes))

    for b in beats:
        for n in b.notes:
            if not n.hidden and n.end - b.t0 > 60.0 / sc.bars[b.bar][3] * 1.01:   # longer than a beat
                n.tail = True

    sc.legato = []   # (a, b, kind) kind in {'H','P','slide'}
    for b in beats:
        for n in b.notes:
            m = n.next_same
            if m is None:
                continue
            if n.hopo_origin and m.hopo_dest:
                sc.legato.append((n, m, "H" if m.fret > n.fret else "P"))
            if n.slide & (SL_LEGATO | SL_SHIFT):
                sc.legato.append((n, m, "slide"))

    # rhythm beaming: group consecutive sub-quarter note beats inside one quarter
    sc.beam_groups = []
    cur, cur_key = [], None
    for b in beats:
        bar_start = sc.bars[b.bar][0]
        key = (b.bar, int(math.floor(b.start_b - bar_start + 1e-6)))
        if b.rest or b.value >= 1.0:
            if cur:
                sc.beam_groups.append(cur)
            cur, cur_key = [], None
            continue
        if cur and key != cur_key:
            sc.beam_groups.append(cur)
            cur = []
        cur.append(b)
        cur_key = key
    if cur:
        sc.beam_groups.append(cur)


def _spans(beats, pred):
    """Index spans of beats satisfying pred; rests between two matching
    beats are bridged so a palm-mute line does not break at every rest."""
    spans, start, last = [], None, None
    for i, b in enumerate(beats):
        if pred(b):
            if start is None:
                start = i
            last = i
        elif b.rest and start is not None:
            continue
        else:
            if start is not None:
                spans.append((start, last))
            start = last = None
    if start is not None:
        spans.append((start, last))
    return spans


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------

def rgba(c, a=1.0):
    return skia.ColorSetARGB(int(round(max(0.0, min(1.0, a)) * 255)), *c)


def mkpaint(color=None, a=1.0, stroke=None, cap=None, blend=None, blur=None, shader=None):
    p = skia.Paint(AntiAlias=True)
    if color is not None:
        p.setColor(rgba(color, a))
    if stroke is not None:
        p.setStyle(skia.Paint.kStroke_Style)
        p.setStrokeWidth(stroke)
        p.setStrokeCap(cap or skia.Paint.kRound_Cap)
    if blend is not None:
        p.setBlendMode(blend)
    if blur:
        p.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, blur))
    if shader is not None:
        p.setShader(shader)
    return p


def load_font(name, size):
    tf = skia.Typeface.MakeFromFile(os.path.join(FONTS, name))
    f = skia.Font(tf, size)
    f.setSubpixel(True)
    f.setEdging(skia.Font.Edging.kAntiAlias)
    f.setHinting(skia.FontHinting.kNone)
    return f


def cap_height(font):
    """Height of the digit 0. Some fonts report an unreliable cap height."""
    return -font.getBounds(font.textToGlyphs("0"))[0].top()


def ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

class Renderer:
    def __init__(self, sc, scale=1.0, width=1920, variant="card", fps=60,
                 preroll=3.0, postroll=2.0, px_per_beat=200.0, playhead=0.22,
                 accent=None):
        self.sc, self.s, self.fps = sc, scale, fps
        self.variant = variant
        self.preroll, self.postroll = preroll, postroll
        s = scale
        self.W = int(round(width * s))
        self.sp = self.STRING_SPACING * s
        self.y0 = 80 * s
        self.ys = [self.y0 + i * self.sp for i in range(sc.nstrings)]
        self.yb = self.ys[-1]
        self.stem_top = self.yb + 19 * s
        self.stem_bot = self.stem_top + 25 * s
        self.H = int(round(self.stem_bot + 28 * s))
        self.H += self.H % 2
        self.card_l, self.card_r = 12 * s, self.W - 12 * s
        self.card_t, self.card_b = 8 * s, self.H - 8 * s
        self.lab_x = self.card_l + 28 * s
        self.P = round(self.W * playhead)
        bpm0 = sc.bars[0][3]
        self.pps = px_per_beat * s * bpm0 / 60.0           # pixels per second
        self.bar_off = 22 * s                             # bar line sits before the downbeat
        self.total = preroll + sc.end_t + postroll
        self.nframes = int(math.ceil(self.total * fps))

        self._style(accent)

        self.beat_t0 = [b.t0 for b in sc.beats]
        self.info = skia.ImageInfo.MakeN32Premul(self.W, self.H)
        assert self.info.colorType() == skia.kBGRA_8888_ColorType
        self.surf_a = skia.Surface(self.W, self.H)
        self.surf_b = skia.Surface(self.W, self.H)
        self.surf_c = skia.Surface(self.W, self.H)
        self.buf_a = np.zeros((self.H, self.W, 4), np.uint8)
        self.buf_b = np.zeros((self.H, self.W, 4), np.uint8)
        self.buf_c = np.zeros((self.H, self.W, 4), np.uint8)
        self.surf_d = skia.Surface(self.W, self.H)      # ambient layer behind the tab (optional)
        self.buf_d = np.zeros((self.H, self.W, 4), np.uint8)
        self._ambient = False
        self.bar_t0 = [b[2] for b in sc.bars]
        self._build_static()

    # -- theme hooks (override these in a theme subclass) ------------------------
    DEFAULT_ACCENT = (255, 183, 77)
    STRING_SPACING = 34

    def _style(self, accent):
        """Colours and fonts."""
        s = self.s
        self.ink = (242, 244, 248)
        self.accent = accent or self.DEFAULT_ACCENT
        self.dark = (22, 18, 12)
        self.string_a = 0.30 if self.variant == "card" else 0.40

        self.f_fret = load_font("Inter-SemiBold.ttf", 21 * s)
        self.f_small = load_font("Inter-Medium.ttf", 11.5 * s)
        self.f_tech = load_font("Inter-SemiBold.ttf", 11.5 * s)
        self.f_hp = load_font("Inter-SemiBold.ttf", 10.5 * s)
        self.f_lab = load_font("Inter-SemiBold.ttf", 13 * s)
        self.f_sig = load_font("InterDisplay-Bold.ttf", 50 * s)
        self.f_tempo = load_font("Inter-Medium.ttf", 12.5 * s)
        self.f_ghost = load_font("Inter-Medium.ttf", 15 * s)
        self.f_smufl = load_font("Bravura.otf", 28 * s)
        self.f_smufl_sm = load_font("Bravura.otf", 22 * s)
        self.f_acc = load_font("Bravura.otf", 19 * s)
        self.cap = cap_height(self.f_fret)


    def pen(self, color=None, a=1.0, stroke=None, cap=None, shader=None):
        """Paint for the drawn marks of the tab: strings, bar lines, stems, arcs, slides."""
        return mkpaint(color, a, stroke=stroke, cap=cap, shader=shader)

    def dashed(self, paint, on, off):
        paint.setPathEffect(skia.DashPathEffect.Make([on, off], 0))
        return paint

    def draw_ambient(self, c, t):
        """Moving decoration behind the tab. Return True if anything was drawn."""
        return False

    def _finish(self, out, master, lift):
        """Last touches on the premultiplied frame: slide-in and fade."""
        if lift > 0.02:
            M = np.float32([[1, 0, 0], [0, 1, lift]])
            out = cv2.warpAffine(out, M, (self.W, self.H), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
        if master < 1.0:
            out *= master
        return out

    # -- geometry -----------------------------------------------------------
    def X(self, t_song, t_now):
        return self.P + (t_song - t_now) * self.pps

    def text_w(self, font, text):
        return font.measureText(text)

    def chip_rect(self, n, x, y, grow=0.0):
        w = max(self.text_w(self.f_fret, n.label) + 11 * self.s, 21 * self.s) + grow
        h = max(22 * self.s, self.cap + 7 * self.s) + grow
        return skia.Rect.MakeXYWH(x - w / 2, y - h / 2, w, h)

    def half_w(self, n):
        return self.chip_rect(n, 0, 0).width() / 2

    def ghost_label(self, n):
        return "(%s)" % n.label

    def ghost_half_w(self, n):
        return self.f_ghost.measureText(self.ghost_label(n)) / 2 + 3 * self.s

    def dim_at(self, x):
        """Opacity of the 'already played' mask at screen x."""
        return float(np.interp(x, self.past_xp, self.past_fp))

    # -- static layers --------------------------------------------------------
    def _build_static(self):
        W, H, s = self.W, self.H, self.s
        xs = np.arange(W, dtype=np.float32) + 0.5
        # edge fade (content emerges after the string names, fades at the right)
        l0, l1 = self.lab_x + 18 * s, self.lab_x + 150 * s
        r1, r0 = self.card_r - 170 * s, self.card_r - 14 * s
        edge = np.interp(xs, [l0, l1, r1, r0], [0, 1, 1, 0])
        edge = edge * edge * (3 - 2 * edge)                      # smoothstep
        # "already played" dimming left of the playhead
        self.past_xp = [self.P - 46 * s, self.P - 4 * s]
        self.past_fp = [0.42, 1.0]
        past = np.interp(xs, self.past_xp, self.past_fp)
        self.mask_a = (edge * past / 255.0).astype(np.float32)[None, :, None]
        self.mask_b = (edge / 255.0).astype(np.float32)[None, :, None]

        # static backdrop: card + string names
        surf = skia.Surface(W, H)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        self._build_backdrop(c)
        self.draw_labels(c)
        buf = np.zeros((H, W, 4), np.uint8)
        surf.readPixels(self.info, buf)
        self.static = buf.astype(np.float32) / 255.0             # premultiplied BGRA

        self.shadow_a = 0.55 if self.variant == "card" else 0.85
        self.shadow_sigma = (1.6 if self.variant == "card" else 2.4) * s
        self.shadow_dy = max(1, int(round(1.2 * s)))

    def _build_backdrop(self, c):
        """Static backdrop behind the tab, drawn once."""
        s = self.s
        if self.variant != "card":
            return
        r = skia.Rect.MakeLTRB(self.card_l, self.card_t, self.card_r, self.card_b)
        rr = skia.RRect.MakeRectXY(r, 18 * s, 18 * s)
        c.drawRRect(rr, mkpaint((0, 0, 0), 0.30, blur=10 * s))
        fill = skia.GradientShader.MakeLinear(
            [skia.Point(0, self.card_t), skia.Point(0, self.card_b)],
            [rgba((20, 23, 30), 0.74), rgba((10, 11, 15), 0.80)], [0, 1])
        c.drawRRect(rr, mkpaint(shader=fill))
        # soft light column behind the playhead
        c.save()
        c.clipRRect(rr, skia.ClipOp.kIntersect, True)
        P, hw = self.P, 110 * s
        beam = skia.GradientShader.MakeLinear(
            [skia.Point(P - hw, 0), skia.Point(P + hw, 0)],
            [rgba(self.accent, 0), rgba(self.accent, 0.085), rgba(self.accent, 0)], [0, 0.5, 1])
        c.drawRect(skia.Rect.MakeLTRB(P - hw, self.card_t, P + hw, self.card_b), mkpaint(shader=beam))
        c.restore()
        # glass edge: brighter along the top, fading down
        inset = skia.RRect.MakeRectXY(r.makeInset(0.5 * s, 0.5 * s), 17.5 * s, 17.5 * s)
        sheen = skia.GradientShader.MakeLinear(
            [skia.Point(0, self.card_t), skia.Point(0, self.card_b)],
            [rgba((255, 255, 255), 0.22), rgba((255, 255, 255), 0.07), rgba((255, 255, 255), 0.04)],
            [0, 0.35, 1])
        c.drawRRect(inset, mkpaint(stroke=1 * s, shader=sheen))

    # -- note states -----------------------------------------------------------
    @staticmethod
    def flash(n, t):
        dt = t - n.beat.t0
        if dt < 0:
            return 0.0
        a = 1.0 if dt < 0.04 else math.exp(-(dt - 0.04) / 0.075)
        return a if a > 0.01 else 0.0

    @staticmethod
    def sustain(n, t):
        if not n.tail or t < n.beat.t0:
            return 0.0
        if t <= n.end:
            return 1.0
        return max(0.0, 1.0 - (t - n.end) / 0.18)

    # -- frame -----------------------------------------------------------------
    def render(self, t_video):
        """Return the frame at video time t_video as straight-alpha BGRA uint8."""
        t = t_video - self.preroll
        self._tv = t_video
        W, H, s = self.W, self.H, self.s

        master = ease_out(max(0.0, min(1.0, t_video / 0.7, (self.total - t_video) / 0.9)))
        lift = (1.0 - master) * 22 * s          # slides up into place, and back down at the end

        t_lo = t - (self.P + 80 * s) / self.pps
        t_hi = t + (W - self.P + 80 * s) / self.pps
        i0 = max(0, bisect.bisect_left(self.beat_t0, t_lo - 30.0))
        i1 = bisect.bisect_right(self.beat_t0, t_hi)
        vis = self.sc.beats[i0:i1]

        # layer A: the tab itself
        c = self.surf_a.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        self.draw_strings(c, t)
        self.draw_barlines(c, t, t_lo, t_hi)
        self.draw_tails(c, t, vis)
        self.draw_knockouts(c, t, vis)
        self.draw_numbers(c, t, vis)
        self.draw_legato(c, t, t_lo, t_hi)
        self.draw_slide_marks(c, t, vis)
        self.draw_techniques(c, t, t_lo, t_hi)
        self.draw_rhythm(c, t, vis)
        self.draw_header(c, t)
        # layer B: playhead and live notes
        c = self.surf_b.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        self.draw_playhead(c, t)
        self.draw_live(c, t, vis)

        c = self.surf_c.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        self.draw_hud(c, t)
        c = self.surf_d.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        self._ambient = self.draw_ambient(c, t)
        if self._ambient:
            self.surf_d.readPixels(self.info, self.buf_d)

        self.surf_a.readPixels(self.info, self.buf_a)
        self.surf_b.readPixels(self.info, self.buf_b)
        self.surf_c.readPixels(self.info, self.buf_c)
        return self.composite(master, lift)

    def composite(self, master, lift=0.0):
        A = self.buf_a.astype(np.float32)
        A *= self.mask_a
        B = self.buf_b.astype(np.float32)
        B *= self.mask_b
        sh = cv2.GaussianBlur(A[..., 3], (0, 0), self.shadow_sigma)
        dy = self.shadow_dy
        sh[dy:] = sh[:-dy].copy()
        sh[:dy] = 0
        sh *= self.shadow_a
        base = self.static
        if self._ambient:                          # lives on the backdrop only
            D = self.buf_d.astype(np.float32) * (1 / 255.0)
            D *= np.clip(self.static[..., 3:4] / 0.8, 0.0, 1.0)
            base = self.static * (1.0 - D[..., 3:4]) + D
        out = base * (1.0 - sh)[..., None]
        out[..., 3] += sh
        out *= 1.0 - A[..., 3:4]
        out += A
        out *= 1.0 - B[..., 3:4]
        out += B
        C = self.buf_c.astype(np.float32) * (1 / 255.0)
        out *= 1.0 - C[..., 3:4]
        out += C
        out = self._finish(out, master, lift)
        alpha = out[..., 3:4]
        np.divide(out[..., :3], alpha, out=out[..., :3], where=alpha > 1e-6)
        out *= 255.0
        out += 0.5
        np.clip(out, 0, 255, out=out)
        frame = out.astype(np.uint8)
        frame[frame[..., 3] == 0] = 0     # no stray colour in fully transparent pixels (avoids fringes)
        return frame

    # -- components ------------------------------------------------------------
    def draw_strings(self, c, t):
        s = self.s
        x_end = min(self.card_r, self.X(self.sc.end_t, t) - self.bar_off)
        x_beg = self.lab_x + 18 * s
        n = self.sc.nstrings
        for i, y in enumerate(self.ys):
            w = (0.95 + 0.85 * i / max(1, n - 1)) * s
            c.drawLine(x_beg, y, x_end, y, self.pen(self.ink, self.string_a, stroke=w, cap=skia.Paint.kButt_Cap))

    def draw_barlines(self, c, t, t_lo, t_hi):
        s = self.s
        bars = self.sc.bars
        for i, (b0, ln, t0, bpm, sig) in enumerate(bars):
            if not (t_lo - 2 <= t0 <= t_hi + 2):
                continue
            x = self.X(t0, t) - self.bar_off
            if i > 0:
                c.drawLine(x, self.y0, x, self.yb, self.pen(self.ink, 0.36, stroke=1.4 * s, cap=skia.Paint.kButt_Cap))
            c.drawString(str(i + 1), x + 5 * s, self.y0 - 10 * s, self.f_small, mkpaint(self.ink, 0.42))
        # final double bar
        x = self.X(self.sc.end_t, t) - self.bar_off
        if x < self.W + 20 * s:
            c.drawLine(x - 7 * s, self.y0, x - 7 * s, self.yb, self.pen(self.ink, 0.45, stroke=1.4 * s, cap=skia.Paint.kButt_Cap))
            c.drawRect(skia.Rect.MakeLTRB(x - 3 * s, self.y0 - 0.7 * s, x + 1 * s, self.yb + 0.7 * s), mkpaint(self.ink, 0.55))

    def tail_span(self, n, t):
        x = self.X(n.beat.t0, t)
        xa = x + self.half_w(n) + 4 * self.s
        xb = self.X(n.end, t) - self.bar_off - 5 * self.s
        return xa, xb

    def tail_segments(self, n, t, x_max=None):
        """Sustain line pieces, broken around the (ghost) tie numbers."""
        s = self.s
        xa, xb = self.tail_span(n, t)
        if x_max is not None:
            xb = min(xb, x_max)
        segs, cur = [], xa
        for g in n.ghosts:
            gx = self.X(g.beat.t0, t)
            hw = self.ghost_half_w(g) + 2 * s
            if gx - hw >= xb:
                break
            if gx + hw <= cur:
                continue
            if gx - hw > cur:
                segs.append((cur, gx - hw))
            cur = gx + hw
        if xb > cur:
            segs.append((cur, xb))
        return [(a, b) for a, b in segs if b - a > 3 * s]

    def draw_tails(self, c, t, vis):
        for b in vis:
            for n in b.notes:
                if not n.tail:
                    continue
                y = self.ys[n.string]
                for xa, xb in self.tail_segments(n, t):
                    c.drawLine(xa, y, xb, y, mkpaint(self.ink, 0.22, stroke=3.4 * self.s))

    def draw_knockouts(self, c, t, vis):
        s = self.s
        clear = mkpaint((0, 0, 0), 1.0, blend=skia.BlendMode.kDstOut)
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                y = self.ys[n.string]
                if n.hidden:
                    hw = self.ghost_half_w(n)
                    r = skia.Rect.MakeLTRB(x - hw, y - 6 * s, x + hw, y + 6 * s)
                else:
                    r = self.chip_rect(n, x, y)
                    r = skia.Rect.MakeLTRB(r.left() + 1.5 * s, r.top() + 3 * s,
                                           r.right() - 1.5 * s, r.bottom() - 3 * s)
                c.drawRRect(skia.RRect.MakeRectXY(r, 5 * s, 5 * s), clear)

    def draw_ghost(self, c, n, x, y, color, a):
        if a <= 0.003:
            return
        txt = self.ghost_label(n)
        w = self.f_ghost.measureText(txt)
        cap = cap_height(self.f_ghost)
        c.drawString(txt, x - w / 2, y + cap / 2, self.f_ghost, mkpaint(color, a))

    def draw_fret_text(self, c, n, x, y, color, a, font=None):
        font = font or self.f_fret
        if a <= 0.003:
            return
        if n.dead:
            d = 4.6 * self.s
            p = self.pen(color, a, stroke=2.3 * self.s)
            c.drawLine(x - d, y - d, x + d, y + d, p)
            c.drawLine(x - d, y + d, x + d, y - d, p)
            return
        w = font.measureText(n.label)
        c.drawString(n.label, x - w / 2, y + self.cap / 2, font, mkpaint(color, a))

    def draw_numbers(self, c, t, vis):
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                y = self.ys[n.string]
                if n.hidden:
                    su = self.sustain(n.root, t) if x <= self.P else 0.0
                    self.draw_ghost(c, n, x, y, self.ink, 0.55 * (1.0 - su))
                    continue
                fl, su = self.flash(n, t), self.sustain(n, t)
                self.draw_fret_text(c, n, x, y, self.ink, 0.0 if fl > 0 else 1.0 - su)

    def draw_legato(self, c, t, t_lo, t_hi):
        s = self.s
        pairs = [p for p in self.sc.legato if p[1].beat.t0 >= t_lo and p[0].beat.t0 <= t_hi]
        top_row = {}
        for (a, b, kind) in pairs:
            if kind != "slide":
                k = (id(a.beat), kind)
                top_row[k] = min(top_row.get(k, 99), a.string)
        for (a, b, kind) in pairs:
            xa, xb = self.X(a.beat.t0, t), self.X(b.beat.t0, t)
            y = self.ys[a.string]
            if kind == "slide":
                d = 0.20 * self.sp * (1 if b.fret >= a.fret else -1)
                x1 = xa + self.half_w(a) + 3 * s
                x2 = xb - self.half_w(b) - 3 * s
                c.drawLine(x1, y + d, x2, y - d, self.pen(self.ink, 0.85, stroke=1.8 * s))
                continue
            ye = y - 12.5 * s
            path = skia.Path()
            path.moveTo(xa + 1 * s, ye)
            path.cubicTo(xa + 6 * s, ye - 9 * s, xb - 6 * s, ye - 9 * s, xb - 1 * s, ye)
            c.drawPath(path, self.pen(self.ink, 0.70, stroke=1.5 * s))
            if top_row.get((id(a.beat), kind)) == a.string:
                w = self.f_hp.measureText(kind)
                c.drawString(kind, (xa + xb) / 2 - w / 2, ye - 9.5 * s, self.f_hp, mkpaint(self.ink, 0.80))

    def draw_slide_marks(self, c, t, vis):
        s = self.s
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                if n.hidden or not (n.slide & (SL_IN_BELOW | SL_IN_ABOVE | SL_OUT_DOWN | SL_OUT_UP)):
                    continue
                y = self.ys[n.string]
                hw = self.half_w(n)
                d = 0.22 * self.sp
                p = self.pen(self.ink, 0.85, stroke=1.8 * s)
                if n.slide & SL_IN_BELOW:
                    c.drawLine(x - hw - 17 * s, y + d, x - hw - 3 * s, y - d * 0.15, p)
                if n.slide & SL_IN_ABOVE:
                    c.drawLine(x - hw - 17 * s, y - d, x - hw - 3 * s, y + d * 0.15, p)
                if n.slide & SL_OUT_DOWN:
                    c.drawLine(x + hw + 3 * s, y - d * 0.15, x + hw + 17 * s, y + d, p)
                if n.slide & SL_OUT_UP:
                    c.drawLine(x + hw + 3 * s, y + d * 0.15, x + hw + 17 * s, y - d, p)

    def draw_techniques(self, c, t, t_lo, t_hi):
        s = self.s
        beats = self.sc.beats
        y_txt = self.y0 - 30 * s
        cap = cap_height(self.f_tech)
        y_line = y_txt - cap / 2
        for spans, label in ((self.sc.pm_spans, "P.M."), (self.sc.lr_spans, "let ring")):
            for (i0, i1) in spans:
                b0, b1 = beats[i0], beats[i1]
                if b1.t1 < t_lo or b0.t0 > t_hi:
                    continue
                x0 = self.X(b0.t0, t) - 8 * s
                x1 = self.X(b1.t1, t) - self.bar_off - 4 * s
                c.drawString(label, x0, y_txt, self.f_tech, mkpaint(self.ink, 0.75))
                xl = x0 + self.f_tech.measureText(label) + 6 * s
                if x1 - xl > 10 * s:
                    p = self.dashed(self.pen(self.ink, 0.45, stroke=1.3 * s, cap=skia.Paint.kButt_Cap), 5 * s, 4 * s)
                    c.drawLine(xl, y_line, x1, y_line, p)
                    p2 = self.pen(self.ink, 0.45, stroke=1.3 * s, cap=skia.Paint.kButt_Cap)
                    c.drawLine(x1, y_line - 0.65 * s, x1, y_line + 6 * s, p2)

    def draw_header(self, c, t):
        """Time signature before bar 1 and the tempo mark above it."""
        s = self.s
        x1 = self.X(0.0, t) - self.bar_off
        if x1 < -100 * s or x1 > self.W + 100 * s:
            return
        num, den = self.sc.bars[0][4]
        xs = x1 - 34 * s
        mid = (self.y0 + self.yb) / 2
        ch = cap_height(self.f_sig)
        clear = mkpaint((0, 0, 0), 1.0, blend=skia.BlendMode.kDstOut)
        for txt, yc in ((str(num), mid - 0.5 * ch - 4 * s), (str(den), mid + 0.5 * ch + 4 * s)):
            w = self.f_sig.measureText(txt)
            c.drawRect(skia.Rect.MakeLTRB(xs - w / 2 - 5 * s, yc - ch / 2 - 3 * s,
                                          xs + w / 2 + 5 * s, yc + ch / 2 + 3 * s), clear)
            c.drawString(txt, xs - w / 2, yc + ch / 2, self.f_sig, mkpaint(self.ink, 0.60))
        # tempo: quarter-note glyph = 148
        y = self.y0 - 30 * s
        note = ""
        c.drawString(note, x1 + 8 * s, y + 1 * s, self.f_smufl_sm, mkpaint(self.ink, 0.75))
        c.drawString("  = %d" % round(self.sc.bars[0][3]), x1 + 8 * s + self.f_smufl_sm.measureText(note),
                     y, self.f_tempo, mkpaint(self.ink, 0.75))

    def draw_rhythm(self, c, t, vis):
        s = self.s
        col, a = self.ink, 0.55
        stem = self.pen(col, a, stroke=1.35 * s, cap=skia.Paint.kButt_Cap)
        fill = self.pen(col, a)
        bt, gap = 3.4 * s, 2.8 * s
        sb = self.stem_bot
        vis_set = set(id(b) for b in vis)
        grouped = {}
        for g in self.sc.beam_groups:
            if id(g[0]) in vis_set or id(g[-1]) in vis_set:
                for b in g:
                    grouped[id(b)] = g

        def nbeams(b):
            return max(1, int(round(-math.log2(b.value))))  # 0.5->1, 0.25->2, 0.125->3

        drawn = set()
        for b in vis:
            x = self.X(b.t0, t)
            if b.rest:
                self.draw_rest(c, b, x, a)
                continue
            if b.value >= 4.0:
                continue
            top = self.stem_top if b.value < 2.0 else self.stem_top + 10 * s
            c.drawLine(x, top, x, sb, stem)
            if b.dots:
                c.drawCircle(x + 5.5 * s, sb - 2 * s, 1.9 * s, fill)
            g = grouped.get(id(b))
            if g is None:
                continue
            if len(g) == 1:
                glyph = {1: "", 2: "", 3: ""}[nbeams(b)]
                c.drawString(glyph, x - 0.65 * s, sb, self.f_smufl, fill)
                continue
            if id(g) in drawn:
                continue
            drawn.add(id(g))
            xs = [self.X(q.t0, t) for q in g]
            c.drawRect(skia.Rect.MakeLTRB(xs[0] - 0.65 * s, sb - bt, xs[-1] + 0.65 * s, sb), fill)
            for level in (2, 3):
                yb = sb - (bt + gap) * (level - 1)
                for k, q in enumerate(g):
                    if nbeams(q) < level:
                        continue
                    right = k + 1 < len(g) and nbeams(g[k + 1]) >= level
                    left = k > 0 and nbeams(g[k - 1]) >= level
                    if right:
                        c.drawRect(skia.Rect.MakeLTRB(xs[k] - 0.65 * s, yb - bt, xs[k + 1] + 0.65 * s, yb), fill)
                    elif not left:
                        stub = 9 * s
                        if k == len(g) - 1:
                            c.drawRect(skia.Rect.MakeLTRB(xs[k] - stub, yb - bt, xs[k] + 0.65 * s, yb), fill)
                        else:
                            c.drawRect(skia.Rect.MakeLTRB(xs[k] - 0.65 * s, yb - bt, xs[k] + stub, yb), fill)

    REST_GLYPHS = {4.0: "", 2.0: "", 1.0: "", 0.5: "",
                   0.25: "", 0.125: ""}

    def draw_rest(self, c, b, x, a):
        s = self.s
        g = self.REST_GLYPHS.get(b.value)
        if g is None:
            return
        font = self.f_smufl
        glyphs = font.textToGlyphs(g)
        bounds = font.getBounds(glyphs)[0]
        yc = (self.stem_top + self.stem_bot) / 2
        if b.value >= 4.0:   # whole-bar rest sits in the middle of the bar
            x += b.dur_b * 0.5 * self.pps * 60 / self.sc.bars[b.bar][3]
        gx = x - (bounds.left() + bounds.right()) / 2
        gy = yc - (bounds.top() + bounds.bottom()) / 2
        c.drawString(g, gx, gy, font, mkpaint(self.ink, a))
        if b.dots:
            c.drawCircle(gx + bounds.right() + 4 * s, yc - 3 * s, 1.9 * s, mkpaint(self.ink, a))

    def draw_playhead(self, c, t):
        s = self.s
        x = self.P
        # soft pulse on every beat
        bpm = self.sc.bars[0][3]
        phase = ((t * bpm / 60.0) % 1.0) * 60.0 / bpm if t >= 0 else 1.0
        pulse = math.exp(-phase / 0.12)
        top, bot = self.y0 - 20 * s, self.yb + 18 * s
        c.drawRect(skia.Rect.MakeLTRB(x - 1.5 * s, top, x + 1.5 * s, bot),
                   mkpaint(self.accent, 0.22 + 0.22 * pulse, blur=7 * s))
        c.drawLine(x, top, x, bot, mkpaint(self.accent, 0.95, stroke=2.0 * s))
        c.drawCircle(x, top, 3.2 * s, mkpaint(self.accent, 1.0))

    def draw_live(self, c, t, vis):
        s = self.s
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                y = self.ys[n.string]
                if n.hidden:
                    su = self.sustain(n.root, t)
                    if su > 0 and x <= self.P:
                        self.draw_ghost(c, n, x, y, self.accent, 0.9 * su)
                    continue
                fl, su = self.flash(n, t), self.sustain(n, t)
                if fl <= 0 and su <= 0:
                    continue
                if su > 0:
                    segs = self.tail_segments(n, t, x_max=self.P)
                    if segs:
                        g0, g1 = segs[0][0], max(segs[-1][1], segs[0][0] + 1)
                        comet = skia.GradientShader.MakeLinear(
                            [skia.Point(g0, 0), skia.Point(g1, 0)],
                            [rgba(self.accent, 0.30 * su), rgba(self.accent, 0.95 * su)], [0, 1])
                        for xa, xb in segs:
                            c.drawLine(xa, y, xb, y, mkpaint(stroke=3.4 * s, shader=comet))
                if fl <= 0:
                    self.draw_fret_text(c, n, x, y, self.accent, su)
                    continue
                pop = 1.0 + 0.14 * math.exp(-max(0.0, t - b.t0) / 0.06)
                c.save()
                c.translate(x, y)
                c.scale(pop, pop)
                c.translate(-x, -y)
                r = self.chip_rect(n, x, y)
                rr = skia.RRect.MakeRectXY(r, r.height() / 2, r.height() / 2)
                c.drawRRect(rr, mkpaint(self.accent, 0.55 * fl, blur=7 * s))
                hi = tuple(int(a + (255 - a) * 0.38) for a in self.accent)
                lo = tuple(int(a * 0.9) for a in self.accent)
                gloss = skia.GradientShader.MakeLinear(
                    [skia.Point(0, r.top()), skia.Point(0, r.bottom())],
                    [rgba(hi, fl), rgba(self.accent, fl), rgba(lo, fl)], [0, 0.55, 1])
                c.drawRRect(rr, mkpaint(shader=gloss))
                base, base_a = (self.accent, su) if su > 0 else (self.ink, self.dim_at(x))
                mix = fl * fl          # text returns to its resting colour before the chip is gone
                col = tuple(int(round(p + (q - p) * mix)) for p, q in zip(base, self.dark))
                self.draw_fret_text(c, n, x, y, col, base_a + (1.0 - base_a) * fl)
                c.restore()

    def draw_labels(self, c):
        cap = cap_height(self.f_lab)
        paint = mkpaint(self.ink, 0.6)
        for name, y in zip(self.sc.tuning, self.ys):
            letter, acc = name[:1], name[1:]
            glyph = {"#": "\uE262", "b": "\uE260"}.get(acc, "")
            w = self.f_lab.measureText(letter)
            gw, gb = 0.0, None
            if glyph:
                gb = self.f_acc.getBounds(self.f_acc.textToGlyphs(glyph))[0]
                gw = gb.width() + 1.5 * self.s
            x = self.lab_x - (w + gw) / 2
            c.drawString(letter, x, y + cap / 2, self.f_lab, paint)
            if glyph:
                gx = x + w + 1.5 * self.s - gb.left()
                gy = y - cap * 0.12 - (gb.top() + gb.bottom()) / 2
                c.drawString(glyph, gx, gy, self.f_acc, paint)

    def draw_hud(self, c, t):
        """Bar counter and a hairline showing progress through the song."""
        s = self.s
        x0, x1 = self.card_l + 22 * s, self.card_r - 22 * s
        y = self.card_b - 8 * s
        c.drawLine(x0, y, x1, y, mkpaint((255, 255, 255), 0.08, stroke=2 * s))
        frac = min(1.0, max(0.0, t / self.sc.end_t))
        if frac > 0:
            xf = x0 + (x1 - x0) * frac
            fill = skia.GradientShader.MakeLinear(
                [skia.Point(x0, 0), skia.Point(xf, 0)],
                [rgba(self.accent, 0.15), rgba(self.accent, 0.85)], [0, 1])
            c.drawLine(x0, y, xf, y, mkpaint(stroke=2 * s, shader=fill))
            c.drawCircle(xf, y, 3.2 * s, mkpaint(self.accent, 0.35, blur=2.5 * s))
            c.drawCircle(xf, y, 2.2 * s, mkpaint(self.accent, 1.0))
        nbars = len(self.sc.bars)
        cur = min(nbars, max(1, bisect.bisect_right(self.bar_t0, t)))
        yb = self.y0 - 30 * s
        xl = self.card_l + 20 * s
        c.drawString("BAR", xl, yb, self.f_small, mkpaint(self.ink, 0.35))
        xl += self.f_small.measureText("BAR") + 6 * s
        num = str(cur)
        c.drawString(num, xl, yb, self.f_tech, mkpaint(self.ink, 0.75))
        xl += self.f_tech.measureText(num) + 3 * s
        c.drawString("/ %d" % nbars, xl, yb, self.f_small, mkpaint(self.ink, 0.35))


# --------------------------------------------------------------------------
# "ink" theme: dry-brush backdrop, typewriter numbers, pen lines, film grain
# --------------------------------------------------------------------------

def _value_noise(n, cell, rng):
    """1-D value noise in [-1, 1]: random points every `cell` samples, eased between."""
    cell = max(float(cell), 1.0)
    pts = rng.uniform(-1.0, 1.0, int(n / cell) + 3)
    xs = np.arange(n) / cell
    i = xs.astype(int)
    f = (1 - np.cos((xs - i) * np.pi)) / 2
    return pts[i] * (1 - f) + pts[i + 1] * f


def _fbm(n, cells, rng):
    out, amp, tot = np.zeros(n), 1.0, 0.0
    for cell in cells:
        out += amp * _value_noise(n, cell, rng)
        tot += amp
        amp *= 0.55
    return out / tot


def _blur1d(v, sigma):
    r = max(1, int(3 * sigma))
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    return np.convolve(np.pad(v, r, mode="edge"), k / k.sum(), mode="valid")


def _unit(v):
    return v / (np.abs(v).max() + 1e-9)


def _seed(*parts):
    """Stable 32-bit seed from a few values (so each note keeps its own quirks)."""
    h = 2166136261
    for ch in repr(parts).encode():
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


def _smoothstep(v):
    v = np.clip(v, 0.0, 1.0)
    return v * v * (3 - 2 * v)


def _petal_path(cx, cy, length, width, angle, notch=0.0):
    """A teardrop petal (or leaf) growing from (cx, cy) along `angle`."""
    ca, sa = math.cos(angle), math.sin(angle)

    def P(u, v):
        return cx + u * ca - v * sa, cy + u * sa + v * ca

    p = skia.Path()
    p.moveTo(*P(0, 0))
    p.cubicTo(*P(length * 0.25, width * 0.9), *P(length * 0.8, width), *P(length, width * 0.2 if notch else 0))
    if notch:
        p.lineTo(*P(length * (1 - notch), 0))
        p.lineTo(*P(length, -width * 0.2))
    p.cubicTo(*P(length * 0.8, -width), *P(length * 0.25, -width * 0.9), *P(0, 0))
    p.close()
    return p


class InkRenderer(Renderer):
    """Hand-made look: a soft dry-brush ink stroke with faint engraved florals,
    typewriter fret numbers, pen-drawn lines, handwritten notes and rose-gold
    highlights."""

    DEFAULT_ACCENT = (228, 158, 146)        # rose gold
    STRING_SPACING = 38                     # room for bigger numbers

    def _style(self, accent):
        s = self.s
        self.ink = (248, 241, 236)          # warm cream
        self.accent = accent or self.DEFAULT_ACCENT
        # a metallic ramp around the accent: champagne highlight to deep rose
        self.accent_hi = tuple(int(round(a + (b - a) * 0.55)) for a, b in zip(self.accent, (255, 240, 232)))
        self.accent_lo = tuple(int(round(a + (b - a) * 0.45)) for a, b in zip(self.accent, (150, 78, 96)))
        self.dark = (44, 22, 30)            # plum ink for digits on a stamp
        self.string_a = 0.38 if self.variant == "card" else 0.5
        te, hand = "CourierPrime-Bold.ttf", "Caveat.ttf"
        self.f_fret = load_font(te, 28 * s)
        self.f_small = load_font(hand, 17 * s)
        self.f_tech = load_font(hand, 20 * s)
        self.f_hp = load_font(hand, 17 * s)
        self.f_lab = load_font(te, 17 * s)
        self.f_sig = load_font(te, 56 * s)
        self.f_tempo = load_font(hand, 19 * s)
        self.f_ghost = load_font(te, 17 * s)
        self.f_hud = load_font(hand, 19 * s)
        self.f_smufl = load_font("Bravura.otf", 28 * s)
        self.f_smufl_sm = load_font("Bravura.otf", 22 * s)
        self.f_acc = load_font("Bravura.otf", 19 * s)
        self.cap = cap_height(self.f_fret)
        self.rough = skia.DiscretePathEffect.Make(9 * s, 0.3 * s, 3)
        self.rough_fill = skia.DiscretePathEffect.Make(3 * s, 0.35 * s, 5)

    def pen(self, color=None, a=1.0, stroke=None, cap=None, shader=None):
        p = mkpaint(color, a, stroke=stroke, cap=cap, shader=shader)
        p.setPathEffect(self.rough)
        return p

    def dashed(self, paint, on, off):
        dash = skia.DashPathEffect.Make([on * 1.4, off, on * 0.7, off * 1.3, on, off * 0.8], 0)
        paint.setPathEffect(skia.PathEffect.MakeCompose(self.rough, dash))
        return paint

    # -- static pieces -----------------------------------------------------------
    def _build_backdrop(self, c):
        s, W, H = self.s, self.W, self.H
        rng = np.random.default_rng(23)
        self.xs_grid = np.arange(W, dtype=np.float32)
        self.wipe_jag = (60 * s * _unit(_blur1d(rng.standard_normal(H), 1.2 * s))).astype(np.float32)
        self.grain = []
        for _ in range(4):
            g = cv2.GaussianBlur(rng.standard_normal((H, W)).astype(np.float32), (0, 0), 1.2 * s)
            self.grain.append(g / g.std())

        # hand-drawn strings: a slow waver plus a faint second pass of the pen
        x_beg, x_end = self.lab_x + 18 * s, self.card_r
        n = int((x_end - x_beg) / (6 * s)) + 2
        xs = np.linspace(x_beg, x_end, n)
        self.string_paths = []
        for y in self.ys:
            dy = 0.8 * s * _fbm(n, [45, 12, 4], rng)
            dy2 = dy + 0.45 * s + 0.5 * s * _fbm(n, [30, 8], rng)
            paths = []
            for d in (dy, dy2):
                p = skia.Path()
                p.moveTo(xs[0], y + d[0])
                for xv, dv in zip(xs[1:].tolist(), d[1:].tolist()):
                    p.lineTo(xv, y + dv)
                paths.append(p)
            self.string_paths.append(paths)

        # the playhead: a marker stroke with a small arrowhead
        P, top, bot = self.P, self.y0 - 20 * s, self.yb + 18 * s
        m = int((bot - top) / (3 * s)) + 2
        yy = np.linspace(top, bot, m)
        left = P - 1.5 * s + 0.55 * s * _fbm(m, [12, 4, 2], rng)
        right = P + 1.5 * s + 0.55 * s * _fbm(m, [12, 4, 2], rng)
        ph = skia.Path()
        ph.moveTo(left[0], yy[0])
        for xv, yv in zip(left[1:].tolist(), yy[1:].tolist()):
            ph.lineTo(xv, yv)
        for xv, yv in zip(right[::-1].tolist(), yy[::-1].tolist()):
            ph.lineTo(xv, yv)
        ph.close()
        tri = skia.Path()
        tri.moveTo(P - 6 * s, top - 8 * s)
        tri.lineTo(P + 6.5 * s, top - 7 * s)
        tri.lineTo(P + 0.5 * s, top + 1.5 * s)
        tri.close()
        self.ph_path, self.ph_tri = ph, tri

        if self.variant != "card":
            return
        img = self._brush_stroke(np.random.default_rng(7))   # own stream: same stroke for every tab
        c.drawImage(skia.Image.fromarray(img, colorType=skia.kRGBA_8888_ColorType,
                                         alphaType=skia.kUnpremul_AlphaType), 0, 0)
        self._botanicals(c, random.Random(11))

    def _brush_stroke(self, rng):
        """One wide stroke of ink: firm where the brush lands on the left,
        dry and streaky where it lifts off on the right."""
        s, W, H = self.s, self.W, self.H
        y = np.arange(H, dtype=np.float32)[:, None]
        x = np.arange(W, dtype=np.float32)[None, :]
        swell = 3 * s * _fbm(W, [700 * s, 260 * s], rng)            # the stroke thickens and thins
        top = self.card_t + 7 * s - swell + 1.6 * s * _fbm(W, [120 * s, 35 * s, 10 * s], rng)
        bot = self.card_b - 7 * s + swell + 1.6 * s * _fbm(W, [110 * s, 32 * s, 9 * s], rng)
        cov_y = (np.clip((y - top[None, :]) / (1.3 * s) + 0.5, 0, 1)
                 * np.clip((bot[None, :] - y) / (1.3 * s) + 0.5, 0, 1))

        bl = _unit(_blur1d(rng.standard_normal(H), 2.2 * s))      # bristles come in clumps,
        br = _unit(_blur1d(rng.standard_normal(H), 2.2 * s))      # not single-pixel slivers
        yn = (np.arange(H) - (self.card_t + self.card_b) / 2) / (self.card_b - self.card_t)
        start = (self.card_l + 4 * s + 10 * s * yn + 6 * s * (yn * 2) ** 2      # the brush lands at a slant
                 + 8 * s * (0.5 + 0.5 * _fbm(H, [70 * s, 20 * s], rng))
                 + 7 * s * np.maximum(0, bl) ** 1.4)
        end = (self.card_r - 6 * s - 26 * s * (0.5 + 0.5 * _fbm(H, [70 * s, 20 * s], rng))
               - 80 * s * np.maximum(0, br) ** 1.6)
        ramp_l = _blur1d((8 + 16 * rng.random(H)) * s, 2.0 * s)
        ramp_r = _blur1d((60 + 140 * rng.random(H)) * s, 2.0 * s)
        cov_x = (np.clip((x - start[:, None]) / ramp_l[:, None], 0, 1) ** 0.55
                 * np.clip((end[:, None] - x) / ramp_r[:, None], 0, 1) ** 0.7)

        # long bristle marks running along the stroke
        small = rng.standard_normal((H, max(4, int(W / (60 * s))))).astype(np.float32)
        streak = cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC)
        k = 2 * int(3 * 3.5 * s) + 1
        streak = cv2.GaussianBlur(streak, (1, k), sigmaX=0, sigmaY=3.5 * s)
        streak /= streak.std() + 1e-9

        dist = np.minimum(y - top[None, :], bot[None, :] - y)
        near = np.exp(-np.maximum(dist, 0) / (6 * s))
        dry = 1 - 0.3 * near * np.clip(streak, 0, None) / 2.5          # slightly dry edges
        body = 0.86
        alpha = np.clip(cov_x * cov_y * body * dry, 0, 0.93)
        # ink wicking a few pixels past the edge, very faint
        wick_t = top - (2 + 4 * (0.5 + 0.5 * _fbm(W, [60 * s, 14 * s], rng))) * s
        wick_b = bot + (2 + 4 * (0.5 + 0.5 * _fbm(W, [60 * s, 14 * s], rng))) * s
        wick = (np.clip((y - wick_t[None, :]) / (2 * s), 0, 1) * np.clip((wick_b[None, :] - y) / (2 * s), 0, 1)
                * cov_x * 0.14 * (0.7 + 0.3 * np.clip(streak, -1, 1)))
        alpha = np.maximum(alpha, wick)
        pool = 5 * _fbm(W, [400 * s, 130 * s], rng)[None, :]         # ink pooling: gentle tone shifts
        rgb = np.stack([22 + 4 * streak + pool, 16 + 3.2 * streak + pool * 0.8, 20 + 3.8 * streak + pool], -1)
        out = np.dstack([np.clip(rgb, 0, 255), alpha[..., None] * 255])
        return np.ascontiguousarray(out.round().astype(np.uint8))


    # -- faint botanical engraving on the ink stroke --------------------------------
    ROSE_GOLD, BLUSH, PEARL = (228, 158, 146), (244, 192, 198), (228, 224, 230)
    FLORAL_STRENGTH = 0.35                  # overall opacity of the floral layer

    def _botanicals(self, c, rng):
        """Flowering sprays drawn like a fine engraving, laid onto the ink:
        full strength in the margins, softer behind the strings."""
        s, W, H = self.s, self.W, self.H
        surf = skia.Surface(W, H)
        art = surf.getCanvas()
        art.clear(skia.ColorTRANSPARENT)
        x, from_top = self.card_l + 10 * s, False
        while x < self.card_r:
            if from_top:
                oy, heading = self.card_t - 8 * s, math.pi / 2 - rng.uniform(0.4, 0.9)
            else:
                oy, heading = self.card_b + 8 * s, -math.pi / 2 + rng.uniform(0.4, 0.9)
            self._spray(art, rng, x, oy, heading, rng.uniform(320, 440) * s)
            x += rng.uniform(190, 270) * s
            from_top = not from_top
        # a finer layer of sprigs, buds and tendrils in between
        x, from_top = self.card_l + 110 * s, True
        while x < self.card_r:
            if from_top:
                oy, heading = self.card_t - 4 * s, math.pi / 2 - rng.uniform(0.3, 1.0)
            else:
                oy, heading = self.card_b + 4 * s, -math.pi / 2 + rng.uniform(0.3, 1.0)
            self._spray(art, rng, x, oy, heading, rng.uniform(130, 220) * s, small=True)
            x += rng.uniform(150, 230) * s
            from_top = not from_top
        buf = np.zeros((H, W, 4), np.uint8)
        surf.readPixels(self.info, buf)
        ys = np.arange(H, dtype=np.float32)
        wy = np.interp(ys, [self.y0 - 22 * s, self.y0 - 4 * s, self.yb + 4 * s, self.yb + 22 * s],
                       [1.0, 0.38, 0.38, 1.0])
        wx = np.interp(np.arange(W, dtype=np.float32), [self.lab_x + 10 * s, self.lab_x + 80 * s], [0.3, 1.0])
        layer = buf.astype(np.float32) * (self.FLORAL_STRENGTH * wy[:, None] * wx[None, :])[..., None]
        img = skia.Image.fromarray(np.ascontiguousarray(layer.round().clip(0, 255).astype(np.uint8)),
                                   colorType=skia.kBGRA_8888_ColorType, alphaType=skia.kPremul_AlphaType)
        c.drawImage(img, 0, 0, skia.SamplingOptions(), mkpaint(blend=skia.BlendMode.kSrcATop))

    def _walk(self, x, y, heading, length, rng, curl=0):
        """Points (x, y, heading) along a gently swaying stem; with `curl` it
        winds into a tendril at the end."""
        s = self.s
        ds = 2.5 * s
        n = max(4, int(length / ds))
        k0 = rng.uniform(-0.0015, 0.0015) / s
        amp = rng.uniform(0.004, 0.008) / s
        f, ph = rng.uniform(0.6, 1.4), rng.uniform(0, 2 * math.pi)
        pts = []
        for i in range(n):
            u = i / (n - 1)
            k = k0 + amp * math.sin(2 * math.pi * f * u + ph)
            if curl and u > 0.72:
                k += curl * ((u - 0.72) / 0.28) ** 2 * 0.1 / s
            heading += k * ds
            pts.append((x, y, heading))
            x += math.cos(heading) * ds
            y += math.sin(heading) * ds
        return pts

    def _stem(self, c, pts, w0, w1, color, a):
        """A tapering stem as a filled ribbon (reads like an engraved line)."""
        left, right, n = [], [], len(pts)
        for i, (x, y, h) in enumerate(pts):
            w = (w0 + (w1 - w0) * i / (n - 1)) / 2
            nx, ny = -math.sin(h), math.cos(h)
            left.append((x + nx * w, y + ny * w))
            right.append((x - nx * w, y - ny * w))
        path = skia.Path()
        path.moveTo(*left[0])
        for q in left[1:] + right[::-1]:
            path.lineTo(*q)
        path.close()
        c.drawPath(path, mkpaint(color, a))

    def _spray(self, c, rng, x, y, heading, length, small=False):
        s = self.s
        end = rng.choice(("bud", "curl")) if small else rng.choice(("bloom", "bloom", "curl"))
        pts = self._walk(x, y, heading, length, rng, curl=rng.choice((-1, 1)) if end == "curl" else 0)
        if small:
            self._stem(c, pts, 1.1 * s, 0.35 * s, self.ROSE_GOLD, 0.26)
            self._leaves_along(c, rng, pts, (8, 14), (0.15, 0.85), (15, 24))
            for _ in range(rng.randint(1, 2)):
                i = int(len(pts) * rng.uniform(0.3, 0.8))
                bx, by, bh = pts[i]
                side = rng.choice((-1, 1))
                bpts = self._walk(bx, by, bh + side * rng.uniform(0.5, 0.9), rng.uniform(35, 70) * s, rng, curl=-side)
                self._stem(c, bpts, 0.8 * s, 0.3 * s, self.ROSE_GOLD, 0.24)
                self._leaves_along(c, rng, bpts, (6, 10), (0.25, 0.6), (14, 20))
            ex, ey, eh = pts[-1]
            if end == "bud":
                self._bud(c, rng, ex, ey, rng.uniform(5, 7) * s, eh)
            return
        self._stem(c, pts, 1.8 * s, 0.5 * s, self.ROSE_GOLD, 0.30)
        self._leaves_along(c, rng, pts, (15, 26), (0.1, 0.9), (22, 36))
        for _ in range(rng.randint(2, 4)):
            i = int(len(pts) * rng.uniform(0.25, 0.85))
            bx, by, bh = pts[i]
            side = rng.choice((-1, 1))
            kind = rng.choice(("bloom", "bud", "bud", "curl", "curl"))
            bpts = self._walk(bx, by, bh + side * rng.uniform(0.5, 0.95), rng.uniform(55, 140) * s, rng,
                              curl=-side if kind == "curl" else 0)
            self._stem(c, bpts, 1.1 * s, 0.35 * s, self.ROSE_GOLD, 0.28)
            self._leaves_along(c, rng, bpts, (9, 16), (0.2, 0.8), (16, 26))
            ex, ey, eh = bpts[-1]
            if kind == "bloom":
                self._bloom(c, rng, ex, ey, rng.uniform(18, 26) * s, eh)
            elif kind == "bud":
                self._bud(c, rng, ex, ey, rng.uniform(6, 9) * s, eh)
        ex, ey, eh = pts[-1]
        if end == "bloom":
            self._bloom(c, rng, ex, ey, rng.uniform(30, 42) * s, eh)

    def _leaves_along(self, c, rng, pts, size, span, every):
        s, n, ds = self.s, len(pts), 2.5 * self.s
        i, side = int(n * span[0]), rng.choice((-1, 1))
        while i < n * span[1]:
            x, y, h = pts[i]
            L = rng.uniform(*size) * s
            self._leaf(c, x, y, h + side * rng.uniform(0.55, 1.0), L, L * rng.uniform(0.22, 0.3),
                       side * rng.uniform(0.1, 0.3))
            side = -side
            i += max(1, int(rng.uniform(*every) * s / ds))

    def _leaf(self, c, x, y, ang, L, width, bend):
        """A slender, gently curved leaf with a midrib and fine veins."""
        s = self.s
        ca, sa = math.cos(ang), math.sin(ang)

        def P(u, v):
            v += bend * L * u * u * 0.6
            return x + u * L * ca - v * sa, y + u * L * sa + v * ca

        def hw(u):
            return width / 2 * math.sin(math.pi * u) ** 0.7 * (1 - 0.3 * u)

        us = [q / 16 for q in range(17)]
        outline = skia.Path()
        outline.moveTo(*P(0, 0))
        for u in us[1:]:
            outline.lineTo(*P(u, hw(u)))
        for u in reversed(us[:-1]):
            outline.lineTo(*P(u, -hw(u)))
        outline.close()
        c.drawPath(outline, mkpaint(self.PEARL, 0.05))
        c.drawPath(outline, mkpaint(self.PEARL, 0.24, stroke=0.7 * s))
        rib = skia.Path()
        rib.moveTo(*P(0, 0))
        for u in us[1:15]:
            rib.lineTo(*P(u, 0))
        c.drawPath(rib, mkpaint(self.PEARL, 0.16, stroke=0.5 * s))
        vein = mkpaint(self.PEARL, 0.11, stroke=0.4 * s)
        for k in range(1, 6):
            u = k / 6.5
            for sgn in (1, -1):
                c.drawLine(*P(u, 0), *P(u + 0.1, sgn * hw(u + 0.1) * 0.85), vein)

    def _bloom(self, c, rng, cx, cy, R, heading):
        """A layered bloom seen at a slight angle: rounded petals with clear
        notches between them, light engraved hatching and a spiral cup."""
        s = self.s
        c.save()
        c.translate(cx, cy)
        c.rotate(math.degrees(heading + math.pi / 2 + rng.uniform(-0.4, 0.4)))
        c.scale(1.0, rng.uniform(0.68, 0.84))
        line = mkpaint(self.BLUSH, 0.30, stroke=0.8 * s)
        fill = mkpaint(self.BLUSH, 0.05)
        hatch = mkpaint(self.ROSE_GOLD, 0.15, stroke=0.5 * s)
        n = rng.randint(5, 7)
        base = rng.uniform(0, 2 * math.pi)
        layers = ((0.22, 1.0, n, 0.0, 1.05), (0.14, 0.64, n - 1, 0.5, 1.08), (0.05, 0.36, 4, 0.25, 1.1))
        for layer, (rin, rout, cnt, off, spread) in enumerate(layers):
            for k in range(cnt):
                a = base + (k + off) * 2 * math.pi / cnt + rng.uniform(-0.1, 0.1)
                span = 2 * math.pi / cnt * spread
                petal = skia.Path()
                petal.moveTo(R * rin * math.cos(a), R * rin * math.sin(a))
                for q in range(29):
                    u = q / 28
                    phi = a - span / 2 + u * span
                    # rounded lobe: full at the middle, pinched at the sides (the notches)
                    rr = R * rout * (0.58 + 0.42 * math.sin(math.pi * u) ** 0.7)
                    rr += R * rout * 0.025 * math.sin(6 * math.pi * u)          # a soft ruffle
                    petal.lineTo(rr * math.cos(phi), rr * math.sin(phi))
                petal.close()
                c.drawPath(petal, fill)
                c.drawPath(petal, line)
                if layer == 0:
                    for m in range(3):
                        phi = a - span * 0.18 + m * span * 0.18
                        r0 = R * (rin + 0.3 * (rout - rin))
                        r1 = R * (rin + rng.uniform(0.62, 0.78) * (rout - rin))
                        stroke = skia.Path()
                        stroke.moveTo(r0 * math.cos(phi), r0 * math.sin(phi))
                        rm = (r0 + r1) / 2
                        stroke.quadTo(rm * math.cos(phi + 0.06), rm * math.sin(phi + 0.06),
                                      r1 * math.cos(phi + 0.03), r1 * math.sin(phi + 0.03))
                        c.drawPath(stroke, hatch)
        cup = skia.Path()                            # the heart of the flower
        for q in range(49):
            u = q / 48
            th = base + u * 1.8 * 2 * math.pi
            rr = R * (0.03 + 0.11 * u)
            if q == 0:
                cup.moveTo(rr * math.cos(th), rr * math.sin(th))
            else:
                cup.lineTo(rr * math.cos(th), rr * math.sin(th))
        c.drawPath(cup, mkpaint(self.ROSE_GOLD, 0.30, stroke=0.6 * s))
        c.restore()

    def _bud(self, c, rng, x, y, r, heading):
        """A closed bud with a few engraved lines, held by two sepals."""
        s = self.s
        body = _petal_path(x, y, r * 1.9, r * 0.55, heading)
        c.drawPath(body, mkpaint(self.BLUSH, 0.06))
        c.drawPath(body, mkpaint(self.BLUSH, 0.30, stroke=0.8 * s))
        ca, sa = math.cos(heading), math.sin(heading)
        hatch = mkpaint(self.ROSE_GOLD, 0.16, stroke=0.5 * s)
        for v in (-0.25, 0.0, 0.25):
            ax, ay = x + 0.35 * r * ca - v * r * sa, y + 0.35 * r * sa + v * r * ca
            bx, by = x + 1.55 * r * ca - v * 0.4 * r * sa, y + 1.55 * r * sa + v * 0.4 * r * ca
            c.drawLine(ax, ay, bx, by, hatch)
        for side in (-1, 1):
            self._leaf(c, x, y, heading + side * 0.55, r * 1.3, r * 0.35, -side * 0.2)

    def draw_hud(self, c, t):
        """No bar counter or progress line in this theme."""
        return

    # -- per-frame finish: film grain and a brush-wipe in/out ---------------------
    def _finish(self, out, master, lift):
        s, tv = self.s, self._tv
        g = self.grain[int(tv * 24) % len(self.grain)]
        out[..., :3] += (0.007 * g)[..., None] * out[..., 3:4]
        k_in, k_out = min(1.0, tv / 1.0), min(1.0, (self.total - tv) / 1.0)
        if k_in < 1.0 or k_out < 1.0:
            soft, travel = 70 * s, self.W + 380 * s
            xs, jag = self.xs_grid[None, :], self.wipe_jag[:, None]
            m = np.ones((self.H, self.W), np.float32)
            if k_in < 1.0:
                edge = -190 * s + (0.5 - 0.5 * math.cos(math.pi * k_in)) * travel
                m *= _smoothstep((edge + jag - xs) / soft)
            if k_out < 1.0:
                edge = -190 * s + (0.5 - 0.5 * math.cos(math.pi * (1 - k_out))) * travel
                m *= _smoothstep((xs - edge + jag) / soft)
            out *= m[..., None]
        return out

    # -- hand-made marks --------------------------------------------------------
    def draw_strings(self, c, t):
        s = self.s
        x_end = min(self.card_r, self.X(self.sc.end_t, t) - self.bar_off)
        c.save()
        c.clipRect(skia.Rect.MakeLTRB(0, 0, x_end, self.H))
        n = self.sc.nstrings
        for i, (p1, p2) in enumerate(self.string_paths):
            w = (0.9 + 0.9 * i / max(1, n - 1)) * s
            c.drawPath(p1, mkpaint(self.ink, self.string_a, stroke=w, cap=skia.Paint.kButt_Cap))
            c.drawPath(p2, mkpaint(self.ink, self.string_a * 0.35, stroke=w * 0.6, cap=skia.Paint.kButt_Cap))
        c.restore()

    def draw_fret_text(self, c, n, x, y, color, a, font=None):
        """Typewriter numbers, each struck a little off-square."""
        if a <= 0.003:
            return
        rng = random.Random(_seed(n.beat.t0, n.string))
        c.save()
        c.rotate(rng.uniform(-0.8, 0.8), x, y)
        c.translate(0, rng.uniform(-0.3, 0.3) * self.s)
        super().draw_fret_text(c, n, x, y, color, a, font)
        c.restore()

    def bristles(self, n):
        rng = random.Random(_seed(n.beat.t0, n.string, "brush"))
        s = self.s
        return [(rng.uniform(-1.8, 1.8) * s, rng.uniform(0.8, 1.5) * s,
                 rng.uniform(0.45, 1.0), rng.uniform(0.7, 1.0)) for _ in range(4)]

    def draw_brush(self, c, n, segs, color=None, a=1.0, shader=None):
        """A sustain as a few dry-brush bristle lines of uneven length."""
        y = self.ys[n.string]
        g0, g1 = segs[0][0], segs[-1][1]
        for dy, w, al, frac in self.bristles(n):
            xe = g0 + (g1 - g0) * frac
            p = self.pen(color, a * al, stroke=w, shader=shader)
            if shader is not None:
                p.setAlphaf(a * al)
            for xa, xb in segs:
                xb = min(xb, xe)
                if xb - xa > 1:
                    c.drawLine(xa, y + dy, xb, y + dy, p)

    def draw_tails(self, c, t, vis):
        for b in vis:
            for n in b.notes:
                if n.tail:
                    segs = self.tail_segments(n, t)
                    if segs:
                        self.draw_brush(c, n, segs, self.ink, 0.30)

    def draw_live(self, c, t, vis):
        s = self.s
        clear = mkpaint((0, 0, 0), 1.0, blend=skia.BlendMode.kDstOut)
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                hw = self.ghost_half_w(n) if n.hidden else self.half_w(n) - 2 * s
                if abs(x - self.P) < hw + 3 * s:
                    y = self.ys[n.string]
                    r = skia.Rect.MakeLTRB(x - hw, y - 8 * s, x + hw, y + 8 * s)
                    c.drawRRect(skia.RRect.MakeRectXY(r, 4 * s, 4 * s), clear)
        for b in vis:
            x = self.X(b.t0, t)
            for n in b.notes:
                y = self.ys[n.string]
                if n.hidden:
                    su = self.sustain(n.root, t)
                    if su > 0 and x <= self.P:
                        self.draw_ghost(c, n, x, y, self.accent, 0.9 * su)
                    continue
                fl, su = self.flash(n, t), self.sustain(n, t)
                if fl <= 0 and su <= 0:
                    continue
                if su > 0:
                    segs = self.tail_segments(n, t)
                    if segs and segs[0][0] < self.P:
                        comet = skia.GradientShader.MakeLinear(
                            [skia.Point(segs[0][0], 0), skia.Point(max(self.P, segs[0][0] + 1), 0)],
                            [rgba(self.accent, 0.35 * su), rgba(self.accent, 0.95 * su)], [0, 1])
                        c.save()
                        c.clipRect(skia.Rect.MakeLTRB(0, 0, self.P, self.H))
                        self.draw_brush(c, n, segs, shader=comet)
                        c.restore()
                if fl <= 0:
                    self.draw_fret_text(c, n, x, y, self.accent, su)
                    continue
                # the hit: the number itself glows rose gold for a moment; nothing covers it
                base, base_a = (self.accent, su) if su > 0 else (self.ink, self.dim_at(x))
                col = tuple(int(round(p + (q - p) * fl)) for p, q in zip(base, self.accent_hi))
                self.draw_fret_text(c, n, x, y, col, base_a + (1.0 - base_a) * fl)

    def draw_playhead(self, c, t):
        s = self.s
        c.drawPath(self.ph_path, mkpaint(self.accent_hi, 0.30, blur=4.5 * s))
        b = self.ph_path.getBounds()
        metal = skia.GradientShader.MakeLinear(
            [skia.Point(0, b.top()), skia.Point(0, b.bottom())],
            [rgba(self.accent_hi, 0.98), rgba(self.accent, 0.95), rgba(self.accent_lo, 0.95)], [0, 0.45, 1])
        for path in (self.ph_path, self.ph_tri):
            p = mkpaint(shader=metal)
            p.setPathEffect(self.rough_fill)
            c.drawPath(path, p)



THEMES = {"studio": Renderer, "ink": InkRenderer}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def to_rgba(bgra):
    return bgra[..., [2, 1, 0, 3]]


def over(bg, fg_rgba, x, y):
    """Composite straight-alpha RGBA onto an RGB background (in place)."""
    h, w = fg_rgba.shape[:2]
    a = fg_rgba[..., 3:4].astype(np.float32) / 255.0
    region = bg[y:y + h, x:x + w].astype(np.float32)
    bg[y:y + h, x:x + w] = (fg_rgba[..., :3] * a + region * (1 - a)).round().astype(np.uint8)
    return bg


_worker = None


def make_renderer(sc, kw):
    kw = dict(kw)
    return THEMES[kw.pop("theme")](sc, **kw)


def _init_worker(gp, track, drop, tuning, kw):
    global _worker
    _worker = make_renderer(load_score(gp, track, drop, tuning), kw)


def _render_frame(k):
    return _worker.render(k / _worker.fps).tobytes()


def ffmpeg_cmd(r, args, fmts):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{r.W}x{r.H}", "-r", str(args.fps), "-i", "-"]
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if "preview" in fmts:
        cmd += ["-loop", "1", "-framerate", str(args.fps), "-i", args.bg]
    fc = [f"[0:v]split={len(fmts)}" + "".join(f"[v{i}]" for i in range(len(fmts)))]
    maps = []
    for i, f in enumerate(fmts):
        if f == "prores":
            fc.append(f"[v{i}]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p10le[o{i}]")
            maps += ["-map", f"[o{i}]", "-c:v", "prores_ks", "-profile:v", "4444", "-alpha_bits", "8",
                     "-vendor", "apl0", *tags, f"{args.out}_prores4444.mov"]
        elif f == "webm":
            fc.append(f"[v{i}]scale=out_color_matrix=bt709:out_range=tv,format=yuva420p[o{i}]")
            maps += ["-map", f"[o{i}]", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "22", "-row-mt", "1",
                     "-tile-columns", "2", "-deadline", "good", "-cpu-used", "4", "-auto-alt-ref", "0", *tags,
                     "-metadata:s:v:0", "alpha_mode=1", f"{args.out}_vp9alpha.webm"]
        elif f == "preview":
            fc.append(f"[1:v]format=rgb24[bg];[bg][v{i}]overlay=x=(W-w)/2:y=H-h-{int(40 * args.scale)}"
                      f":shortest=1:format=auto,scale=out_color_matrix=bt709:out_range=tv,format=yuv420p[o{i}]")
            maps += ["-map", f"[o{i}]", "-c:v", "libx264", "-preset", "medium", "-crf", "17", *tags,
                     "-movflags", "+faststart", f"{args.out}_preview.mp4"]
        else:
            raise SystemExit(f"unknown format {f}")
    return cmd + ["-filter_complex", ";".join(fc)] + maps


def parse_tuning(text):
    """'A-E-B-E-G#-B', 'A E B E G♯ B' or 'A,E,B,...' -> ['A', 'E', 'B', 'E', 'G#', 'B']."""
    import re
    text = text.replace("♯", "#").replace("♭", "b")
    names = [n for n in re.split(r"[\s,\-–—/]+", text) if n]
    for n in names:
        if not re.fullmatch(r"[A-Ga-g][#b]?", n):
            raise SystemExit(f"--tuning: can't read note name {n!r}")
    return [n[0].upper() + n[1:] for n in names]


def list_tracks(path):
    """Print every track with its tuning and how many notes sit on each string."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("Content/score.gpif"))
    notes = {e.get("id"): e for e in root.find("Notes")}
    beats = {e.get("id"): e for e in root.find("Beats")}
    voices = {e.get("id"): e for e in root.find("Voices")}
    bars = {e.get("id"): e for e in root.find("Bars")}
    for ti, tr in enumerate(root.find("Tracks")):
        pitches = tr.find(".//Property[@name='Tuning']/Pitches")
        tuning = [int(p) for p in pitches.text.split()] if pitches is not None else []
        used = {}
        for mb in root.find("MasterBars"):
            bar = bars[mb.findtext("Bars").split()[ti]]
            for vid in bar.findtext("Voices").split():
                if vid == "-1":
                    continue
                for bid in voices[vid].findtext("Beats").split():
                    for nid in (beats[bid].findtext("Notes") or "").split():
                        s = notes[nid].find("Properties/Property[@name='String']/String")
                        if s is not None:
                            used[int(s.text)] = used.get(int(s.text), 0) + 1
        names = " ".join(PITCH_NAMES[p % 12] for p in tuning)
        counts = ", ".join(f"{i}:{used.get(i, 0)}" for i in range(len(tuning)))
        print(f"track {ti}: {tr.findtext('Name').strip()!r}  {len(tuning)} strings, low->high {names}  "
              f"notes per string [{counts}]")


def default_background(r, args):
    """Plain dark gradient used for previews when --bg is not given."""
    from PIL import Image
    w, h = int(1920 * args.scale), int(1080 * args.scale)
    y = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    top, bot = np.array([46, 52, 64], np.float32), np.array([12, 13, 17], np.float32)
    img = np.broadcast_to(top * (1 - y) + bot * y, (h, w, 3)).astype(np.uint8)
    path = f"{args.out}_preview_bg.png"
    Image.fromarray(img).save(path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gp")
    ap.add_argument("--track", type=int, default=0)
    ap.add_argument("--drop-strings", default="",
                    help="comma list of strings to hide, counted from the LOWEST string starting at 0 "
                         "(e.g. 0 hides the low B of a 7-string)")
    ap.add_argument("--list-tracks", action="store_true", help="print the tracks in the file and exit")
    ap.add_argument("--tuning", help='string names to display, low to high, e.g. "A E B E G# B" '
                                     "(labels only; fret numbers are unchanged)")
    ap.add_argument("--scale", type=float, default=1.0, help="1 = 1920 wide, 2 = 3840 wide")
    ap.add_argument("--theme", choices=sorted(THEMES), default="studio",
                    help="studio = clean glass card, ink = rugged dry-brush / typewriter look")
    ap.add_argument("--variant", choices=["card", "clean"], default="card",
                    help="card = with backdrop, clean = tab only")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--preroll", type=float, default=3.0)
    ap.add_argument("--px-per-beat", type=float, default=200.0, help="scroll speed (at 1920 wide)")
    ap.add_argument("--playhead", type=float, default=0.22, help="playhead position, fraction of width")
    ap.add_argument("--accent", help="highlight colour, hex RGB (default depends on the theme)")
    ap.add_argument("--still", type=float, action="append", help="render PNG stills at these video times")
    ap.add_argument("--out", help="output basename")
    ap.add_argument("--formats", default="prores,webm", help="comma list of prores,webm,preview")
    ap.add_argument("--bg", help="background image for previews / composited stills")
    ap.add_argument("--start", type=float, default=0.0, help="start rendering at this video time")
    ap.add_argument("--limit", type=float, help="render only N seconds")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()

    if args.list_tracks:
        list_tracks(args.gp)
        return
    drop = tuple(int(v) for v in args.drop_strings.split(",") if v.strip())
    tuning = parse_tuning(args.tuning) if args.tuning else None
    accent = tuple(int(args.accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if args.accent else None
    kw = dict(theme=args.theme, scale=args.scale, variant=args.variant, fps=args.fps,
              preroll=args.preroll, px_per_beat=args.px_per_beat, playhead=args.playhead, accent=accent)
    sc = load_score(args.gp, args.track, drop, tuning)
    r = make_renderer(sc, kw)
    print(f"{sc.track_name}: {len(sc.bars)} bars, {len(sc.beats)} beats, strings {' '.join(sc.tuning)}, "
          f"dropped {sc.dropped} notes; frame {r.W}x{r.H}, {r.total:.3f}s, {r.nframes} frames; "
          f"bar 1 downbeat at {args.preroll:.3f}s", file=sys.stderr)

    if args.still:
        from PIL import Image
        base = args.out or "still"
        for tv in args.still:
            img = to_rgba(r.render(tv))
            Image.fromarray(img, "RGBA").save(f"{base}_{tv:07.3f}.png")
            if args.bg:
                bg = np.array(Image.open(args.bg).convert("RGB"))
                y = bg.shape[0] - r.H - int(40 * args.scale)
                x = (bg.shape[1] - r.W) // 2
                Image.fromarray(over(bg.copy(), img, x, y)).save(f"{base}_{tv:07.3f}_comp.png")
        return

    if not args.out:
        ap.error("--out or --still required")
    fmts = [f.strip() for f in args.formats.split(",") if f.strip()]
    if "preview" in fmts and not args.bg:
        args.bg = default_background(r, args)
    first = int(round(args.start * args.fps))
    nframes = r.nframes - first if not args.limit else int(args.limit * args.fps)
    proc = subprocess.Popen(ffmpeg_cmd(r, args, fmts), stdin=subprocess.PIPE)
    import multiprocessing as mp
    import time
    t_start, last = time.time(), 0.0
    with mp.Pool(args.workers, initializer=_init_worker, initargs=(args.gp, args.track, drop, tuning, kw)) as pool:
        for k, frame in enumerate(pool.imap(_render_frame, range(first, first + nframes), chunksize=4)):
            proc.stdin.write(frame)
            now = time.time()
            if now - last > 5 or k == nframes - 1:
                last = now
                print(f"  frame {k + 1}/{nframes}  ({(k + 1) / (now - t_start):.1f} fps)", file=sys.stderr)
    proc.stdin.close()
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
