// Reply to every kind of her message: text, image, video, 图文. Same quoted-reply mechanism.
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { connect, openPinned, scrollToBottom, captureMediaUrl, closeOverlay } from './douyin.mjs';
import { downloadMedia, extractAssets } from './ingest.mjs';
import { transcribe } from './asr.mjs';

const run = promisify(execFile);
const cfg = JSON.parse(fs.readFileSync('./config.json', 'utf8'));
const SETTINGS = JSON.parse(fs.readFileSync('./settings.json', 'utf8'));
const HER = fs.existsSync('./her-schedule.json') ? JSON.parse(fs.readFileSync('./her-schedule.json', 'utf8')) : null;
function studyNow() {
  const d = new Date();
  const h = d.getHours() + d.getMinutes() / 60;
  return SETTINGS.studySlots.some((slot) => {
    const [x, y] = slot.split('-').map(Number);
    return h >= x && h < y;
  });
}
function herNow() {
  if (!HER) return { state: 'unknown', hint: '' };
  const d = new Date();
  const hm = d.getHours() + d.getMinutes() / 60;
  const iso = d.toISOString().slice(0, 10);
  const slots = HER.days?.[iso] || Object.values(HER.days || {})[0] || [];
  const toMin = (t) => Number(t.slice(0, 2)) + Number(t.slice(3, 5)) / 60;
  for (const sl of slots) {
    if (hm >= toMin(sl.start) && hm < toMin(sl.end)) {
      return { state: 'class', hint: '她正在上「' + sl.subject + '」这节课，回简短一句就好，别拉长对话、别问要长篇回答的问题。' };
    }
  }
  for (const g of HER.freeGaps || []) {
    if (hm >= toMin(g.start) && hm < toMin(g.end)) {
      return { state: 'free', hint: '她这会有空（' + g.note + '），可以正常聊，多问一句她今天的情况。' };
    }
  }
  return { state: 'after', hint: '她今天课已经上完或者还没开始，可以正常回。' };
}
const LEDGER = './sent.json';
const RETRIES = fs.existsSync('./retries.json') ? JSON.parse(fs.readFileSync('./retries.json', 'utf8')) : {};
const ledger = fs.existsSync(LEDGER) ? JSON.parse(fs.readFileSync(LEDGER, 'utf8')) : {};
const DRY = process.env.DRY === '1';
const MAX = Number(process.env.MAX || 30);
const KINDS = (process.env.KINDS || 'text,image,video,photos').split(',');
const log = (...a) => console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...a);

async function ocrFile(file) {
  try {
    const { stdout } = await run('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', './ocr.ps1', '-Path', file], { encoding: 'utf8' });
    return stdout.replace(/(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])/g, '').trim();
  } catch {
    return '';
  }
}

