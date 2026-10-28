// Ingest one incoming video from the pinned conversation:
// click card -> capture media url -> download -> audio + frames + transcript -> inbox/<id>/
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import ffmpegPath from 'ffmpeg-static';
import { connect, openPinned, messages, videoSig, captureMediaUrl, scrollToBottom } from './douyin.mjs';
import { transcribe } from './asr.mjs';

const run = promisify(execFile);
const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36';

export async function downloadMedia(url, dest) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 120000);
  const resp = await fetch(url, {
    headers: { 'User-Agent': UA, Referer: 'https://www.douyin.com/', Accept: '*/*' },
    signal: ac.signal,
  });
  clearTimeout(t);
  if (!resp.ok) throw new Error(`download failed ${resp.status}`);
  const buf = Buffer.from(await resp.arrayBuffer());
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, buf);
  return buf.length;
}

export async function extractAssets(videoPath, dir) {
  fs.mkdirSync(path.join(dir, 'frames'), { recursive: true });
  await run(ffmpegPath, ['-hide_banner', '-loglevel', 'error', '-y', '-i', videoPath, '-vn', '-t', '90', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', path.join(dir, 'audio.wav')], { timeout: 90000 });
  const probe = await run(ffmpegPath, ['-hide_banner', '-i', videoPath]).catch((e) => ({ stderr: e.stderr || '' }));
  const m = /Duration: (\d+):(\d+):(\d+\.\d+)/.exec(probe.stderr || '');
  const dur = m ? +m[1] * 3600 + +m[2] * 60 + +m[3] : 30;
  const fps = `fps=1/${Math.max(2, dur / 6).toFixed(2)}`;
  await run(ffmpegPath, ['-hide_banner', '-loglevel', 'error', '-y', '-i', videoPath, '-vf', `${fps},scale=540:-2`, '-frames:v', '6', path.join(dir, 'frames', 'f%02d.jpg')], { timeout: 90000 });
  return { duration: dur };
}

/**
 * 长视频友好的资源抽取：音频按 9 分钟切段（参考 douyin-mcp-server 的做法），
 * 避免整段长视频一次性喂给模型。返回 { duration, segments, frames }。
 * 注意：保留原 extractAssets 不动，老链路 auto-all.mjs 继续用 audio.wav。
 */
export async function extractAssetsLong(videoPath, dir, segmentSeconds = 540) {
  fs.mkdirSync(path.join(dir, 'frames'), { recursive: true });
  await run(ffmpegPath, ['-hide_banner', '-loglevel', 'error', '-y', '-i', videoPath,
    '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
    '-f', 'segment', '-segment_time', String(segmentSeconds),
    path.join(dir, 'audio_%03d.wav')], { timeout: 600000 });

  const probe = await run(ffmpegPath, ['-hide_banner', '-i', videoPath]).catch((e) => ({ stderr: e.stderr || '' }));
  const m = /Duration: (\d+):(\d+):(\d+\.\d+)/.exec(probe.stderr || '');
  const dur = m ? +m[1] * 3600 + +m[2] * 60 + +m[3] : 30;
  const fps = `fps=1/${Math.max(2, dur / 6).toFixed(2)}`;
  await run(ffmpegPath, ['-hide_banner', '-loglevel', 'error', '-y', '-i', videoPath,
    '-vf', `${fps},scale=540:-2`, '-frames:v', '6', path.join(dir, 'frames', 'f%02d.jpg')], { timeout: 90000 });

  const segments = fs.readdirSync(dir)
    .filter((f) => /^audio_\d+\.wav$/.test(f))
    .sort()
    .map((f) => path.join(dir, f));
  return { duration: dur, segments, frames: fs.readdirSync(path.join(dir, 'frames')).sort() };
}

/** Main entry: ingest the newest incoming video card. */
export async function ingest() {  const { browser, page } = await connect();
  try {
    const convName = await openPinned(page);
    await scrollToBottom(page);
    const msgs = await messages(page);
    const incoming = msgs.filter((x) => x.kind === 'video' && !x.fromMe);
    if (!incoming.length) throw new Error('no incoming video in pinned conversation');
    const newest = incoming[incoming.length - 1];
    const sig = videoSig(newest);

    const cards = page.locator('.MessageItemShareAwemecontainer');
    const all = await messages(page);
    // Map msg index -> card ordinal among video cards.
    let ordinal = 0;
    for (const m of all) {
      if (m.kind !== 'video') continue;
      if (m.idx === newest.idx) break;
      ordinal++;
    }

    const url = await captureMediaUrl(page, cards.nth(ordinal));
    if (!url) throw new Error('media url not captured');

    // The inline player also shows the caption + top comments: cheap extra context.
    const overlayText = await page
      .evaluate(() => {
        const sel = ['[class*="videoDetail"]', '[class*="playerContainer"]', '[class*="modal"]', '[class*="VideoDetail"]'];
        for (const s of sel) {
          const el = document.querySelector(s);
          if (el && el.innerText && el.innerText.trim().length > 10) return el.innerText.trim().slice(0, 900);
        }
        return '';
      })
      .catch(() => '');
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(800);

    const id = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const dir = path.resolve('./inbox', id);
    fs.mkdirSync(dir, { recursive: true });
    const videoPath = path.join(dir, 'video.mp4');
    const bytes = await downloadMedia(url, videoPath);
    const { duration } = await extractAssets(videoPath, dir);
    const { text } = await transcribe(path.join(dir, 'audio.wav'));

    const meta = { id, convName, sig, author: newest.card?.author ?? '', cardText: newest.text, mediaUrl: url, bytes, duration, transcript: text, overlayText };
    fs.writeFileSync(path.join(dir, 'meta.json'), JSON.stringify(meta, null, 2));
    fs.writeFileSync(path.join(dir, 'transcript.txt'), text);
    return { dir, meta };
  } finally {
    await browser.close().catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  ingest()
    .then(({ dir, meta }) => {
      console.log('ingested ->', dir);
      console.log(`duration=${meta.duration.toFixed(1)}s author=${meta.author}`);
      console.log('transcript:', meta.transcript.slice(0, 400));
    })
    .catch((e) => {
      console.error('FAILED:', e.message);
      process.exit(1);
    });
}
