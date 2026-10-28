// Whisper ASR via transformers.js, models fetched from hf-mirror.
import fs from 'node:fs';
import { pipeline, env } from '@huggingface/transformers';

env.remoteHost = 'https://hf-mirror.com';
env.cacheDir = './models';

const MODEL = process.env.ASR_MODEL || 'Xenova/whisper-small';
let asrPromise = null;

export function loadAsr() {
  asrPromise ??= pipeline('automatic-speech-recognition', MODEL, { dtype: 'q8' });
  return asrPromise;
}

export function readWav16(file) {
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

export async function transcribe(wavPath) {
  const asr = await loadAsr();
  const audio = readWav16(wavPath);
  const res = await asr(audio, {
    language: 'zh',
    task: 'transcribe',
    chunk_length_s: 30,
    stride_length_s: 5,
  });
  return { text: (res.text || '').trim(), seconds: audio.length / 16000 };
}