/** Everything visible that she sent, with a stable key for re-locating it. */
const scan = (page) =>
  page.evaluate(() => {
    const half = window.innerWidth / 2;
    const nodes = [...document.querySelectorAll('[data-e2e="msg-item-content"]')].sort(
      (a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top,
    );
    return nodes.map((el, i) => {
      const r = el.getBoundingClientRect();
      const below = nodes[i + 1];
      const belowRect = below?.getBoundingClientRect();
      const belowFromMe = !!belowRect && belowRect.left + belowRect.width / 2 > (el.closest('.messageMessageListlist')?.getBoundingClientRect().x || 0) + (el.closest('.messageMessageListlist')?.getBoundingClientRect().width || window.innerWidth) / 2;
      const paneRect = el.closest('.messageMessageListlist')?.getBoundingClientRect();
      const centerX = paneRect ? paneRect.x + paneRect.width / 2 : window.innerWidth / 2;
      const fromMe = r.left + r.width / 2 > centerX;
      const card = el.querySelector('.MessageItemShareAwemecontainer');
      const coverImg = card?.querySelector('img.MessageItemShareAwemeawemeContainer') || card?.querySelector('img');
      const cover = coverImg?.getAttribute('src') || '';
      let kind = 'text';
      if (card) kind = card.querySelector('img.MessageItemShareAwemephotosTag') ? 'photos' : 'video';
      else if (el.querySelector('.MessageItemCommentSharecontainer')) kind = 'comment';
      else if (el.querySelector('img')) kind = 'image';
      const img = el.querySelector('img')?.getAttribute('src') || '';
      const txt = (el.innerText || '').trim();
      const quotesBack = !!el.querySelector('[class*=refContainer], [class*=MessageBoxRefContainer]');
      const mention = /^@/.test(txt);
      const bot =
        kind === 'card' ||
        mention ||
        quotesBack ||
        /喂精灵|点亮|火花|接受邀请|纪念一下|邀请你|快捷|系统/.test(txt) ||
        !!el.querySelector('.BulletGeneralCardcontainer');
      const id = 'm' + i;
      el.setAttribute('data-dsh-id', id);
      return {
        id,
        cover,
        author: el.querySelector('.MessageItemShareAwemeauthorName')?.textContent?.trim() || '',
        kind,
        bot,
        conflict: /(是不是|到底|凭什么|骗|发错|给谁|分手|拉黑|不理我|解释一下|承认|又.*不理|复制|没看|敷衍|机器人|自动回复|谁发的|你自己写)/.test(txt),
        emoji: /emoji|Emoji|sticker|Sticker/.test(el.innerHTML) || /^\[[^\]]{1,6}\]$/.test(txt),
        fromMe,
        text: (el.innerText || '').trim().slice(0, 400),
        hash: (() => {
          const a = cover.match(/image-cut-tos-priv\/([0-9a-f]{32})/);
          if (a) return a[1];
          const b = cover.match(/tos-cn-[a-z0-9-]+\/([A-Za-z0-9]{16,})/);
          if (b) return b[1];
          if (cover) { const p = cover.split('?')[0]; let x = 0; for (let i = 0; i < p.length; i++) x = (x * 31 + p.charCodeAt(i)) >>> 0; return 'src' + x.toString(36); }
          return img.slice(-24);
        })(),
        visible: r.top > 40 && r.bottom < window.innerHeight - 20,
        lastOfBurst: kind !== 'text' || !below || belowFromMe || !(below.innerText || '').trim(),
        replied: [nodes[i + 1], nodes[i + 2], nodes[i + 3]].some((n) => n && n.querySelector('.MessageBoxRefContainerisFromMe')),
      };
    });
  });

const SYSTEM = `你是一个22岁男生的聊天替身，正在回复女朋友发来的消息。你在杭州钱塘，他在杭州桐庐集训（高三舞蹈生），你在备考研究生。两人不算远，但最近双方都在忙，所以近期不见面——是没时间，不是见不到。
她要的是被疼、被让着：多付出、多承诺（「我来」「我给你」「你别管了」），少讲道理。别让她累着、饿着、受委屈，让她练完就能歇、吃饱就能睡。形象基准：非常爱她、疼她的男朋友——这一点要让人从字里读出来，但别用「宝贝」「亲爱的」这类书面称呼，靠内容体现。温柔、体贴、稳得住。他本人的原话只作参考，不像话的地方往上修。
她发这些东西，八成不是要你评价内容，是想被宠、被惦记、被心疼。先摸她的心思，再落成一句具体的宠她。
内容乱的时候可以顺口吐槽一句（「这都什么乱糟糟的」这种是自然反应，没问题），但别停在那儿，歪到「那我去给你弄点吃的」这种宠她的方向。真正没看懂就别硬编具体细节。

先判断这条视频/图片到底想让你做什么，再回：
- 她发一个东西（毛毯、吃的、小玩意、想去的地方）→ 十有八九是「我想要/想和你一起」，要接成行动，比如「那我给你织一条」「等我给你买」「考完我带你去」，不要只评价那东西好不好。
- 她发吐槽男友/情侣的视频 → 认领 + 保证（我不会这样）。
- 她发夸人/甜的视频 → 接糖，顺着说。
- 她发自己的练功、上课、受伤 → 先关心累不累、伤到没，再给一句具体的。
- 她发吃的、用的、想去的地方 → 当成「想要」。但先分两种：
  · 做得到的（留一份饭、给她点个东西、陪她走一段）→ 接「我给你留」「我来弄」。
  · 做不到的（织毛毯、买很贵的东西、请假跑过去）→ **不许硬承诺**，用甜话接：夸她、说以后两人的日子、说等她挑/等她回来。例：「你这毛毯一裹，人肯定裹成一小团，我光想想就想抱着你。」
  · 美食/家常菜/红烧肉这类 → 她就是想让你做给她吃。接「这个我会，回头做给你」「你点菜，我照着学」，别点评好不好吃。
- 别只评价东西，也别空口承诺做不到的事。
- 拿不准就看评论和文案再判断；再拿不准，就只疼她这个人，不碰内容。
只看内容判断不出来的，宁可不说，也不要瞎猜。

规则：
1. 先接住她消息里的具体点，不要只回"哈哈""好"。
2. 短句为主，一到两句，30字以内，口语，别像客服，别土味情话。
必须从内容里抓一个**具体的词**写进回复（菜名、动作名、物品名、她说过的某个词），没有就说明你没看懂，宁可换个角度也别写「练完歇会儿」这种放谁身上都成立的话。
3. 大多数视频是在提醒"男生别这样"，那就认领+保证。
4. 不提具体日期、不承诺马上见面、不安排要花钱的事；别把话说死成见不到。
5. 不评价身材、不谈考研压力、不提双方家庭。
6. 只输出回复正文，不要解释、不要引号、不要多条。`;

