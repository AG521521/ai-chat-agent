// Send (or dry-run) a reply into the pinned conversation.
import fs from 'node:fs';
import path from 'node:path';
import { connect, openPinned, sendReply, messages } from './douyin.mjs';

const text = process.argv[2];
const doSend = process.argv.includes('--send');
if (!text) {
  console.error('usage: node send.mjs "<text>" [--send]');
  process.exit(2);
}

const before = path.resolve('./out');
fs.mkdirSync(before, { recursive: true });

const { browser, page } = await connect();
try {
  const name = await openPinned(page);
  console.log('conversation:', name);
  if (!/白名单/.test(name)) throw new Error(`refusing to send: pinned conversation is "${name}", not 白名单`);

  const prior = await messages(page);
  const priorMine = prior.filter((m) => m.fromMe).length;

  // Screenshot the editor with the text staged, before anything is committed.
  const staged = await sendReply(page, text, { dryRun: !doSend });
  await page.screenshot({ path: path.join(before, doSend ? 'sent.png' : 'staged.png') });
  console.log('staged:', JSON.stringify(staged.typed));

  if (doSend) {
    if (!staged.sent) {
      console.log('NOT SENT - editor still holds:', JSON.stringify(staged.remaining));
      process.exit(1);
    }
    await page.waitForTimeout(1500);
    const after = await messages(page);
    const newest = after[after.length - 1];
    console.log('after send, newest bubble:', JSON.stringify({ fromMe: newest.fromMe, text: newest.text.slice(0, 80) }));
    await page.screenshot({ path: path.join(before, 'after-send.png') });
    console.log(`mine before=${priorMine} after=${after.filter((m) => m.fromMe).length}`);
  } else {
    // dry run: leave the conversation exactly as we found it
    const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
    await editor.click();
    await page.keyboard.press('Control+A');
    await page.keyboard.press('Delete');
    console.log('dry-run: editor cleared, nothing sent');
  }
} finally {
  await browser.close().catch(() => {});
}
