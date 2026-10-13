// Render a scene HTML to a still PNG or to an animation (frames -> MP4 + GIF).
// Usage: node render.js <scene.html> <out-base> <width> <height> [--anim fps] [--scale n] [--gif-width px]
// A scene defines window.DURATION (seconds) and window.render(t). Stills render t = DURATION.
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

(async () => {
  const [scene, outBase, w, h, ...rest] = process.argv.slice(2);
  const opt = (k, d) => { const i = rest.indexOf(k); return i >= 0 ? rest[i + 1] : d; };
  const anim = rest.includes('--anim');
  const fps = Number(opt('--anim', 30));
  const scale = Number(opt('--scale', anim ? 1 : 2));
  const gifWidth = Number(opt('--gif-width', 960));
  const W = Number(w), H = Number(h);

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: scale });
  await page.goto('file://' + path.resolve(scene));
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
  const dur = await page.evaluate(() => window.DURATION || 0);

  if (!anim) {
    const tAt = opt('--t', null);
    await page.evaluate(t => window.render && window.render(t), tAt === null ? dur : Number(tAt));
    await page.screenshot({ path: outBase + '.png', omitBackground: true });
    console.log('wrote', outBase + '.png');
  } else {
    await page.evaluate(() => document.body.classList.add('flat'));
    const dir = outBase + '_frames';
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
    const n = Math.round(dur * fps);
    for (let i = 0; i < n; i++) {
      await page.evaluate(t => window.render(t), i / fps);
      await page.screenshot({ path: path.join(dir, String(i).padStart(5, '0') + '.png') });
    }
    const pattern = path.join(dir, '%05d.png');
    execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(fps), '-i', pattern,
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'slow', '-movflags', '+faststart',
      outBase + '.mp4']);
    const gifFps = 15;
    const vf = `fps=${gifFps},scale=${gifWidth}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`;
    execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(fps), '-i', pattern,
      '-vf', vf, '-loop', '0', outBase + '.gif']);
    fs.rmSync(dir, { recursive: true, force: true });
    console.log('wrote', outBase + '.mp4', outBase + '.gif', `(${n} frames)`);
  }
  await browser.close();
})();
