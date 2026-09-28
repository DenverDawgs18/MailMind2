"""Synthesises the 60 s soundtrack for the MailMind film.

Uptempo electro-pop at 120 BPM (one bar = 2 s, so every scene cut lands on a
bar line): four-on-the-floor kick, claps, 16th hats, a side-chained pumping
bass, supersaw chord stabs, a plucked lead hook and risers/impacts synced to
the picture. Everything is generated from scratch with numpy/scipy.
"""
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, fftconvolve, resample_poly, sosfilt

SR = 48000
DUR = 60.0
N = int(SR * DUR)
BAR, BEAT, S16 = 2.0, 0.5, 0.125
rng = np.random.default_rng(11)

BUSES = ['drums', 'bass', 'stabs', 'pad', 'lead', 'arp', 'fx']
bus = {b: np.zeros((N, 2)) for b in BUSES}
send = np.zeros((N, 2))   # reverb send
dsend = np.zeros((N, 2))  # delay send


def hz(note):
    names = {'C': 0, 'C#': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4, 'F': 5, 'F#': 6, 'G': 7, 'G#': 8, 'A': 9, 'Bb': 10, 'B': 11}
    n, o = note[:-1], int(note[-1])
    return 440.0 * 2 ** ((names[n] + 12 * (o + 1) - 69) / 12)


def tt(d):
    return np.arange(int(d * SR)) / SR


def lp(x, f, order=2):
    return sosfilt(butter(order, min(f, SR * .45), 'low', fs=SR, output='sos'), x, axis=0)


def hp(x, f, order=2):
    return sosfilt(butter(order, f, 'high', fs=SR, output='sos'), x, axis=0)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo, hi], 'band', fs=SR, output='sos'), x, axis=0)


def add(name, sig, t, gain=1.0, pan=0.0, rev=0.0, dly=0.0):
    i = int(round(t * SR))
    if i >= N or i + len(sig) <= 0:
        return
    if i < 0:
        sig, i = sig[-i:], 0
    sig = sig[: N - i]
    if sig.ndim == 1:
        l, r = np.cos((pan + 1) * np.pi / 4), np.sin((pan + 1) * np.pi / 4)
        sig = np.stack([sig * l, sig * r], 1) * np.sqrt(2)
    sig = sig * gain
    bus[name][i:i + len(sig)] += sig
    if rev:
        send[i:i + len(sig)] += sig * rev
    if dly:
        dsend[i:i + len(sig)] += sig * dly


def saw(f, d, phase=None):
    """Naive saw rendered at 2x and decimated (cheap anti-aliasing)."""
    n2 = int(d * SR) * 2
    ph = (phase if phase is not None else rng.random()) + f * np.arange(n2) / (2 * SR)
    return resample_poly(2 * (ph % 1.0) - 1, 1, 2)[: int(d * SR)]


def adsr(n, a=.005, d=.1, s=.6, r=.05, hold=None):
    t = np.arange(n) / SR
    hold = hold if hold is not None else n / SR - r
    e = np.where(t < a, t / a, s + (1 - s) * np.exp(-(t - a) / max(d, 1e-4)))
    rel = np.clip((t - hold) / r, 0, 1)
    return e * (1 - rel)


# ---------------- drums ----------------
def kick(punch=1.0):
    t = tt(.5)
    f = 48 + 120 * np.exp(-t * 28) + 40 * np.exp(-t * 180)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 6.5)
    click = hp(rng.standard_normal(len(t)), 3000) * np.exp(-t * 400) * .25
    return np.tanh((body + click) * 1.6 * punch) * .9


def clap():
    t = tt(.4)
    n = rng.standard_normal(len(t))
    e = sum((t >= o) * np.exp(-np.clip(t - o, 0, None) * 110) for o in (0, .009, .019))
    e = e + (t >= .026) * np.exp(-np.clip(t - .026, 0, None) * 16) * .55
    body = np.sin(2 * np.pi * 190 * t) * np.exp(-t * 35) * .25
    return (bp(n, 1000, 4200) * e + body) * .55


