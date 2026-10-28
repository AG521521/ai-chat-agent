// 共享核心桥：抖音适配器 → D:\ai_chat_core 的 CLI。
// 约定：核心只输出 JSON（stdout），错误在 stderr；退出码 0/3 都算"有结果"。
import { spawn } from 'node:child_process';

const PYTHON =
  process.env.CORE_PYTHON ||
  'C:\\Users\\yourname\\AppData\\Local\\Programs\\Python\\Python314\\python.exe';
const CORE_ROOT = process.env.CORE_ROOT || 'D:\\ai_chat_core';

/**
 * @param {string[]} args 例如 ['run'] 或 ['queue']
 * @param {object|null} payload 从 stdin 传入的 JSON
 * @param {{timeoutMs?: number}} opts
 * @returns {Promise<{code:number, data:any, stderr:string}>}
 */
export function callCore(args = [], payload = null, opts = {}) {
  const timeoutMs = opts.timeoutMs ?? 300000;
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, ['-m', 'core.cli', ...args], {
      cwd: CORE_ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
    });

    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`core 超时（${timeoutMs}ms）`));
    }, timeoutMs);

    child.stdout.on('data', (d) => { out += d.toString('utf8'); });
    child.stderr.on('data', (d) => { err += d.toString('utf8'); });
    child.on('error', (e) => { clearTimeout(timer); reject(e); });
    child.on('close', (code) => {
      clearTimeout(timer);
      const text = out.trim();
      let data = null;
      if (text.startsWith('{') || text.startsWith('[')) {
        try { data = JSON.parse(text); } catch { /* 交给下面判断 */ }
      }
      if (code !== 0 && code !== 3) {
        reject(new Error(`core exit ${code}: ${(err.trim() || text).slice(0, 300)}`));
        return;
      }
      resolve({ code, data, stderr: err.trim() });
    });

    if (payload === null) child.stdin.end();
    else child.stdin.end(JSON.stringify(payload));
  });
}
