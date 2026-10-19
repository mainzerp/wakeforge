const WORKLET = `
class Recorder extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("recorder", Recorder);`;

export class Recorder {
  readonly analyser: AnalyserNode;
  readonly started = performance.now();
  private chunks: Float32Array[] = [];
  private length = 0;
  private peak = 0;

  private constructor(
    private stream: MediaStream,
    private ctx: AudioContext,
    node: AudioWorkletNode,
  ) {
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 2048;
    node.port.onmessage = (e: MessageEvent<Float32Array>) => {
      this.chunks.push(e.data);
      this.length += e.data.length;
      for (const v of e.data) this.peak = Math.max(this.peak, Math.abs(v));
    };
  }

  static async start(deviceId: string): Promise<Recorder> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    const ctx = new AudioContext({ sampleRate: 48000 });
    await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" })));
    const source = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, "recorder");
    const mute = ctx.createGain();
    mute.gain.value = 0;
    source.connect(node).connect(mute).connect(ctx.destination);
    const rec = new Recorder(stream, ctx, node);
    source.connect(rec.analyser);
    return rec;
  }

  /** Peak level since the last call, in dBFS. */
  takePeakDb(): number {
    const db = 20 * Math.log10(this.peak + 1e-9);
    this.peak = 0;
    return db;
  }

  get seconds(): number {
    return this.length / this.ctx.sampleRate;
  }

  async stop(): Promise<Blob> {
    this.stream.getTracks().forEach((t) => t.stop());
    const rate = this.ctx.sampleRate;
    await this.ctx.close();
    return encodeWav(this.chunks, this.length, rate);
  }
}

export function encodeWav(chunks: Float32Array[], length: number, rate: number): Blob {
  const buf = new ArrayBuffer(44 + length * 2);
  const v = new DataView(buf);
  const str = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF");
  v.setUint32(4, 36 + length * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, length * 2, true);
  let o = 44;
  for (const c of chunks) for (let i = 0; i < c.length; i++, o += 2) v.setInt16(o, Math.max(-1, Math.min(1, c[i])) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}

/** Decode an audio file in any format the browser plays and mix it down to a mono WAV (the server only reads WAV). */
export async function fileToWav(file: File): Promise<Blob> {
  const ctx = new AudioContext();
  try {
    const buf = await ctx.decodeAudioData(await file.arrayBuffer());
    const mono = new Float32Array(buf.length);
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const ch = buf.getChannelData(c);
      for (let i = 0; i < ch.length; i++) mono[i] += ch[i] / buf.numberOfChannels;
    }
    return encodeWav([mono], mono.length, buf.sampleRate);
  } finally {
    await ctx.close();
  }
}

export interface Peaks {
  values: Float32Array;
  duration: number;
}

const peakCache = new Map<string, Promise<Peaks>>();
let decodeCtx: OfflineAudioContext | null = null;

/** Max-abs envelope of an audio file in `buckets` bins, cached per URL. */
export function loadPeaks(url: string, buckets = 1200): Promise<Peaks> {
  const key = `${url}#${buckets}`;
  let p = peakCache.get(key);
  if (!p) {
    p = (async () => {
      const data = await (await fetch(url)).arrayBuffer();
      decodeCtx ??= new OfflineAudioContext(1, 1, 16000);
      const audio = await decodeCtx.decodeAudioData(data);
      const ch = audio.getChannelData(0);
      const values = new Float32Array(buckets);
      const step = ch.length / buckets;
      for (let b = 0; b < buckets; b++) {
        let max = 0;
        const end = Math.min(ch.length, Math.floor((b + 1) * step));
        for (let i = Math.floor(b * step); i < end; i++) max = Math.max(max, Math.abs(ch[i]));
        values[b] = max;
      }
      return { values, duration: audio.duration };
    })();
    p.catch(() => peakCache.delete(key));
    peakCache.set(key, p);
  }
  return p;
}

export function dbToPercent(db: number, floor = -60): number {
  return Math.max(0, Math.min(100, ((db - floor) / -floor) * 100));
}
