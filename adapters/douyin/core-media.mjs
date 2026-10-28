// 抖音媒体理解：点开卡片 → 抓直链 → 下载 → 抽帧 + 音频分段 → OCR 字幕 + 语音转写。
// 复用现有 ingest.mjs / asr.mjs，不重写旧的 auto-all 链路。
import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { captureMediaUrl, closeOverlay } from './douyin.mjs';
import { downloadMedia, extractAssetsLong } from './ingest.mjs';
import { transcribe } from './asr.mjs';

const run = promisify(execFile);

export function coverHash(msg) {
  const cover = msg?.card?.cover || '';
  const hit = cover.match(/([0-9a-f]{16,})/);
  return hit ? hit[1] : '';
}

async function ocrFile(file) {
  try {
    const { stdout } = await run(
      'powershell.exe',
      ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', './ocr.ps1', '-Path', file],
      { encoding: 'utf8', timeout: 120000 },
    );
    return stdout.replace(/(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])/g, '').trim();
  } catch {
    return '';
  }
}

/** 只保留"像人话"的字幕行：至少 4 个汉字、汉字占比够高，丢掉英文商品名/logo 噪字。 */
function meaningfulSubtitle(line) {
  const t = (line || '').replace(/\s+/g, '');
  if (t.length < 4) return '';
  const cjk = (t.match(/[\u4e00-\u9fa5]/g) || []).length;
  if (cjk < 4 || cjk / t.length < 0.45) return '';
  return t;
}

/**
 * 理解一条视频卡片。
 * 主证据是**语音转写**（和 yzfly/douyin-mcp-server 一致），画面 OCR 只做补充，
 * 因为短视频画面里经常是商品名/英文广告，OCR 反而更脏。
 */
export async function enrichVideo(page, msg) {
  const hash = coverHash(msg);
  if (!hash) throw new Error('视频卡片没有可用封面');
  const card = page.locator(`.MessageItemShareAwemecontainer:has(img[src*="${hash}"])`).first();
  if (!(await card.count())) throw new Error('卡片已不在视图内');

  await closeOverlay(page).catch(() => {});
  const url = await captureMediaUrl(page, card);
  if (!url) throw new Error('没抓到媒体直链');

  const dir = path.resolve('./inbox', `core-${hash.slice(0, 10)}`);
  fs.mkdirSync(dir, { recursive: true });
  await downloadMedia(url, path.join(dir, 'video.mp4'));
  const { segments, frames, duration } = await extractAssetsLong(path.join(dir, 'video.mp4'), dir);

  // 1) 语音转写（主）
  const parts = [];
  for (const seg of segments.slice(0, 6)) {
    try {
      const { text } = await transcribe(seg);
      if (text && text.trim()) parts.push(text.trim());
    } catch {
      /* 单段失败不影响其它段 */
    }
  }
  const speech = parts.join('\n');

  // 2) 画面字幕（补充，只留像人话的）
  const subs = [];
  for (const f of frames) {
    const t = meaningfulSubtitle(await ocrFile(path.join(dir, 'frames', f)));
    if (t && !subs.includes(t)) subs.push(t);
  }

  const summary = [
    speech ? `语音：${speech}` : '',
    subs.length ? `画面字幕：${subs.slice(0, 6).join(' / ')}` : '',
  ].filter(Boolean).join('\n').slice(0, 800);

  return {
    summary,
    quoteTarget: hash,
    dir,
    ocrChars: subs.join('').length,
    speechChars: speech.length,
    segments: segments.length,
    durationSec: Math.round(duration || 0),
  };
}