def snare():
    t = tt(.25)
    n = bp(rng.standard_normal(len(t)), 1500, 8000) * np.exp(-t * 22)
    tone = np.sin(2 * np.pi * 200 * t) * np.exp(-t * 30)
    return (n * .6 + tone * .5) * .6


def hat(open_=False):
    d = .32 if open_ else .05
    t = tt(d)
    metal = sum(np.sign(np.sin(2 * np.pi * f * t)) for f in (3140, 4270, 5510, 6830, 8120, 9460))
    x = hp(metal * .15 + rng.standard_normal(len(t)), 7500)
    return x * np.exp(-t * (9 if open_ else 70)) * (.22 if open_ else .2)


def crash(d=2.8):
    t = tt(d)
    metal = sum(np.sign(np.sin(2 * np.pi * f * t + rng.random() * 6)) for f in (2913, 3781, 4410, 5203, 6671, 7935))
    x = hp(metal * .2 + rng.standard_normal(len(t)), 4500)
    x = np.stack([x, np.roll(x, 371)], 1)
    return x * (np.exp(-t * 1.6) * np.minimum(1, t / .002))[:, None] * .22


# ---------------- tonal ----------------
def bass_note(f, d):
    n = int(d * SR)
    x = lp(saw(f, d) * .55, 900) + np.sin(2 * np.pi * f * np.arange(n) / SR) * .8
    return np.tanh(x * 1.4) * adsr(n, .003, .12, .55, .03) * .5


def supersaw(f, d, voices=5, spread=.012):
    n = int(d * SR)
    L, R = np.zeros(n), np.zeros(n)
    for v in range(voices):
        det = 1 + spread * (v - (voices - 1) / 2) / ((voices - 1) / 2)
        s = saw(f * det, d)
        p = (v / (voices - 1)) * 2 - 1
        L += s * (1 - p) / 2
        R += s * (1 + p) / 2
    return np.stack([L, R], 1) / voices


def stab(freqs, d=.22, bright=4200):
    x = sum(supersaw(f, d) for f in freqs) / len(freqs)
    x = lp(x, bright)
    return x * adsr(len(x), .002, .07, .35, .04)[:, None] * .75


def pluck(f, d=.3, bright=3600):
    n = int(d * SR)
    x = saw(f, d) * .6 + saw(f * 1.006, d) * .4
    x = lp(x, bright) + lp(x, 900) * .6
    return x * adsr(n, .002, .09, .12, .05)


def pad_chord(freqs, d):
    x = sum(supersaw(f, d, 5, .008) for f in freqs) / len(freqs)
    return lp(x, 1600) * .35


def blip(f0, f1=None, d=.12, g=.5):
    """Soft synth UI blip (pitch glide), replaces bell sounds."""
    t = tt(d)
    f1 = f1 or f0
    f = f0 + (f1 - f0) * np.minimum(1, t / (d * .6))
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = np.sin(ph) + .3 * np.sin(2 * ph)
    return x * np.exp(-t * 28) * np.minimum(1, t / .002) * g


def tick():
    t = tt(.04)
    return (np.sin(2 * np.pi * 1900 * t) * np.exp(-t * 160) + hp(rng.standard_normal(len(t)), 5000) * np.exp(-t * 300) * .4) * .35


def riser(d, f0=300, f1=9000):
    t = tt(d)
    n = rng.standard_normal(len(t))
    out = np.zeros(len(t))
    blocks = 48
    for b in range(blocks):
        a, e = int(len(t) * b / blocks), int(len(t) * (b + 1) / blocks)
        fc = f0 * (f1 / f0) ** (b / blocks)
        seg = bp(n[max(0, a - 2000):e], fc * .6, min(fc * 1.6, 20000))
        out[a:e] = seg[-(e - a):]
    x = out * (t / d) ** 2.2
    return np.stack([x, np.roll(x, 240)], 1) * .5


def impact():
    t = tt(1.8)
    boom = np.sin(2 * np.pi * np.cumsum(38 + 60 * np.exp(-t * 12)) / SR) * np.exp(-t * 2.2)
    return np.tanh(boom * 1.5) * .8


