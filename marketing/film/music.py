"""Synthesises the 60 s soundtrack for the MailMind spot.

120 BPM, one bar = 2 s, so scene cuts land on bar lines. Everything is
generated from scratch with numpy: plucked-string strums, felt piano, bell
melody, bass, soft drums, plus UI sound design synced to the picture.
"""
import numpy as np
from scipy.signal import butter, sosfilt, fftconvolve
from scipy.io import wavfile

SR = 48000
DUR = 60.0
N = int(SR * DUR)
BEAT = 0.5
BAR = 2.0
rng = np.random.default_rng(3)

dry = np.zeros((N, 2))
wet = np.zeros((N, 2))  # reverb send


def hz(note):
    names = {'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4, 'F': 5, 'F#': 6, 'Gb': 6,
             'G': 7, 'G#': 8, 'Ab': 8, 'A': 9, 'A#': 10, 'Bb': 10, 'B': 11}
    n, o = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[n] + 12 * (o + 1) - 69) / 12)


def add(sig, t, gain=1.0, pan=0.0, send=0.25):
    i = int(t * SR)
    if i >= N:
        return
    sig = sig[: N - i]
    l, r = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
    dry[i:i + len(sig), 0] += sig * gain * l
    dry[i:i + len(sig), 1] += sig * gain * r
    wet[i:i + len(sig), 0] += sig * gain * l * send
    wet[i:i + len(sig), 1] += sig * gain * r * send


def tt(d):
    return np.arange(int(d * SR)) / SR


def env_ar(n, a, total):
    e = np.ones(n)
    na = max(1, int(a * SR))
    e[:na] = np.linspace(0, 1, na)
    return e


def lp(x, f, order=2):
    return sosfilt(butter(order, f, 'low', fs=SR, output='sos'), x)


def hp(x, f, order=2):
    return sosfilt(butter(order, f, 'high', fs=SR, output='sos'), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], 'band', fs=SR, output='sos'), x)


# ---------------- instruments ----------------
def pluck(f, d=1.6, bright=1.0):
    t = tt(d)
    s = np.zeros_like(t)
    for k in range(1, 14):
        if f * k > 16000:
            break
        amp = (1 / k ** 1.25) * (bright ** (k - 1))
        dec = 2.2 + 0.9 * k
        s += amp * np.sin(2 * np.pi * f * k * t * (1 + 0.0004 * k)) * np.exp(-dec * t)
    s *= env_ar(len(t), 0.002, d)
    noise = lp(rng.standard_normal(len(t)), 3500) * np.exp(-60 * t) * 0.15
    return (s + noise) * 0.5


def piano(f, d=3.0):
    t = tt(d)
    s = np.zeros_like(t)
    B = 0.0002
    for k in range(1, 10):
        fk = f * k * np.sqrt(1 + B * k * k)
        if fk > 15000:
            break
        amp = 1 / k ** 1.6
        s += amp * np.sin(2 * np.pi * fk * t) * (0.6 * np.exp(-(1.2 + .5 * k) * t) + 0.4 * np.exp(-(0.35 + .2 * k) * t))
    s *= env_ar(len(t), 0.004, d)
    s = lp(s, 2600)  # felt
    fade = np.ones_like(t)
    fade[-int(.3 * SR):] = np.linspace(1, 0, int(.3 * SR))
    return s * fade * 0.45


def bell(f, d=2.2):
    t = tt(d)
    parts = [(1, 1.0, 1.6), (2.0, .35, 2.6), (2.76, .28, 3.5), (5.4, .12, 6), (8.93, .05, 9)]
    s = sum(a * np.sin(2 * np.pi * f * r * t) * np.exp(-dec * t) for r, a, dec in parts)
    return s * env_ar(len(t), .001, d) * 0.28


def bass(f, d=0.9):
    t = tt(d)
    s = np.sin(2 * np.pi * f * t) + .25 * np.sin(4 * np.pi * f * t) + .08 * np.sin(6 * np.pi * f * t)
    e = np.minimum(1, t / .008) * np.exp(-2.2 * t)
    e[-int(.05 * SR):] *= np.linspace(1, 0, int(.05 * SR))
    return s * e * 0.55


def pad(freqs, d, a=1.2, r=1.2):
    t = tt(d)
    s = np.zeros_like(t)
    for f in freqs:
        for det in (-0.004, 0, 0.0045):
            for k in range(1, 7):
                s += np.sin(2 * np.pi * f * (1 + det) * k * t + rng.random() * 6.28) / k ** 1.5
    s = lp(s, 1400)
    e = np.minimum(1, t / a) * np.minimum(1, (d - t) / r)
    return s * e * 0.03


