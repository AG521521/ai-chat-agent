// 抖音适配器（阶段 B）：读界面 → 交共享核心决策 → 只出稿，绝不发送。
//
// 刻意不改 auto-all.mjs：老的链路原样保留，这个文件是新增的并行通道。
// 发送功能在阶段 E 之后才会出现；在那之前 ALLOW_SEND=1 直接拒绝启动。
import { connect, scrollToBottom, messages, closeOverlay } from './douyin.mjs';
import { callCore } from './core-bridge.mjs';
import { enrichVideo } from './core-media.mjs';
import { sendReply } from './douyin-send.mjs';

const CONV = process.env.CONV || '白名单';
const OFFLINE = process.env.OFFLINE === '1';
const ENRICH = process.env.ENRICH !== '0';
const MAX_MESSAGES = Number(process.env.MAX_MESSAGES || 20);
const DRY = process.env.DRY !== '0'; // 默认就是预演

const ARGV = process.argv.slice(2);
const argValue = (name, def = '') => {
  const i = ARGV.indexOf(name);
  return i >= 0 ? (ARGV[i + 1] ?? def) : def;
};
const DISPATCH_ID = argValue('--dispatch-task', '');
const DO_SEND = ARGV.includes('--send');
const LIVE = ARGV.includes('--live');

/** 按会话名打开会话（比"只认置顶"更可控；找不到就硬失败，避免回错人）。 */
async function openConversation(page, name) {
  const items = page.locator('[data-e2e="conversation-item"]');
  for (let pass = 0; pass < 10; pass++) {
    const n = await items.count();
    for (let k = 0; k < n; k++) {
      const text = await items.nth(k).innerText().catch(() => '');
      if (text.includes(name)) {
        await items.nth(k).click();
        await page.waitForTimeout(1500);
        return text.split('\n')[0].trim();
      }
    }
    await page.evaluate(() => {
      const el = [...document.querySelectorAll('[class*=onversationList]')].find(
        (e) => e.scrollHeight > e.clientHeight,
      );
      if (el) el.scrollTop += 600;
    });
    await page.waitForTimeout(500);
  }
  return null;
}

if (process.env.ALLOW_SEND === '1') {
  console.error('阶段 E 之前禁止发送：agent-core.mjs 只做观察与出稿。');
  process.exit(2);
}
if (!DRY) {
  console.error('DRY=0 也已禁用：本文件不含发送逻辑。');
  process.exit(2);
}

const KIND_MAP = { video: 'video', image: 'image', photos: 'image', comment: 'text', text: 'text' };

function mediaSig(m) {
  const cover = m.card?.cover || '';
  const hit = cover.match(/([0-9a-f]{16,})/);
  if (hit) return hit[1];
  if (cover) return 'src' + cover.split('?')[0].slice(-24);
  return '';
}

function toMessage(m, localId) {
  const contentType = KIND_MAP[m.kind] || 'unknown';
  return {
    content_type: contentType,
    text: (m.text || '').trim().slice(0, 400),
    media_sig: mediaSig(m),
    // 抖音网页版的 DOM 没有服务端 message_id，这里不伪造强键
    source_message_id: null,
    // 视频/图集必须做原生引用 + 目标验证，交给核心走 VERIFYING_QUOTE
    requires_quote: contentType === 'video' || m.kind === 'photos',
    quote_target_verified: false,
    evidence_level: 'ui_observed',
    raw_ref: { kind: m.kind, author: m.card?.author || '', local_id: localId },
  };
}

async function dispatchTask(taskId, doSend, live) {
  const { data } = await callCore(['status', '--task-id', taskId]);
  const task = data?.task;
  if (!task) throw new Error(`核心库里没有任务 ${taskId}`);
  if (task.state !== 'READY_TO_SEND') {
    return { task_id: taskId, error: `状态不是 READY_TO_SEND: ${task.state}`, sent: false };
  }

  const presendArgs = ['presend', '--task-id', taskId];
  if (live) presendArgs.push('--live');
  const pre = await callCore(presendArgs);
  const blockers = pre.data?.blockers || [];
  if (blockers.length) return { task_id: taskId, blocked: blockers, sent: false };

  const hash = task.requires_quote ? String(task.quote_target || '') : '';
  if (task.requires_quote && !hash) {
    return { task_id: taskId, error: '需要引用但缺少 quote_target', sent: false };
  }

  const { browser, page } = await connect();
  try {
    const conv = await openConversation(page, String(task.contact_id));
    if (!conv) throw new Error(`会话「${task.contact_id}」没找到，拒绝发送`);
    await scrollToBottom(page);
    await page.waitForTimeout(600);
    const result = await sendReply(page, {
      hash,
      text: String(task.draft_text || ''),
      dryRun: !doSend,
    });
    const out = { task_id: taskId, ...result };

    if (doSend && ['verified', 'unknown', 'failed'].includes(result.status)) {
      const mapping = {
        verified: ['verified', 'ui_verified'],
        unknown: ['unknown', 'ui_observed'],
        failed: ['failed', 'ui_observed'],
      };
      const [status, evidence] = mapping[result.status];
      await callCore([
        'mark-sent',
        '--task-id', taskId,
        '--status', status,
        '--evidence', evidence,
        '--detail', (result.detail || '').slice(0, 200),
      ]);
      const after = await callCore(['status', '--task-id', taskId]);
      out.state_after = after.data?.task?.state;
    }
    return out;
  } finally {
    await browser.close().catch(() => {});
  }
}