# ---------------- harmony ----------------
PROG = {  # voicings for stabs/pads and bass roots
    'Am': (['A3', 'C4', 'E4', 'A4'], 'A1'),
    'F': (['A3', 'C4', 'F4', 'A4'], 'F1'),
    'C': (['G3', 'C4', 'E4', 'G4'], 'C2'),
    'G': (['G3', 'B3', 'D4', 'G4'], 'G1'),
}
ORDER = ['Am', 'F', 'C', 'G']


def chord_at(bar):
    return ORDER[bar % 4]


LEAD = {
    'Am': [('E5', 0), ('E5', 2), ('D5', 4), ('C5', 6), ('D5', 8), ('E5', 11)],
    'F': [('C5', 0), ('C5', 2), ('A4', 4), ('C5', 6), ('D5', 10), ('C5', 12)],
    'C': [('E5', 0), ('E5', 2), ('G5', 4), ('E5', 6), ('D5', 8), ('C5', 12), ('D5', 14)],
    'G': [('D5', 0), ('B4', 4), ('D5', 8), ('E5', 10), ('D5', 12), ('B4', 14)],
}
STAB_POS = [0, 3, 6, 8, 11, 14]  # 16th positions in a bar


def drums_bar(b, kick_on=True, clap_on=True, hats=16, open_hats=True, g=1.0):
    t0 = b * BAR
    if kick_on:
        for k in range(4):
            add('drums', kick(), t0 + k * BEAT, .95 * g)
    if clap_on:
        for k in (1, 3):
            add('drums', clap(), t0 + k * BEAT, .75 * g, .05, rev=.18)
    if hats:
        step = 16 // hats
        for s in range(0, 16, step):
            acc = 1.0 if s % 4 == 2 else .55
            add('drums', hat(), t0 + s * S16, acc * g, .3)
    if open_hats:
        for k in range(4):
            add('drums', hat(True), t0 + k * BEAT + BEAT / 2, .6 * g, -.25, rev=.1)


def bass_bar(b, g=1.0):
    root = hz(PROG[chord_at(b)][1])
    for s in range(8):
        f = root * (2 if s % 2 else 1)
        add('bass', bass_note(f, .24), b * BAR + s * .25, g)


def stabs_bar(b, g=1.0, bright=4200):
    fr = [hz(n) for n in PROG[chord_at(b)][0]]
    for p in STAB_POS:
        add('stabs', stab(fr, .2, bright), b * BAR + p * S16, g * (1 if p in (0, 8) else .8), rev=.2, dly=.08)


def pad_bars(b0, b1, g=1.0):
    for b in range(b0, b1):
        fr = [hz(n) / 2 for n in PROG[chord_at(b)][0]] + [hz(PROG[chord_at(b)][0][2])]
        x = pad_chord(fr, BAR + .25)
        x *= np.minimum(1, np.arange(len(x)) / (.05 * SR))[:, None]
        x[-int(.25 * SR):] *= np.linspace(1, 0, int(.25 * SR))[:, None]
        add('pad', x, b * BAR, g, rev=.35)


def lead_bar(b, g=1.0, octave=1):
    for n, pos in LEAD[chord_at(b)]:
        add('lead', pluck(hz(n) * octave, .32), b * BAR + pos * S16, g, .1, rev=.2, dly=.35)


def arp_bar(b, g=1.0):
    notes = PROG[chord_at(b)][0]
    seq = [notes[i] for i in (0, 1, 2, 3, 2, 1, 2, 3)] * 2
    for s, n in enumerate(seq):
        add('arp', pluck(hz(n) * 2, .16, 5200), b * BAR + s * S16, g * (1 if s % 4 == 0 else .7), -.3 + .6 * (s % 2), dly=.15)


# ---------------- arrangement ----------------
# 0-6: filtered intro — pad + muted four-on-the-floor + ticking hats
pad_bars(0, 6, .9)
for b in range(0, 3):
    drums_bar(b, clap_on=False, hats=8, open_hats=False, g=.55)
# 6-12: build — clap in, 16th hats, stabs + bass fading up under a filter sweep
for b in range(3, 6):
    drums_bar(b, clap_on=True, hats=16, open_hats=b >= 4, g=.8)
    stabs_bar(b, .45 + .15 * (b - 3), bright=900 + 900 * (b - 3))
    bass_bar(b, .5 + .15 * (b - 3))
