// Quote-reply to video cards: locate by cover hash (bidirectional scroll), skip already-sent,
// hover-free right-click on the card itself, verify via elementFromPoint and quote chip.
import fs from 'node:fs';
import { connect, openPinned, scrollToBottom } from './douyin.mjs';

const jobs = JSON.parse(fs.readFileSync(process.argv[2] || './out/replies.json', 'utf8'));
const DRY = process.env.DRY === '1';
const LEDGER = './sent.json';
const ledger = fs.existsSync(LEDGER) ? JSON.parse(fs.readFileSync(LEDGER, 'utf8')) : {};

const { browser, page } = await connect();
const log = (...a) => console.log(...a);

try {
  await openPinned(page);
  await scrollToBottom(page);
  const pane = page.locator('.messageMessageListlist').first();

  const findCard = async (hash) => {
    const sel = page.locator(`.MessageItemShareAwemecontainer:has(img[src*="${hash}"])`).first();
    const visible = async () => (await sel.count()) && !!(await sel.boundingBox());
    if (await visible()) return sel;

    // Downwards first (newer), then upwards (older).
    for (const dir of [1, -1]) {
      for (let i = 0; i < 20; i++) {
        const edge = await pane.evaluate((el, d) => {
          el.scrollTop = Math.max(0, Math.min(el.scrollHeight, el.scrollTop + d * 500));
          return d > 0 ? el.scrollTop + el.clientHeight >= el.scrollHeight - 2 : el.scrollTop <= 1;
        }, dir);
        await page.waitForTimeout(350);
        if (await visible()) return sel;
        if (edge) break;
      }
    }
    return null;
  };

  for (const job of jobs) {
    if (ledger[job.hash]) {
      log(`SKIP ${job.hash.slice(0, 8)} already sent: ${ledger[job.hash].slice(0, 20)}`);
      continue;
    }
    const card = await findCard(job.hash);
    if (!card) {
      log(`MISS ${job.hash.slice(0, 8)} (${job.author || '?'}) not found after bidirectional scan`);
      continue;
    }
    await card.scrollIntoViewIfNeeded().catch(() => {});
    await page.waitForTimeout(250);
    const b = await card.boundingBox();
    const cx = b.x + b.width / 2;
    const cy = b.y + b.height / 2;
    const onCard = await page.evaluate(
      ([x, y]) => !!document.elementFromPoint(x, y)?.closest('.MessageItemShareAwemecontainer'),
      [cx, cy],
    );
    if (!onCard) {
      log(`MISS ${job.hash.slice(0, 8)}: pointer off card (${Math.round(cx)},${Math.round(cy)})`);
      continue;
    }

    await card.click({ button: 'right', position: { x: b.width / 2, y: b.height / 2 }, timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(900);

    const replyItem = page.getByText('回复', { exact: true }).first();
    if (!(await replyItem.count())) {
      log(`MISS ${job.hash.slice(0, 8)}: no 回复 menu`);
      await page.keyboard.press('Escape').catch(() => {});
      continue;
    }
    await replyItem.click({ timeout: 5000 }).catch(() => {});

    const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
    await editor.waitFor({ state: 'visible', timeout: 8000 });
    const quoted = (await page.evaluate(() => document.querySelector('[data-e2e="msg-input"]')?.innerText || '')).includes('回复消息');
    if (!quoted) {
      log(`MISS ${job.hash.slice(0, 8)}: no quote chip, aborting this one`);
      await page.keyboard.press('Escape').catch(() => {});
      continue;
    }

    await editor.click();
    await page.keyboard.insertText(job.text);
    await page.waitForTimeout(400);

    if (DRY) {
      log(`DRY ${job.hash.slice(0, 8)} quoted=${quoted} ${JSON.stringify(job.text)}`);
      await page.keyboard.press('Control+A');
      await page.keyboard.press('Delete');
      await page.keyboard.press('Escape');
      continue;
    }

    await page.keyboard.press('Enter');
    await page.waitForTimeout(1800);
    const left = (await editor.innerText()).trim();
    const preview = (await page.locator('[data-e2e="conversation-item"]').first().innerText().catch(() => '')).replace(/\s+/g, '');
    const ok = left.length < 2 && preview.includes(job.text.replace(/\s+/g, '').slice(0, 6));
    log(`${ok ? 'SENT' : 'UNSURE'} ${job.hash.slice(0, 8)} ${JSON.stringify(job.text)}`);
    if (ok) {
      ledger[job.hash] = job.text;
      fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
      await page.screenshot({ path: `./out/sent-${job.hash.slice(0, 8)}.png` });
    }
  }
} finally {
  await browser.close().catch(() => {});
}
