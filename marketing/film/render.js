// usage: node render.js preview t1,t2,...   |  node render.js full fps start end outdir
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [mode, a, b, c, d] = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath: undefined, args: ['--font-render-hinting=none', '--force-color-profile=srgb'] });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  await page.goto('file://' + path.resolve('scene.html'));
  await page.evaluate(() => window.ready);
  if (mode === 'preview') {
    for (const t of a.split(',').map(Number)) {
      await page.evaluate(t => window.render(t), t);
      await page.screenshot({ path: `prev/t${t.toFixed(2)}.png` });
    }
  } else {
    const fps = +a, start = +b, end = +c, out = d;
    for (let f = start; f < end; f++) {
      await page.evaluate(t => window.render(t), f / fps);
      await page.screenshot({ path: `${out}/f${String(f).padStart(5, '0')}.jpg`, type: 'jpeg', quality: 96 });
    }
  }
  await browser.close();
})();