if (DISPATCH_ID) {
  try {
    const out = await dispatchTask(DISPATCH_ID, DO_SEND, LIVE);
    console.log(JSON.stringify(out, null, 2));
    process.exit(out.status === 'verified' || out.status === 'dry_run' ? 0 : 3);
  } catch (e) {
    console.error(JSON.stringify({ error: String(e.message) }, null, 2));
    process.exit(1);
  }
}

const { browser, page } = await connect();
try {
  const conv = await openConversation(page, CONV);
  if (!conv) throw new Error(`会话「${CONV}」没找到，拒绝操作（避免回错人）`);
  if (!conv.includes(CONV)) {
    throw new Error(`会话不匹配：期望包含 "${CONV}"，实际拿到 "${conv}"，拒绝继续`);
  }
  await scrollToBottom(page);
  const all = await messages(page);
  const rawIncoming = all.filter((m) => !m.fromMe && KIND_MAP[m.kind]).slice(-MAX_MESSAGES);
  const incoming = rawIncoming.map((m, i) => toMessage(m, i));

  if (!incoming.length) {
    console.log(JSON.stringify({ conversation: conv, incoming: 0, note: '当前窗口没有可处理的消息' }, null, 2));
  } else {
    const { data } = await callCore(['observe'], {
      platform: 'douyin',
      contact_id: conv,
      contact_verified: true,
      messages: incoming,
    });
    const created = data?.created || [];

    // 阶段 D：把视频/图集的媒体理解补进来（复用 ingest.mjs / asr.mjs）
    const enriched = [];
    if (ENRICH && created.length) {
      const byLocal = new Map(
        created.map((c) => [c?.raw_ref?.local_id, c]),
      );
      for (let i = 0; i < incoming.length; i += 1) {
        const task = byLocal.get(i);
        if (!task) continue; // 老消息已存在，跳过
        const payload = incoming[i];
        if (payload.content_type !== 'video' && payload.content_type !== 'image') continue;
        try {
          const info = await enrichVideo(page, rawIncoming[i]);
          await callCore([
            'attach-media',
            '--task-id', task.task_id,
            '--summary', (info.summary || '(媒体无可用文字)').slice(0, 400),
            '--quote-target', info.quoteTarget,
          ]);
          enriched.push({
            task_id: task.task_id,
            chars: (info.summary || '').length,
            ocr_chars: info.ocrChars,
            speech_chars: info.speechChars,
            segments: info.segments,
          });
        } catch (e) {
          await callCore([
            'attach-media',
            '--task-id', task.task_id,
            '--summary', `(媒体理解失败：${String(e.message).slice(0, 80)})`,
          ]).catch(() => {});
        }
        await closeOverlay(page).catch(() => {});
      }
    }

    const decisions = [];
    for (const c of created) {
      const argv = ['decide', '--task-id', c.task_id];
      if (OFFLINE) argv.push('--offline');
      const r = await callCore(argv);
      decisions.push(r.data);
    }

    const tasks = decisions.map((d) => ({
      task_id: d?.task_id,
      state: d?.decision?.state,
      risk: d?.risk?.level,
      reasons: (d?.risk?.reasons || []).slice(0, 4),
      draft_chars: (d?.decision?.draft_text || '').trim().length,
      skipped_model: Boolean(d?.skipped_model),
      error: d?.error || null,
    }));

    const { data: queue } = await callCore(['queue']);
    console.log(
      JSON.stringify(
        {
          conversation: conv,
          incoming: incoming.length,
          allowlisted: data?.allowlisted,
          created: created.length,
          duplicates: data?.duplicates?.length ?? 0,
          enriched,
          tasks,
          pending_approvals: Array.isArray(queue) ? queue.length : null,
          sent_anything: false,
        },
        null,
        2,
      ),
    );
  }
} finally {
  await browser.close().catch(() => {});
}