def kick():
    t = tt(.45)
    f = 45 + 80 * np.exp(-30 * t)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-7 * t) * 0.8


def clap():
    t = tt(.35)
    n = rng.standard_normal(len(t))
    e = np.zeros_like(t)
    for off in (0, .011, .022):
        e += (t >= off) * np.exp(-90 * np.clip(t - off, 0, None)) * (t >= off)
    e += (t >= .03) * np.exp(-14 * np.clip(t - .03, 0, None)) * .5
    return bp(n, 900, 2600) * e * 0.35


def shaker(v=1.0):
    t = tt(.09)
    return lp(hp(rng.standard_normal(len(t)), 5000), 11000) * np.exp(-55 * t) * np.minimum(1, t / .006) * 0.06 * v


def ding(f=1318.5):
    return bell(f, 1.6) * 1.2 + bell(f * 1.5, 1.6) * .6


def tick():
    t = tt(.06)
    return (np.sin(2 * np.pi * 2400 * t) * np.exp(-120 * t) + hp(rng.standard_normal(len(t)), 4000) * np.exp(-200 * t) * .3) * .35


def whoosh(d=1.2, rev=False):
    t = tt(d)
    n = rng.standard_normal(len(t))
    x = t / d
    e = np.sin(np.pi * x) ** 2
    out = np.zeros_like(n)
    # sweep a band-pass up by stitching filtered chunks
    bands = np.geomspace(400, 5000, 12)
    for i, fc in enumerate(bands):
        w = np.exp(-((x - i / 11) ** 2) / 0.02)
        out += bp(n, fc * .7, min(fc * 1.4, 20000)) * w
    if rev:
        out = out[::-1]
    return out * e * 0.07


def strum(notes, t0, down=True, vel=1.0, bright=.85, pan=0.0):
    seq = notes if down else notes[::-1]
    for i, n in enumerate(seq):
        add(pluck(hz(n), 1.3, bright), t0 + i * 0.011, gain=vel * 0.18 * (1 - .06 * i), pan=pan + (i - 2) * .08, send=.18)


# ---------------- arrangement ----------------
CH = {  # ukulele-ish voicings, bass root
    'C': (['G4', 'C5', 'E5', 'G5'], 'C3'),
    'G': (['G4', 'B4', 'D5', 'G5'], 'G2'),
    'Am': (['A4', 'C5', 'E5', 'A5'], 'A2'),
    'F': (['A4', 'C5', 'F5', 'A5'], 'F2'),
    'Fmaj7': (['A4', 'C5', 'E5', 'A5'], 'F1'),
    'Dm7': (['A4', 'C5', 'D5', 'F5'], 'D2'),
    'Gsus': (['G4', 'C5', 'D5', 'G5'], 'G2'),
    'Cadd9': (['G4', 'C5', 'D5', 'E5'], 'C2'),
}
PROG = ['C', 'G', 'Am', 'F']

# --- Intro 0-12: felt piano, wistful. Fmaj7 | C | Dm7 | Gsus | (chaos) Fmaj7 | Gsus
intro = [('Fmaj7', ['F3', 'C4', 'E4', 'A4']), ('C', ['C3', 'G3', 'E4', 'G4']), ('Dm7', ['D3', 'A3', 'C4', 'F4']),
         ('Gsus', ['G2', 'D3', 'C4', 'D4']), ('Fmaj7', ['F2', 'C4', 'E4', 'A4']), ('Gsus', ['G2', 'D3', 'C4', 'G4'])]
for b, (_, notes) in enumerate(intro):
    t0 = b * BAR + 0.15
    # arpeggio in 8ths: 1-2-3-4-3-2-3-4
    pat = [0, 1, 2, 3, 2, 1, 2, 3]
    for k, p in enumerate(pat):
        add(piano(hz(notes[p]), 2.2), t0 + k * 0.25, gain=0.2 if k else 0.26, pan=-.2 + .13 * p, send=.5)
add(piano(hz('E5'), 3), 0.15 + 2 * BAR + 1.5, .14, .3, .5)
add(piano(hz('D5'), 3), 0.15 + 3 * BAR + 1.5, .14, .3, .5)

