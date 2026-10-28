// 抖音发送：引用回复 / 纯文本，带发送前后校验。默认 dry-run，不按回车。
// 逻辑沿用 auto-all.mjs 里已经跑通的部分（右键→回复→校验引用条→输入→发送→核验），
// 但独立成模块，不碰老链路。
import { closeOverlay } from './douyin.mjs';

const SEND_BTN = '.messageMsgInputpublishBtn, .e2e-send-msg-btn';

async function findCard(page, hash) {
  const sel = page.locator(`.MessageItemShareAwemecontainer:has(img[src*="${hash}"])`).first();
  const visible = async () => (await sel.count()) > 0 && Boolean(await sel.boundingBox());
  if (await visible()) return sel;
  const pane = page.locator('.messageMessageListlist').first();
  for (const dir of [1, -1]) {
    for (let i = 0; i < 20; i += 1) {
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
}

async function clearComposer(page) {
  const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
  await editor.click().catch(() => {});
  await page.keyboard.press('Control+A').catch(() => {});
  await page.keyboard.press('Delete').catch(() => {});
  const close = page.locator('.MsgInputReferenceclose').first();
  if (await close.count()) {
    await close.click({ force: true }).catch(() => {});
    await page.waitForTimeout(200);
  }
  await page.waitForTimeout(150);
}

/**
 * @returns {Promise<{status:'verified'|'unknown'|'failed'|'aborted'|'dry_run', stage:string, detail?:string, evidence?:object}>}
 */
export async function sendReply(page, { hash = '', text, dryRun = false }) {
  if (!text || !text.trim()) return { status: 'aborted', stage: 'empty_text' };
  await closeOverlay(page).catch(() => {});
  await page.waitForTimeout(300);

  // 先清空输入框（含可能残留的引用框）。注意：引用建立之后**绝对不能**再清，
  // 否则会把引用条一起点掉，发出去就变成普通文本 —— 之前踩过这个坑。
  await clearComposer(page);

  let quoted = false;
  if (hash) {
    const card = await findCard(page, hash);
    if (!card) return { status: 'aborted', stage: 'card_not_found', detail: '滚动后仍找不到目标卡片' };
    const html = await card.evaluate((n) => n.innerHTML || '').catch(() => '');
    if (!html.includes(hash)) return { status: 'aborted', stage: 'target_mismatch', detail: '卡片身份与目标 hash 不符' };
    await card.scrollIntoViewIfNeeded().catch(() => {});
    await page.waitForTimeout(250);
    const b = await card.boundingBox();
    if (!b) return { status: 'aborted', stage: 'no_box' };
    const cx = b.x + b.width / 2;
    const cy = b.y + b.height / 2;
    const onCard = await page.evaluate(
      ([x, y]) => Boolean(document.elementFromPoint(x, y)?.closest('.MessageItemShareAwemecontainer')),
      [cx, cy],
    );
    if (!onCard) return { status: 'aborted', stage: 'pointer_off_card', detail: `${Math.round(cx)},${Math.round(cy)}` };

    await card.click({ button: 'right', position: { x: b.width / 2, y: b.height / 2 }, timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(900);
    const replyItem = page.getByText('回复', { exact: true }).first();
    if (!(await replyItem.count())) return { status: 'aborted', stage: 'no_menu' };
    await replyItem.click({ timeout: 5000 }).catch(() => {});

    const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
    await editor.waitFor({ state: 'visible', timeout: 8000 });
    quoted = (await page.evaluate(() => document.querySelector('[data-e2e="msg-input"]')?.innerText || '')).includes('回复消息');
    if (!quoted) return { status: 'aborted', stage: 'no_quote_chip', detail: '输入框没有出现引用标示' };
  }

  const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
  await editor.waitFor({ state: 'visible', timeout: 8000 });
  await editor.click();
  await page.keyboard.insertText(text);
  await page.waitForTimeout(450);

  const typed = (await editor.innerText()).trim();
  if (typed.length < 2) return { status: 'failed', stage: 'type_failed', detail: '文字没进输入框' };

  if (hash) {
    const stillQuoted = (await page.evaluate(
      () => document.querySelector('[data-e2e="msg-input"]')?.innerText || '',
    )).includes('回复消息');
    if (!stillQuoted) {
      await clearComposer(page);
      return { status: 'aborted', stage: 'quote_lost_after_typing', detail: '输入过程中引用条消失' };
    }
  }

  if (dryRun) {
    await clearComposer(page);
    await page.keyboard.press('Escape').catch(() => {});
    return {
      status: 'dry_run', stage: 'staged', quoted,
      evidence: { typed_len: typed.length, quoted },
    };
  }

  await page.keyboard.press('Enter');
  await page.waitForTimeout(2200);
  let left = (await editor.innerText()).trim();
  if (left.length > 1) {
    await page.locator(SEND_BTN).first().click({ force: true }).catch(() => {});
    await page.waitForTimeout(2200);
    left = (await editor.innerText()).trim();
  }

  const probe = text.replace(/\s+/g, '').slice(0, 6);
  const appeared = await page.evaluate(({ p, needQuote }) => {
    const nodes = [...document.querySelectorAll('[data-e2e="msg-item-content"]')];
    return nodes.some((n) => {
      const t = (n.innerText || '').replace(/\s+/g, '');
      if (!t.includes(p)) return false;
      if (!needQuote) return true;
      // 类名大小写两种都试：MessageBoxRefContainerisFromMe / ...refContainer
      return Boolean(n.querySelector('[class*="RefContainer"], [class*="refContainer"]'));
    });
  }, { p: probe, needQuote: Boolean(hash) });

  if (left.length > 1) {
    await clearComposer(page);
    return { status: 'failed', stage: 'editor_holds_text', detail: '发送后输入框仍有内容' };
  }
  return {
    status: appeared ? 'verified' : 'unknown',
    stage: 'sent',
    detail: appeared
      ? (hash ? '出现带引用条的气泡' : '出现含回复文本的气泡')
      : '输入框已清空但没看到气泡',
    evidence: { quoted, input_cleared: true, bubble_seen: appeared, expected_quote: Boolean(hash) },
  };
}

/** 在页面里设置"是否要求气泡带引用条"（引用回复时用）。 */
export async function expectQuote(page, value) {
  await page.evaluate((v) => { window.__expectQuote = v; }, value).catch(() => {});
}
