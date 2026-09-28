# MailMind film

A 60-second spot, generated entirely in code — no stock footage, no samples.

- `scene.html` — every frame of the picture. `window.render(t)` lays out the scene for time `t` (seconds), so rendering is fully deterministic.
- `render.js` — drives headless Chromium (Playwright) to screenshot each frame.
- `music.py` — synthesises the soundtrack (plucked strums, felt piano, bells, bass, soft drums and UI sound design) with numpy/scipy. 120 BPM, so every scene cut lands on a bar line.
- `mailmind.mp4` — the 1080p60 master. The site serves a lighter encode from `static/video/mailmind.mp4`.

## Rebuild

```bash
cd marketing/film
mkdir -p fonts frames
# Inter (variable, latin subset) from Google Fonts
curl -sSL -o fonts/inter.woff2 https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2
npm i playwright && pip install numpy scipy

# picture: 3600 frames at 60 fps (4 workers in parallel)
for i in 0 1 2 3; do node render.js full 60 $((i*900)) $(((i+1)*900)) frames & done; wait

# sound
python3 music.py   # -> soundtrack.wav

# master
ffmpeg -framerate 60 -i frames/f%05d.jpg -i soundtrack.wav -c:v libx264 -preset slow -crf 17 \
  -tune animation -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart -shortest mailmind.mp4

# web encode + poster for the site
ffmpeg -i mailmind.mp4 -c:v libx264 -preset slow -crf 25 -tune animation -pix_fmt yuv420p \
  -c:a aac -b:a 128k -movflags +faststart ../../static/video/mailmind.mp4
ffmpeg -ss 56.75 -i mailmind.mp4 -frames:v 1 -vf scale=1280:-1 -q:v 3 ../../static/video/poster.jpg
```

`mkdir -p prev && node render.js preview 2,9.5,17.5` writes stills to `prev/` for quick checks.

## Storyboard

| Time | Scene |
|---|---|
| 0–6 s | "You have 1,284 unread emails." |
| 6–12 s | Emails pile up; one stays sharp — "Somewhere in there is the one that matters." |
| 12–16 s | "What if your inbox just told you what to do?" |
| 16–20 s | Logo: the envelope flap folds into a check. |
| 20–30 s | "It reads your email. And writes your to-do list." |
| 30–38 s | 7:00 AM notification → the digest. "Every morning. Right in your inbox." |
| 38–44 s | Four inboxes merge into one list. |
| 44–50 s | On the beat: "No app to open. No new password. No noise. Just what to do." |
| 50–54 s | "Your inbox, handled." |
| 54–60 s | End card. |