# chaos 6-12: notification blips, increasing density, pentatonic so it stays pretty
penta = ['C6', 'D6', 'E6', 'G6', 'A6', 'C7', 'E6', 'G5', 'A5']
t = 5.6
while t < 11.8:
    dens = 0.32 * (1 - (t - 5.6) / 7) + 0.045
    add(bell(hz(penta[rng.integers(len(penta))]), .8) * .9, t, gain=.13, pan=rng.uniform(-.8, .8), send=.35)
    t += dens * rng.uniform(.5, 1.5)
add(whoosh(1.0), 5.0, gain=1.0)
# riser into the stop
rt = tt(2.0)
riser = lp(hp(rng.standard_normal(len(rt)), 2500), 9000) * (rt / 2.0) ** 3 * .035
add(riser, 10.0, 1, 0, .6)

# --- 12-16: suspended hush. pad + "what if" piano notes
add(pad([hz('F3'), hz('C4'), hz('G4'), hz('A4')], 4.4, 1.0, 1.5), 12.2, 1, 0, .6)
add(piano(hz('G5'), 3), 12.6, .25, -.2, .6)
add(piano(hz('A5'), 3), 13.4, .25, .2, .6)
add(piano(hz('C6'), 3), 14.4, .2, 0, .7)
add(whoosh(1.4, rev=True), 14.7, gain=1.1)

# --- 16: logo chime + downbeat
add(ding(hz('E6')), 17.25, .45, 0, .6)
add(bell(hz('C6'), 3), 16.0, .35, -.1, .6)
add(bell(hz('G6'), 3), 17.25, .2, .2, .6)


def groove(bar0, bar1, drums=True, melody=False, full=1.0):
    for b in range(bar0, bar1):
        name = PROG[b % 4]
        notes, root = CH[name]
        t0 = b * BAR
        # strum pattern: D . D U . U D U (8ths)
        for pos, down, v in [(0, 1, 1), (2, 1, .8), (3, 0, .55), (5, 0, .6), (6, 1, .85), (7, 0, .5)]:
            strum(notes, t0 + pos * .25, bool(down), v * full, pan=-.25)
        # bass: root on 1 and 3, fifth pickup
        r = hz(root)
        add(bass(r, .9), t0, .9 * full, 0, .05)
        add(bass(r, .5), t0 + 1.0, .7 * full, 0, .05)
        add(bass(r * 1.5, .4), t0 + 1.5, .5 * full, 0, .05)
        if drums:
            add(kick(), t0, .55 * full, 0, .05)
            add(kick(), t0 + 1.0, .45 * full, 0, .05)
            add(clap(), t0 + .5, .8 * full, .1, .35)
            add(clap(), t0 + 1.5, .8 * full, .1, .35)
            for s in range(16):
                add(shaker(1.0 if s % 2 else .55), t0 + s * .125, full, .45, .1)
        if melody:
            mel = {
                'C': [('E5', 0), ('G5', 2), ('A5', 4), ('G5', 5)],
                'G': [('D5', 0), ('G5', 2), ('B5', 4), ('A5', 5), ('G5', 6)],
                'Am': [('C6', 0), ('B5', 2), ('A5', 4), ('E5', 6)],
                'F': [('F5', 0), ('A5', 2), ('G5', 4)],
            }[name]
            for n, pos in mel:
                add(bell(hz(n), 1.8), t0 + pos * .25, .55, .25, .4)


groove(8, 22, drums=False, full=.85)          # 16-44 strums + bass
# drums enter at 20 s with the "reads your email" section, melody at 24
for b in range(10, 22):
    t0 = b * BAR
    add(kick(), t0, .5, 0, .05); add(kick(), t0 + 1.0, .4, 0, .05)
    add(clap(), t0 + .5, .7, .1, .35); add(clap(), t0 + 1.5, .7, .1, .35)
    for s in range(16):
        add(shaker(1.0 if s % 2 else .55), t0 + s * .125, 1, .45, .1)
for b in range(12, 22):
    name = PROG[b % 4]
    mel = {'C': [('E5', 0), ('G5', 2), ('A5', 4), ('G5', 5)], 'G': [('D5', 0), ('G5', 2), ('B5', 4), ('A5', 5), ('G5', 6)],
           'Am': [('C6', 0), ('B5', 2), ('A5', 4), ('E5', 6)], 'F': [('F5', 0), ('A5', 2), ('G5', 4)]}[name]
    for n, pos in mel:
        add(bell(hz(n), 1.8), b * BAR + pos * .25, .5, .25, .4)