for k in range(8):  # snare roll 11-12
    add('drums', snare(), 11.0 + k * S16, .3 + .08 * k, rev=.2)
add('fx', riser(2.0), 10.0, .9, rev=.3)
add('fx', crash(), 12.0, .8, rev=.4)
add('drums', kick(1.2), 12.0, 1.0)
add('fx', impact(), 12.0, .6)

# 12-16: the question — break. Pad + heartbeat kick, build back in
pad_bars(6, 8, 1.1)
for k in range(8):
    add('drums', lp(kick(), 400), 12.0 + k * BEAT, .45)
for k in range(8):
    add('drums', snare(), 15.0 + k * S16, .15 + .08 * k, rev=.25)
add('fx', riser(2.2, 250, 11000), 13.8, 1.0, rev=.3)

# 16: DROP with the logo
add('fx', impact(), 16.0, .9)
add('fx', crash(), 16.0, 1.0, rev=.4)
for b in range(8, 22):        # 16-44 main groove
    drums_bar(b)
    bass_bar(b)
    stabs_bar(b, .85)
pad_bars(8, 22, .6)
for b in list(range(10, 15)) + list(range(19, 22)):  # hook 20-30, 38-44
    lead_bar(b, .9)
for b in range(15, 19):       # 30-38: arp section for the phone
    arp_bar(b, .7)
add('fx', crash(), 30.0, .6, rev=.3)
add('fx', crash(), 38.0, .6, rev=.3)
add('fx', riser(1.5), 42.5, .7, rev=.3)

# 44-48: stop-time hits on "No app… / No password… / No noise… / Just what to do."
for k, (tm, name) in enumerate([(44, 'F'), (45, 'G'), (46, 'Am'), (47, 'C')]):
    fr = [hz(n) for n in PROG[name][0]]
    add('stabs', stab(fr + [f * 2 for f in fr], .55 if k < 3 else .9, 7000), tm, 1.1, rev=.35)
    add('bass', bass_note(hz(PROG[name][1]), .6), tm, 1.0)
    add('drums', kick(1.3), tm, 1.0)
    add('drums', clap(), tm, .6, rev=.3)
    if k in (0, 3):
        add('fx', crash(), tm, .8, rev=.4)
for k in range(4):
    add('drums', snare(), 47.5 + k * S16, .35 + .1 * k, rev=.2)
add('fx', riser(1.6, 400, 12000), 46.4, .8)

# 48-54: peak — everything
add('fx', crash(), 48.0, 1.0, rev=.4)
add('fx', impact(), 48.0, .7)
for b in range(24, 27):
    drums_bar(b)
    bass_bar(b)
    stabs_bar(b, .9, bright=5200)
    lead_bar(b, 1.0)
    arp_bar(b, .45)
pad_bars(24, 27, .7)
for k in range(8):
    add('drums', snare(), 53.0 + k * S16, .25 + .07 * k, rev=.2)

# 54-60: final hit on C, ring out
add('fx', impact(), 54.0, 1.0)
add('fx', crash(4.5), 54.0, 1.1, rev=.5)
add('drums', kick(1.3), 54.0, 1.0)
fin = [hz(n) for n in ['C3', 'G3', 'C4', 'E4', 'G4', 'D5']]
x = sum(supersaw(f, 6.0, 7, .01) for f in fin) / len(fin)
t6 = tt(6.0)
x = lp(x, 3000) * (np.exp(-t6 * .55) * np.minimum(1, t6 / .004))[:, None]
add('stabs', x, 54.0, 1.1, rev=.5)
add('bass', bass_note(hz('C2'), 2.0) * np.exp(-tt(2.0) * 1.2), 54.0, 1.0)
for k, n in enumerate(['E5', 'G5', 'C6']):
    add('lead', pluck(hz(n), .4), 55.0 + k * S16 * 2, .6, rev=.3, dly=.4)

# ---------------- UI sound design synced to picture ----------------
for tm in [23.0, 25.1, 26.67, 27.98]:       # rows land in the list
    add('fx', tick(), tm, 1.0, .35)
