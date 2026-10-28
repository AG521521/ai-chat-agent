import { chromium } from 'playwright-core';
const b = await chromium.connectOverCDP('http://127.0.0.1:9222');
const ctx = b.contexts()[0];
const page = ctx.pages().find((p) => p.url().includes('/chat'));
await page.bringToFront();
const st = await page.evaluate(() => {
  const box = document.querySelector('[data-e2e="msg-input"]');
  const nodes = [...document.querySelectorAll('[data-e2e="msg-item-content"]')];
  const last = nodes.slice(-3).map((n) => ({
    mine: !!n.querySelector('.MessageBoxRefContainerisFromMe') || /isFromMe/.test(n.innerHTML),
    ref: !!n.querySelector('[class*=refContainer]'),
    t: (n.innerText || '').replace(/\n/g, ' ').slice(0, 40),
  }));
  return { composer: (box?.innerText || '').replace(/\n/g, '|').slice(0, 80), hasQuote: !!box?.querySelector('.MsgInputReferenceclose'), last };
});
console.log(JSON.stringify(st, null, 1));
await page.screenshot({ path: './out/verify-send.png' });
await b.close();