# --- 44-48: stop-time hits on each word
for k, (tm, name) in enumerate([(44, 'F'), (45, 'G'), (46, 'Am'), (47, 'C')]):
    notes, root = CH[name]
    strum(notes, tm, True, 1.4, bright=.95)
    strum([n[:-1] + str(int(n[-1]) - 1) for n in notes], tm + .005, True, .9, bright=.8, pan=.3)
    add(bass(hz(root), .9 if k < 3 else 1.6), tm, 1.0, 0, .1)
    add(kick(), tm, .6, 0, .05)
    if k == 3:
        add(ding(hz('G6')), tm, .35, 0, .6)
        add(clap(), tm, .9, 0, .5)
add(whoosh(.9, rev=True), 43.2, 1.0)
# 47.5-48 fill
for s in range(4):
    add(clap(), 47.5 + s * .125, .35 + .12 * s, .1, .3)

# --- 48-54: full groove, bigger
groove(24, 27, drums=True, melody=True, full=1.0)
add(pad([hz('C4'), hz('E4'), hz('G4'), hz('C5')], 6.2, 1.5, 1.5), 48.0, .8, 0, .5)

# --- 54-60: resolve on Cadd9, ring out
notes, root = CH['Cadd9']
strum(notes, 54.0, True, 1.2, bright=.9)
add(bass(hz(root), 2.5), 54.0, 1.0, 0, .2)
add(kick(), 54.0, .6, 0, .05)
add(pad([hz('C3'), hz('G3'), hz('D4'), hz('E4'), hz('G4')], 6.0, .4, 3.5), 54.0, 1.2, 0, .6)
for k, n in enumerate(['E5', 'G5', 'D6', 'C6']):
    add(bell(hz(n), 3.5), 54.0 + k * .5 + (1.0 if k == 3 else 0), .45, -.3 + .2 * k, .6)
add(piano(hz('C4'), 4), 55.0, .5, 0, .6)
add(piano(hz('G4'), 4), 55.0, .35, 0, .6)
add(ding(hz('C7') / 2), 55.0, .3, 0, .7)  # logo check

# ---------------- UI sound design synced to picture ----------------
for tm in [23.0, 25.1, 26.67, 27.98]:            # rows land in the list
    add(tick(), tm, .9, .35, .15)
add(ding(hz('A6')), 28.55, .3, .3, .5)           # first item checked
add(whoosh(1.0), 29.6, .9)
add(ding(hz('E6')), 31.5, .5, .25, .5)        # notification
add(ding(hz('B6')), 31.62, .25, .25, .5)
for k in range(4):
    add(tick(), 34.0 + k * .22, .6, .3, .15)
add(ding(hz('A6')), 36.05, .3, .3, .5)           # snacks checked
add(whoosh(1.0), 37.5, .9)
for k in range(4):
    add(tick(), 38.4 + k * .25, .5, (-.5, .5, -.5, .5)[k], .2)
add(whoosh(1.1), 39.8, .8)
for k in range(5):
    add(tick(), 41.0 + k * .18, .5, .1, .15)
add(whoosh(1.0), 49.5, .8)
add(whoosh(1.0), 53.3, .8)

# ---------------- reverb + master ----------------
ir_t = tt(2.4)
ir = np.stack([lp(rng.standard_normal(len(ir_t)), 5000) * np.exp(-3.2 * ir_t) for _ in range(2)], 1)
ir[: int(.012 * SR)] = 0  # pre-delay
ir /= np.sqrt((ir ** 2).sum(0))
rev = np.stack([fftconvolve(wet[:, c], ir[:, c])[:N] for c in range(2)], 1)
mix = dry + rev * 0.9
mix = hp(mix.T, 30).T
# gentle master glue: soft clip + fade
mix /= np.abs(mix).max() / 0.95
mix = np.tanh(mix * 1.25) / np.tanh(1.25)
fade_n = int(1.5 * SR)
mix[-fade_n:] *= np.linspace(1, 0, fade_n)[:, None] ** 1.5
mix[: int(.02 * SR)] *= np.linspace(0, 1, int(.02 * SR))[:, None]
mix *= 0.89 / np.abs(mix).max()
wavfile.write('soundtrack.wav', SR, (mix * 32767).astype(np.int16))
print('peak', np.abs(mix).max(), 'rms', np.sqrt((mix ** 2).mean()))