add('fx', blip(1320, 1760), 28.55, .5, .3, rev=.2)   # first item checked
add('fx', blip(880, 1320, .1), 31.5, .6, .2, rev=.2)   # notification
add('fx', blip(1320, 1320, .12), 31.62, .5, .2, rev=.2)
for k in range(4):
    add('fx', tick(), 34.0 + k * .22, .7, .3)
add('fx', blip(1320, 1760), 36.05, .5, .3, rev=.2)   # snacks checked
for k in range(4):
    add('fx', tick(), 38.4 + k * .25, .6, (-.5, .5, -.5, .5)[k])
for k in range(5):
    add('fx', tick(), 41.0 + k * .18, .6, .1)
add('fx', blip(660, 1320, .16), 17.25, .45, 0, rev=.3)  # logo check

# ---------------- mix ----------------
# side-chain pump from the kick pattern (four-on-the-floor where the kick plays)
duck = np.ones(N)
kick_times = [b * BAR + k * BEAT for b in list(range(0, 6)) + list(range(8, 22)) + list(range(24, 27)) for k in range(4)]
kick_times += [44, 45, 46, 47, 54]
tk = np.arange(int(.35 * SR)) / SR
shape = 1 - .75 * np.exp(-tk / .09) * np.minimum(1, tk / .004 + .6)
for kt in kick_times:
    i = int(kt * SR)
    seg = duck[i:i + len(shape)]
    duck[i:i + len(shape)] = np.minimum(seg, shape[:len(seg)])
for name, depth in (('bass', 1.0), ('stabs', .8), ('pad', 1.0), ('arp', .6)):
    bus[name] *= (1 - depth + depth * duck)[:, None]

# intro filter: pad + stabs open up across 0-12
cut = np.interp(np.arange(N) / SR, [0, 6, 11.9, 12.0, 16, 44, 60], [500, 900, 3500, 900, 1400, 1400, 1400])
blk = 256
for name in ('pad',):
    y = bus[name]
    out = np.zeros_like(y)
    zi = np.zeros((1, 2, 2))
    for a in range(0, N, blk):
        sos = butter(2, cut[a], 'low', fs=SR, output='sos')
        out[a:a + blk], zi = sosfilt(sos, y[a:a + blk], axis=0, zi=zi)
    bus[name] = out

levels = {'drums': .8, 'bass': .75, 'stabs': 7.0, 'pad': 3.0, 'lead': .5, 'arp': .35, 'fx': .8}
dry = sum(bus[b] * levels[b] for b in BUSES)
send_total = send.copy()

# ping-pong delay (dotted 8th)
d = int(.375 * SR)
dl = np.zeros_like(dsend)
buf = dsend.copy()
for k in range(1, 6):
    g = .45 ** k
    sh = buf[: N - k * d] * g
    if k % 2:
        dl[k * d:, 0] += sh[:, 1]
        dl[k * d:, 1] += sh[:, 0]
    else:
        dl[k * d:] += sh
dl = lp(hp(dl, 400), 5000)

# plate-ish reverb
ir_t = tt(2.2)
ir = np.stack([lp(rng.standard_normal(len(ir_t)), 6500) * np.exp(-3.4 * ir_t) for _ in range(2)], 1)
ir[: int(.015 * SR)] = 0
ir /= np.sqrt((ir ** 2).sum(0))
rev = np.stack([fftconvolve(send_total[:, c], ir[:, c])[:N] for c in range(2)], 1)
rev = hp(rev, 250)

mix = dry + dl * .5 + rev * .6
mix = hp(mix, 28)
# master: gentle glue + saturation, then fades
mix /= np.percentile(np.abs(mix), 99.9) / .8
mix = np.tanh(mix * 1.1) / np.tanh(1.1)
fi = int(.01 * SR)
mix[:fi] *= np.linspace(0, 1, fi)[:, None]
fo = int(1.2 * SR)
mix[-fo:] *= (np.linspace(1, 0, fo) ** 1.5)[:, None]
mix *= .93 / np.abs(mix).max()
wavfile.write('soundtrack.wav', SR, (mix * 32767).astype(np.int16))
print('peak', np.abs(mix).max(), 'rms dB', 20 * np.log10(np.sqrt((mix ** 2).mean())))
