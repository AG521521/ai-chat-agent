// One-click entry: make sure the automation Chrome is up, then collect today's videos
// that still need a reply and write them to out/pending.json.
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { chromium } from 'playwright-core';
import { connect, openPinned, scrollToBottom } from './douyin.mjs';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PROFILE = path.resolve('./chrome-profile');

async function ensureChrome() {
  try {
    const r = await fetch('http://127.0.0.1:9222/json/version', { signal: AbortSignal.timeout(1500) });
    if (r.ok) return 'already running';
  } catch {}
  spawn(
    CHROME,
    ['--remote-debugging-port=9222', `--user-data-dir=${PROFILE}`, '--no-first-run', '--no-default-browser-check', '--disable-blink-features=AutomationControlled', '--window-size=1500,1000', 'https://www.douyin.com/chat?isPopup=1'],
    { detached: true, stdio: 'ignore' },
  ).unref();
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const r = await fetch('http://127.0.0.1:9222/json/version', { signal: AbortSignal.timeout(1500) });
      if (r.ok) return 'started';
    } catch {}
  }
  throw new Error('Chrome 没起来，检查端口 9222');
}

const ledgerPath = './sent.json';
const ledger = fs.existsSync(ledgerPath) ? JSON.parse(fs.readFileSync(ledgerPath, 'utf8')) : {};

console.log('[1/3]', await ensureChrome());

const { browser, page } = await connect();
try {
  const conv = await openPinned(page);
  console.log('[2/3] 会话:', conv);
  await scrollToBottom(page);

  const pane = page.locator('.messageMessageListlist').first();
  const seen = new Map();
  for (let i = 0; i < 30; i++) {
    const batch = await page.evaluate(() => {
      const half = window.innerWidth / 2;
      return [...document.querySelectorAll('.MessageItemShareAwemecontainer')].map((el) => {
        const r = el.getBoundingClientRect();
        const cover = el.querySelector('img.MessageItemShareAwemeawemeContainer')?.getAttribute('src') || '';
        return {
          mine: r.x > half,
          author: el.querySelector('.MessageItemShareAwemeauthorName')?.textContent?.trim() || '',
          hash: (cover.match(/([0-9a-f]{16,})/) || [null, cover.slice(-24)])[1],
        };
      });
    });
    for (const v of batch) if (!seen.has(v.hash)) seen.set(v.hash, v);
    const top = await pane.evaluate((el) => {
      el.scrollTop = Math.max(0, el.scrollTop - 600);
      return el.scrollTop <= 1;
    });
    await page.waitForTimeout(400);
    if (top) break;
  }

  const pending = [...seen.values()].filter((v) => !v.mine && !ledger[v.hash]);
  fs.mkdirSync('./out', { recursive: true });
  fs.writeFileSync('./out/pending.json', JSON.stringify(pending, null, 2));
  console.log(`[3/3] 待回复视频 ${pending.length} 条，已写入 out/pending.json`);
  for (const p of pending.slice(0, 20)) console.log('   -', p.author, p.hash.slice(0, 8));
} finally {
  await browser.close().catch(() => {});
}
