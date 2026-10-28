// 用抖音工程里已经下好的 whisper-small 把微信视频的音频转成文字。
// 仅读取 C:\Users\yourname\douyin-agent\models 下的模型缓存，不下载、不改那边任何文件。
import fs from 'node:fs';
import { pipeline, env } from '@huggingface/transformers';

env.remoteHost = 'https://hf-mirror.com';
env.cacheDir = 'C:/Users/yourname/douyin-agent/models';
env.allowRemoteModels = false;

const MODEL = process.env.ASR_MODEL || 'Xenova/whisper-small';

function readWav16(file) {
  const buf = fs.readFileSync(file);
  let off = 12;
  while (off + 8 <= buf.length) {
    const id = buf.toString('ascii', off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === 'data') {
      const data = buf.subarray(off + 8, off + 8 + size);
      const n = Math.floor(data.length / 2);
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) out[i] = data.readInt16LE(i * 2) / 32768;
      return out;
    }
    off += 8 + size + (size % 2);
  }
  throw new Error('no data chunk in wav');
}

const wav = process.argv[2];
if (!wav) {
  console.error('usage: node asr.mjs <16k-mono.wav>');
  process.exit(2);
}

const asr = await pipeline('automatic-speech-recognition', MODEL, { dtype: 'q8' });
const audio = readWav16(wav);
const res = await asr(audio, { language: 'zh', task: 'transcribe', chunk_length_s: 30, stride_length_s: 4 });
console.log((res.text || '').trim());