async function draft(kind, content, hint) {
  const head = { video: '视频字幕/语音', text: '她说的话', image: '图片里的文字', photos: '图集里的文字', comment: '评论分享' }[kind] || '内容';
  const res = await fetch(`${cfg.baseUrl.replace(/\/$/, '')}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${cfg.apiKey}` },
    body: JSON.stringify({
      model: cfg.model,
      temperature: 0.8,
      max_tokens: 120,
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: `${hint ? hint + '\n' : ''}${head}：\n${content.slice(0, 1200)}\n\n请写一条回复。` },
      ],
    }),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${(await res.text()).slice(0, 160)}`);
  const j = await res.json();
  return (j.choices?.[0]?.message?.content || '').trim().replace(/^["'“]|["'”]$/g, '');
}

async function understand(page, msg, el, dir) {
  fs.mkdirSync(dir, { recursive: true });
  if (msg.kind === 'text' || msg.kind === 'comment') return msg.text;
  if (msg.kind === 'image') {
    const src = await el.locator('img').first().getAttribute('src');
    await downloadMedia(src, path.join(dir, 'img.jpg'));
    return await ocrFile(path.join(dir, 'img.jpg'));
  }
  if (msg.kind === 'video') {
    log('  · 关弹层');
    await page.keyboard.press('Escape').catch(() => {});
    await closeOverlay(page).catch(() => {});
    await page.waitForTimeout(500);
    log('  · 抓直链');
    const url = await captureMediaUrl(page, el.locator('.MessageItemShareAwemecontainer').first());
    log('  · 直链 ok');
    if (!url) throw new Error('no media url');
    log('  · 下载');
    await downloadMedia(url, path.join(dir, 'video.mp4'));
    log('  · 下载 ok');
    log('  · 抽帧/音频');
    await extractAssets(path.join(dir, 'video.mp4'), dir);
    log('  · ffmpeg ok');
    const subs = [];
    for (const f of fs.readdirSync(path.join(dir, 'frames')).sort()) {
      const t = await ocrFile(path.join(dir, 'frames', f));
      if (t && !subs.includes(t)) subs.push(t);
    }
    const ocrChars = subs.join('').replace(/\s/g, '').length;
    if (ocrChars >= 24) {
      log('  · 字幕够用（' + ocrChars + ' 字），跳过语音转写');
      return '字幕：\n' + subs.join('\n');
    }
    log('  · 转写');
    const { text } = await transcribe(path.join(dir, 'audio.wav'));
    log('  · 转写 ok');
    return `字幕：\n${subs.join('\n')}\n语音：\n${text}`;
  }
  if (msg.kind === 'photos') {
    await el.locator('.MessageItemShareAwemecontainer').first().click();
    await page.waitForTimeout(4000);
    const srcs = await page.evaluate(() =>
      [...document.querySelectorAll('#pc-im-popup-container img, [class*="modal"] img')]
        .map((i) => i.getAttribute('src'))
        .filter((s) => s && s.startsWith('http'))
        .slice(0, 4),
    );
    const texts = [];
    for (let i = 0; i < srcs.length; i++) {
      const f = path.join(dir, `p${i}.jpg`);
      await downloadMedia(srcs[i], f).catch(() => null);
      if (fs.existsSync(f)) {
        const t = await ocrFile(f);
        if (t) texts.push(t);
      }
    }
    await closeOverlay(page);
    if (!texts.length) throw new Error('图集里没读到文字');
    return texts.join('\n');
  }
  throw new Error('unsupported kind ' + msg.kind);
}

function locate(page, msg) {
  const all = page.locator('[data-e2e="msg-item-content"]');
  if (msg.id) return page.locator(`[data-e2e="msg-item-content"][data-dsh-id="${msg.id}"]`).first();
  if (msg.cover) {
    const byCover = all.filter({ has: page.locator(`img[src="${msg.cover.replace(/"/g, '')}"]`) }).first();
    if (byCover) return byCover;
  }
  if (msg.hash && /^[0-9a-f]{16,}$/i.test(msg.hash)) {
    const byRes = all.filter({ has: page.locator(`img[src*="${msg.hash}"]`) }).first();
    if (byRes) return byRes;
  }
  if (msg.kind === 'text' || msg.kind === 'comment') return all.filter({ hasText: msg.text.slice(0, 16) }).first();
  return all.filter({ has: page.locator(`img[src*="${msg.hash}"]`) }).first();
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
  await page.keyboard.press('Escape').catch(() => {});
  await page.waitForTimeout(200);
}

