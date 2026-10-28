// Scheduled runner: during the configured time slots, check 白名单 and auto-reply.
// Usage: node loop.mjs   (background safe; Ctrl+C to stop)
import { spawn } from 'node:child_process';

const CONV = process.env.CONV || '白名单';
const EVERY = Number(process.env.EVERY_MIN || 8) * 60 * 1000;
const SLOTS = (process.env.SLOTS || '18-24')
  .split(',')
  .map((s) => s.split('-').map(Number));

const inSlot = (h) => SLOTS.some(([a, b]) => h >= a && h < b);
const log = (...a) => console.log(`[${new Date().toLocaleString('zh-CN')}]`, ...a);

log('定时回复启动', `会话=${CONV}`, `每${EVERY / 60000}分钟`, `时段=${SLOTS.map(([a,b])=>a+'-'+b).join(',')}`);

for (;;) {
  const h = new Date().getHours();
  if (inSlot(h)) {
    log('开始检查');
    await new Promise((resolve) => {
      const p = spawn(process.execPath, ['auto-all.mjs'], { cwd: process.cwd(), env: { ...process.env, CONV, KINDS: process.env.KINDS || 'video,image' }, stdio: 'inherit' });
      p.on('exit', (c) => { log('本轮结束', 'code=' + c); resolve(); });
    });
  } else {
    log('不在时段内，等待');
  }
  await new Promise((r) => setTimeout(r, EVERY));
}
