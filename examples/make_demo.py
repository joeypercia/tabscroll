"""Build examples/demo.gp: a short original riff in a minimal GPIF container.

It holds only the parts tabscroll reads, so it demonstrates the renderer but is
not a complete Guitar Pro document.
"""
import os
import zipfile

VALUES = {1: "Quarter", 0.5: "Eighth", 0.25: "16th", 2: "Half", 4: "Whole"}
TUNING = [40, 45, 50, 55, 59, 64]           # standard E, low -> high

# each beat: (duration in quarters, [(string, fret, {flags})])  string 0 = low E
PM = {"pm": True}
bars = [
    [(0.25, [(0, 0, PM)])] * 4 + [(0.5, [(0, 3), (1, 5)]), (0.5, [(0, 5), (1, 7)])]
    + [(0.25, [(0, 0, PM)])] * 4 + [(0.5, [(0, 7, {"slide": 2}), (1, 9, {"slide": 2})]), (0.5, [(0, 10), (1, 12)])],
    [(0.5, [(2, 7, {"lr": True})]), (0.5, [(3, 9, {"lr": True})]), (0.5, [(4, 8, {"lr": True})]),
     (0.5, [(5, 7, {"lr": True})]), (0.5, [(4, 8, {"lr": True})]), (0.5, [(3, 7, {"ho": True, "lr": True})]),
     (1, [(3, 9, {"hd": True, "lr": True})])],
    [(0.25, [(0, 0, PM)])] * 2 + [(0.5, [(0, 0, PM)])] + [(0.25, [(0, 0, PM)])] * 2 + [(0.5, [(0, 0, PM)])]
    + [(0.5, [(0, 1, {"slide": 16}), (1, 3)])] + [(0.5, [])] + [(0.5, [(0, 3), (1, 5), (2, 5)])] + [(0.5, [(0, 5, {"to": True}), (1, 7, {"to": True}), (2, 7, {"to": True})])],
    [(4, [(0, 5, {"td": True}), (1, 7, {"td": True}), (2, 7, {"td": True})])],
]

rhythms, beats, notes, voices, bar_xml, master = {}, [], [], [], [], []
for bi, bar in enumerate(bars):
    ids = []
    for dur, ns in bar:
        rid = rhythms.setdefault(dur, len(rhythms))
        nids = []
        for n in ns:
            s, f = n[0], n[1]
            fl = n[2] if len(n) > 2 else {}
            props = f"<Property name='String'><String>{s}</String></Property><Property name='Fret'><Fret>{f}</Fret></Property>"
            if fl.get("pm"):
                props += "<Property name='PalmMuted'><Enable/></Property>"
            if fl.get("ho"):
                props += "<Property name='HopoOrigin'><Enable/></Property>"
            if fl.get("hd"):
                props += "<Property name='HopoDestination'><Enable/></Property>"
            if fl.get("slide"):
                props += f"<Property name='Slide'><Flags>{fl['slide']}</Flags></Property>"
            extra = "<LetRing/>" if fl.get("lr") else ""
            if fl.get("to") or fl.get("td"):
                extra += f"<Tie origin='{str(bool(fl.get('to'))).lower()}' destination='{str(bool(fl.get('td'))).lower()}'/>"
            nids.append(str(len(notes)))
            notes.append(f"<Note id='{len(notes)}'>{extra}<Properties>{props}</Properties></Note>")
        ids.append(str(len(beats)))
        beats.append(f"<Beat id='{len(beats)}'><Rhythm ref='{rid}'/>"
                     + (f"<Notes>{' '.join(nids)}</Notes>" if nids else "") + "</Beat>")
    voices.append(f"<Voice id='{bi}'><Beats>{' '.join(ids)}</Beats></Voice>")
    bar_xml.append(f"<Bar id='{bi}'><Voices>{bi} -1 -1 -1</Voices></Bar>")
    master.append(f"<MasterBar><Time>4/4</Time><Bars>{bi}</Bars></MasterBar>")

rh = "".join(f"<Rhythm id='{i}'><NoteValue>{VALUES[d]}</NoteValue></Rhythm>" for d, i in rhythms.items())
gpif = f"""<?xml version="1.0" encoding="utf-8"?>
<GPIF><Score><Title>tabscroll demo</Title></Score>
<MasterTrack><Automations><Automation><Type>Tempo</Type><Bar>0</Bar><Position>0</Position><Value>132 2</Value></Automation></Automations></MasterTrack>
<Tracks><Track id="0"><Name>Demo Guitar</Name><Staves><Staff><Properties>
<Property name="Tuning"><Pitches>{' '.join(map(str, TUNING))}</Pitches></Property></Properties></Staff></Staves></Track></Tracks>
<MasterBars>{''.join(master)}</MasterBars><Bars>{''.join(bar_xml)}</Bars><Voices>{''.join(voices)}</Voices>
<Beats>{''.join(beats)}</Beats><Notes>{''.join(notes)}</Notes><Rhythms>{rh}</Rhythms></GPIF>"""

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.gp")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("Content/score.gpif", gpif)
print("wrote", out)