async function quoteReply(page, msg, text) {
  await closeOverlay(page).catch(() => {});
  await page.waitForTimeout(200);

  let el = locate(page, msg);
  if (!(await el.count())) {
    await page.evaluate(() => {
      const half = window.innerWidth / 2;
      [...document.querySelectorAll('[data-e2e="msg-item-content"]')]
        .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
        .forEach((n, i) => n.setAttribute('data-dsh-id', 'm' + i));
    });
    el = locate(page, msg);
  }
  if (!(await el.count())) throw new Error('target message gone');
  const verified = await el.evaluate((n, m) => {
    const txt = (n.innerText || '');
    const html = n.innerHTML || '';
    if (m.hash && /^[0-9a-f]{16,}$/i.test(m.hash) && html.includes(m.hash)) return true;
    if (m.hash && m.hash.length > 12 && html.includes(m.hash)) return true;
    if (m.kind === 'text' || m.kind === 'comment') return txt.includes(m.text.slice(0, 12));
    return false;
  }, { hash: msg.hash, kind: msg.kind, text: msg.text }).catch(() => false);
  if (!verified) throw new Error('target mismatch (元素与目标不符，拒绝右键)');
  await el.evaluate((n) => n.scrollIntoView({ block: 'center' })).catch(() => {});
  await page.waitForTimeout(350);
  const b = await el.boundingBox();
  if (!b) throw new Error('no box');

  const openMenu = async (how) => {
    await closeOverlay(page).catch(() => {});
    if (how === 'click') {
      await el.click({ button: 'right', position: { x: Math.min(b.width - 12, 40), y: Math.min(b.height - 12, 24) }, timeout: 6000 }).catch(() => {});
    } else if (how === 'edge') {
      await el.click({ button: 'right', position: { x: Math.min(b.width - 4, 6), y: 6 }, timeout: 6000 }).catch(() => {});
    } else {
      await el.evaluate((n, pt) => {
        const r = n.getBoundingClientRect();
        n.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: r.x + pt.x, clientY: r.y + pt.y, button: 2 }));
      }, { x: Math.min(b.width - 12, 40), y: Math.min(b.height - 12, 24) }).catch(() => {});
    }
    await page.waitForTimeout(800);
    return (await page.getByText('回复', { exact: true }).count()) > 0;
  };

  let ok = false;
  for (const how of ['click', 'edge', 'js']) {
    ok = await openMenu(how);
    if (ok) break;
  }
  if (!ok) {
    await page.keyboard.press('Escape').catch(() => {});
    throw new Error('no 回复 menu');
  }

  await page.getByText('回复', { exact: true }).first().click({ timeout: 5000 });
  const editor = page.locator('[data-e2e="msg-input"] [contenteditable="true"]').first();
  await editor.waitFor({ state: 'visible', timeout: 8000 });
  if (!(await page.evaluate(() => document.querySelector('[data-e2e="msg-input"]')?.innerText || '')).includes('回复消息')) {
    throw new Error('no quote chip');
  }
  const mineBefore = await page.evaluate(() =>
    [...document.querySelectorAll('[data-e2e="msg-item-content"]')]
      .filter((n) => n.querySelector('.MessageBoxRefContainerisFromMe') || /isFromMe/.test(n.innerHTML)).length).catch(() => 0);
  await clearComposer(page);
  await editor.click();
  await page.keyboard.insertText(text);
  await page.waitForTimeout(300);
  if (DRY) {
    await clearComposer(page);
    return { sent: false, dry: true };
  }
  await page.waitForTimeout(400);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(2200);
  let left = (await editor.innerText()).trim();
  if (left.length > 1) {
    // Enter sometimes leaves the draft sitting there; the send button is authoritative.
    await page.waitForTimeout(500);
    await page.locator('.messageMsgInputpublishBtn, .e2e-send-msg-btn').first().click({ force: true }).catch(() => {});
    await page.waitForTimeout(2200);
    left = (await editor.innerText()).trim();
  }
  if (left.length > 1) {
    // Don't leave a half-finished draft behind: it turns into junk text on the next try.
    await clearComposer(page);
  }
  // 编辑器空 != 已发送；以会话里真的出现了我这条消息为准
  await page.waitForTimeout(600);
  const appeared = await page.evaluate((probe) =>
    [...document.querySelectorAll('[data-e2e="msg-item-content"]')]
      .some((n) => (n.innerText || '').includes(probe) && !!n.querySelector('[class*=refContainer]')), text.slice(0, 8)).catch(() => false);
  // 输入框清空是主判据；气泡是否被看见只作诊断（虚拟列表里常常已滚出渲染窗口）
  return { sent: left.length < 2, left, appeared };
}

