// Douyin web IM adapter: pins down selectors, reads the pinned conversation, sends text.
import { chromium } from 'playwright-core';

export const CDP = 'http://127.0.0.1:9222';

export async function connect() {
  const browser = await chromium.connectOverCDP(CDP);
  const ctx = browser.contexts()[0];
  let page = ctx.pages().find((p) => p.url().includes('/chat'));
  let created = false;
  if (!page) {
    page = await ctx.newPage();
    await page.goto('https://www.douyin.com/chat?isPopup=1', { waitUntil: 'domcontentloaded' });
    created = true;
  }
  await page.waitForTimeout(1200);
  await page.bringToFront().catch(() => {});
  await closeOverlay(page);
  return { browser, ctx, page, created };
}

/** The inline video player opens an overlay that swallows clicks. Clear it. */
export async function closeOverlay(page) {
  const stillOpen = () =>
    page
      .evaluate(() => {
        const v = [...document.querySelectorAll('video')].find((x) => (x.src || '').startsWith('blob:') && x.offsetWidth > 200);
        return !!v || !!document.querySelector('#pc-im-popup-container.isAweme, .commonAllPopupContainer.isAweme');
      })
      .catch(() => false);

  if (!(await stillOpen())) return false;
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(700);
  if (!(await stillOpen())) return true;

  // Nuke the transient player container; the conversation itself is untouched.
  await page.evaluate(() => {
    for (const el of document.querySelectorAll('#pc-im-popup-container, .commonAllPopupContainer')) {
      if (el.classList.contains('isAweme') || el.querySelector('video')) el.remove();
    }
    for (const v of document.querySelectorAll('video')) {
      if ((v.src || '').startsWith('blob:')) {
        let el = v;
        for (let i = 0; i < 7 && el.parentElement && el.parentElement !== document.body; i++) el = el.parentElement;
        if (el.querySelector('video') && el.getBoundingClientRect().width > 300) el.remove();
      }
    }
  });
  await page.waitForTimeout(600);
  return true;
}

export async function conversations(page) {
  const items = page.locator('[data-e2e="conversation-item"]');
  const n = await items.count();
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = (await items.nth(i).innerText().catch(() => '')).trim();
    out.push({ i, name: t.split('\n')[0], raw: t.replace(/\n/g, ' | ').slice(0, 140) });
  }
  return out;
}

/** The whitelist target: the first (pinned) conversation. */
export async function openPinned(page) {
  const items = page.locator('[data-e2e="conversation-item"]');
  await items.first().click();
  await page.waitForTimeout(1500);
  const name = (await items.first().innerText().catch(() => '')).trim().split('\n')[0];
  return name;
}

/** Scroll the message pane to the newest message so the latest cards are rendered. */
export async function scrollToBottom(page) {
  for (let round = 0; round < 30; round++) {
    const rect = await page.evaluate(() => {
      const el = document.querySelector('.messageMessageListlist');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, st: Math.round(el.scrollTop) };
    }).catch(() => null);
    if (!rect) { await page.waitForTimeout(300); continue; }
    await page.mouse.move(rect.x, rect.y);
    await page.mouse.wheel(0, 900);
    await page.waitForTimeout(180);
    const st = await page.evaluate(() => Math.round(document.querySelector('.messageMessageListlist')?.scrollTop ?? 0)).catch(() => 0);
    if (st === rect.st) {
      await page.mouse.wheel(0, 900);
      await page.waitForTimeout(180);
      const st2 = await page.evaluate(() => Math.round(document.querySelector('.messageMessageListlist')?.scrollTop ?? 0)).catch(() => 0);
      if (st2 === st) break; // two stable readings => really at the newest end
    }
  }
}

/** All messages rendered in the open conversation, ordered top -> bottom (newest last). */
export async function messages(page) {
  const raw = await page.evaluate(() => {
    const nodes = [...document.querySelectorAll('[data-e2e="msg-item-content"]')];
    return nodes.map((el, idx) => {
      const fromMe = !!el.querySelector('.MessageBoxRefContainerisFromMe') || !!el.closest('.MessageBoxRefContainerisFromMe');
      let kind = 'text';
      let card = null;
      const aweme = el.querySelector('.MessageItemShareAwemecontainer');
      const comment = el.querySelector('.MessageItemCommentSharecontainer');
      if (aweme) {
        kind = 'video';
        card = {
          author: el.querySelector('.MessageItemShareAwemeauthorName')?.textContent?.trim() ?? '',
          cover: el.querySelector('img')?.getAttribute('src') ?? '',
        };
      } else if (comment) {
        kind = 'comment';
      } else if (el.querySelector('img')) {
        kind = 'image';
      } else if (el.querySelector('.lynx-card__ref, [data-tag="lynx-card"]')) {
        kind = 'card';
      }
      const r = el.getBoundingClientRect();
      return { idx, y: Math.round(r.top), fromMe, kind, text: (el.innerText || '').trim().slice(0, 300), card };
    });
  });
  // Visual order, not DOM order: the pane renders a reversed list.
  return raw.sort((a, b) => a.y - b.y).map((m, i) => ({ ...m, idx: i }));
}

/** Signature for a video card, stable across re-renders and list shifts. */
export function videoSig(m) {
  const cover = m.card?.cover || '';
  const hash = (cover.match(/([0-9a-f]{16,})/) || [null, cover.slice(-32)])[1];
  return `${hash}:${m.card?.author ?? ''}`;
}

/**
 * Click a video card and capture the media URL the inline player requests.
 * Returns the douyinvod URL, or null.
 */
export async function captureMediaUrl(page, cardLocator) {
  const seen = [];
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  const onReq = (e) => {
    const u = e.request.url;
    if (/douyinvod|\.mp4|m3u8/.test(u)) seen.push(u);
  };
  cdp.on('Network.requestWillBeSent', onReq);

  await cardLocator.scrollIntoViewIfNeeded().catch(() => {});
  await cardLocator.click({ timeout: 10000 });

  // Wait for playback to actually start requesting media.
  for (let i = 0; i < 30 && !seen.length; i++) await page.waitForTimeout(500);
  const url = seen.find((u) => /douyinvod/.test(u) && !/media-audio/.test(u)) || seen[0] || null;

  cdp.off('Network.requestWillBeSent', onReq);
  await cdp.detach().catch(() => {});
  return url;
}

/** Type a reply and send it. Set dryRun to type without sending. */
export async function sendReply(page, text, { dryRun = false } = {}) {
  const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
  await editor.waitFor({ state: 'visible', timeout: 10000 });
  await editor.click();
  await page.waitForTimeout(200);

  // Slate editor: use the browser's input pipeline so listeners fire.
  await page.keyboard.insertText(text);
  await page.waitForTimeout(400);

  const typed = (await editor.innerText()).trim();
  if (dryRun) return { sent: false, typed };
  if (typed.length < 2) return { sent: false, typed, error: 'text did not land in editor' };

  await page.keyboard.press('Enter');
  await page.waitForTimeout(1800);

  const remaining = (await editor.innerText()).trim();
  // The conversation-list preview is the most reliable "it really went out" signal.
  const preview = (await page.locator('[data-e2e="conversation-item"]').first().innerText().catch(() => '')).replace(/\s+/g, '');
  const probe = text.replace(/\s+/g, '').slice(0, 8);
  return { sent: remaining.length < 2 && preview.includes(probe), typed, remaining, preview: preview.slice(0, 60) };
}