const usedReplies = [];
const sentMarked = (v) => typeof v === 'string' && v.length > 0 && !v.startsWith('__');
function tooSimilar(a, b) {
  const A = new Set([...a.replace(/[\s，。！？、,.!?]/g, '')]);
  const B = new Set([...b.replace(/[\s，。！？、,.!?]/g, '')]);
  let inter = 0;
  for (const ch of A) if (B.has(ch)) inter++;
  return inter / Math.min(A.size, B.size) > 0.6;
}

const { browser, page } = await connect();
let handled = 0;
try {
  await page.reload({ waitUntil: 'domcontentloaded' }).catch(() => {});
  await page.waitForTimeout(3500);
  const conv = process.env.CONV || '白名单';
  const items = page.locator('[data-e2e="conversation-item"]');
  let idx = -1;
  for (let pass = 0; pass < 10 && idx < 0; pass++) {
    const n = await items.count();
    for (let k = 0; k < n; k++) {
      const t = await items.nth(k).innerText().catch(() => '');
      if (t.includes(conv)) { idx = k; break; }
    }
    if (idx < 0) {
      await page.evaluate(() => {
        const el = [...document.querySelectorAll('[class*=onversationList]')].find((e) => e.scrollHeight > e.clientHeight);
        if (el) el.scrollTop += 600;
      });
      await page.waitForTimeout(500);
    }
  }
  if (idx < 0) throw new Error('会话「' + conv + '」没找到，拒绝操作（避免回错人）');
  await items.nth(idx).click();
  await page.waitForSelector('[data-e2e="msg-item-content"]', { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2000);
  log('滚动到底…');
  await scrollToBottom(page);
  log('已到底');
  await page.waitForTimeout(800);
  for (let i = 0; i < 4; i++) {
    const st = await page.evaluate(() => document.querySelector('.messageMessageListlist')?.scrollTop ?? 0);
    if (st < -200) break;
    await scrollToBottom(page);
  }
  log('目标会话:', conv);

  const scrollUp = async (steps = 2) => {
    const rect = await page
      .evaluate(() => {
        const el = document.querySelector('.messageMessageListlist');
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2, st: Math.round(el.scrollTop) };
      })
      .catch(() => null);
    if (!rect) return false;
    const wasAt = rect.st;
    await page.mouse.move(rect.x, rect.y);
    await page.waitForTimeout(120);
    // No DOM queries here on purpose: reading between wheels lets the list re-anchor.
    for (let i = 0; i < steps; i++) {
      await page.mouse.wheel(0, -600);
      await page.waitForTimeout(200);
    }
    await page.waitForTimeout(500);
    const st = await page.evaluate(() => Math.round(document.querySelector('.messageMessageListlist')?.scrollTop ?? 0));
    return st !== wasAt;
  };

  const seenPath = './seen.json';
  const seenPrev = new Set(fs.existsSync(seenPath) ? JSON.parse(fs.readFileSync(seenPath, 'utf8')) : []);
  const seenNow = new Set(seenPrev);
  const anchor = fs.existsSync('./anchor.json') ? JSON.parse(fs.readFileSync('./anchor.json', 'utf8')).id : null;
  const skipAuthors = String(process.env.SKIP_AUTHORS || '').split(',').map((x) => x.trim()).filter(Boolean);
  const doneThisRun = new Set();
  let stagnant = 0;
  for (let guard = 0; guard < 60 && handled < MAX; guard++) {
    const all = await scan(page);
    const conflicts = all.filter((m) => m.conflict && !m.fromMe && !m.replied);
    for (const cf of conflicts) {
      if (ledger['cf:' + cf.text.slice(0, 24)]) continue;
      fs.appendFileSync('./conflicts.jsonl', JSON.stringify({ at: new Date().toISOString(), text: cf.text }) + '\n');
      ledger['cf:' + cf.text.slice(0, 24)] = '__conflict__';
      log('搁置（疑似争执，需你自己回）:', cf.text.slice(0, 40));
    }
    fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
    const herTextHere = process.env.BOUNDARY === 'text' && all.some((m) => !m.fromMe && !m.bot && m.kind === 'text' && m.text.length > 1);
    let anchorIdx = -1;
    if (anchor) all.forEach((m, i) => { if (m.hash === anchor) anchorIdx = i; });
    const scope = anchorIdx >= 0 ? all.slice(anchorIdx + 1) : all.slice(-8);
    const target = scope.find(
      (m) => !m.fromMe && !m.bot && !m.emoji && !m.conflict && !m.replied && m.lastOfBurst !== false && KINDS.includes(m.kind) && !skipAuthors.includes(m.author) && !doneThisRun.has(m.hash || m.text.slice(0, 24)) && (process.env.IGNORE_LEDGER === '1' || (!sentMarked(ledger['h:' + m.hash]) && !sentMarked(ledger[m.hash]) && !sentMarked(ledger['t:' + m.text.slice(0, 24)]) && !sentMarked(ledger[m.text.slice(0, 24)]))),
    );
    if (!target) {
      if (herTextHere) { log('翻到她的文字消息，按边界规则停止'); break; }
      if (++stagnant > Number(process.env.MAX_UP || 2)) { log('翻了两屏，按本次规则停止'); break; }
      let moved = await scrollUp();
      if (!moved) {
        // 到顶后会异步加载更早的历史，等一会儿再试
        await page.waitForTimeout(1500);
        moved = await scrollUp();
      }
      if (!moved) { log('滚不动了，结束'); break; }
      continue;
    }
    stagnant = 0;

    const key = target.hash ? 'h:' + target.hash : 't:' + target.text.slice(0, 24);
    doneThisRun.add(target.hash || target.text.slice(0, 24));
    const dir = path.resolve('./inbox', `all-${(target.hash || 'text').slice(0, 8)}`);

    try {
      log('定位目标…', target.kind, (target.hash || '').slice(0, 10));
      const el0 = locate(page, target);
      if (!(await el0.count())) throw new Error('target message gone');
      const el = el0;
      log('抓内容…');
      const content = await understand(page, target, el, dir);
      log('内容 ok');
      const thin = String(content).replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '').length < 10;
      if (thin) {
        log('内容太少，识别不了，跳过（不硬编）');
        ledger[key] = '__needs_review__';
        fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
        await closeOverlay(page);
        handled++;
        continue;
      }
      // 同一个视频就算换了封面的签名 URL，OCR/转写出来的内容也是同一份 -> 用它去重
      const contentKey = 'c:' + crypto.createHash('md5').update(String(content).replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, '')).digest('hex');
      if (String(content).replace(/\s+/g, '').length >= 12 && ledger[contentKey]) {
        ledger[key] = ledger[contentKey];
        fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
        log('内容与已回过的相同，跳过');
        await closeOverlay(page);
        continue;
      }
      await closeOverlay(page);
      await page.waitForTimeout(500);
      const today = new Date().toISOString().slice(0, 10);
      const saidPath = './study-said.json';
      const said = fs.existsSync(saidPath) ? JSON.parse(fs.readFileSync(saidPath, 'utf8')) : {};
      const study = false; // 按要求：回复里不再提「在学习」
      const her = herNow();
      let hint = [study ? SETTINGS.studyModeHint : '', her.hint].filter(Boolean).join('\n');
      if (usedReplies.length) hint += '\n本轮已经回过这些话，不要再重复同样的意思：' + usedReplies.slice(-4).join(' / ');
      log('生成草稿…');
      let reply = await draft(target.kind, content, hint);
      log('草稿 ok');
      const BAD = /没加载|加载不出|看不清|什么乱|乱糟糟|看不懂|读不出来/;
      const CARE = /累|睡|吃|歇|抱|陪|别硬撑|心疼/;
      if (BAD.test(reply) && !CARE.test(reply)) {
        log('稿子暴露了技术细节，重写');
        reply = await draft(target.kind, content, hint + '\n上一句暴露了"你没看懂视频"，不要提加载/乱/看不懂，改成关心她本人。');
      }
      if (usedReplies.some((u) => tooSimilar(u, reply))) {
        log('与前面重复，重写一次');
        reply = await draft(target.kind, content, hint + '\n上一句和前面太像了，换一个说法。');
      }
      log(`[${target.kind}] (${target.hash || 'nohash'}) ${reply}`);
      const r = await quoteReply(page, target, reply);
      if (!DRY && r.sent) {
        ledger[contentKey] = reply;
        if (study) fs.writeFileSync('./study-said.json', JSON.stringify({ date: today }));
        ledger[key] = reply;
        fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
      }
      usedReplies.push(reply);
      handled++;
      log(r.dry ? 'dry-run ok' : r.sent ? 'SENT' : 'UNSURE');
      delete RETRIES[key];
      fs.writeFileSync('./retries.json', JSON.stringify(RETRIES, null, 2));
      await scrollToBottom(page);
      await page.waitForTimeout(600);
    } catch (e) {
      const tries = (RETRIES[key] = (RETRIES[key] || 0) + 1);
      log(`FAIL ${target.kind}: ${e.message}（第 ${tries} 次）`);
      if (tries >= 3) ledger[key] = '__needs_review__';
      fs.writeFileSync('./retries.json', JSON.stringify(RETRIES, null, 2));
      fs.writeFileSync(LEDGER, JSON.stringify(ledger, null, 2));
      handled++;
    }
  }
  try {
    const finalCards = await scan(page);
    for (const m of finalCards) if (m.hash) seenNow.add(m.hash);
    fs.writeFileSync(seenPath, JSON.stringify([...seenNow], null, 2));
  } catch {}
  log('done, handled', handled);
} finally {
  await browser.close().catch(() => {});
}
