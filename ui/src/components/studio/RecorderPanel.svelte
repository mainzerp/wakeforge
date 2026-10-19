<script lang="ts">
  import Icon from "../Icon.svelte";
  import Scope from "../Scope.svelte";
  import { studio } from "../../lib/api";
  import { plural, t } from "../../lib/i18n.svelte";
  import { Recorder, dbToPercent, fileToWav } from "../../lib/audio";
  import { setCounts, toast, toastError } from "../../lib/state.svelte";
  import type { Session, SplitParams } from "../../lib/types";

  let { params = $bindable(), onuploaded }: { params: SplitParams; onuploaded: (s: Session) => void } = $props();

  const PARAM_FIELDS: { key: keyof SplitParams; label: string; step: number; hint: string }[] = [
    // Getters: t() must run at render time, not once when the module loads.
    { key: "margin_db", get label() { return t("Margin dB"); }, step: 1, get hint() { return t("dB above the local noise floor that counts as speech"); } },
    { key: "min_len", get label() { return t("Min length s"); }, step: 0.05, get hint() { return t("shortest clip"); } },
    { key: "max_len", get label() { return t("Max length s"); }, step: 0.1, get hint() { return t("longest clip"); } },
    { key: "merge_gap", get label() { return t("Merge gap s"); }, step: 0.05, get hint() { return t("join parts separated by less than this"); } },
    { key: "pad", get label() { return t("Padding s"); }, step: 0.05, get hint() { return t("silence kept around each word"); } },
    { key: "floor_window", get label() { return t("Floor window s"); }, step: 0.5, get hint() { return t("window for the noise floor estimate"); } },
  ];
  const SEGMENTS = 24;

  let rec = $state<Recorder | null>(null);
  let busy = $state(false);
  let level = $state(-90);
  let elapsed = $state(0);
  let devices = $state<MediaDeviceInfo[]>([]);
  let deviceId = $state(localStorage.getItem("deviceId") ?? "");
  let prefix = $state(localStorage.getItem("prefix") ?? "");
  let showParams = $state(false);
  // Uploading files: position in the queue, and whether files are dragged over the panel
  let uploading = $state<{ done: number; total: number } | null>(null);
  let dragging = $state(false);
  let picker: HTMLInputElement;

  $effect(() => localStorage.setItem("prefix", prefix));
  $effect(() => localStorage.setItem("deviceId", deviceId));

  $effect(() => {
    if (!rec) return;
    const r = rec;
    const id = setInterval(() => {
      level = r.takePeakDb();
      elapsed = r.seconds;
    }, 80);
    return () => clearInterval(id);
  });

  async function refreshDevices() {
    const all = await navigator.mediaDevices.enumerateDevices();
    devices = all.filter((d) => d.kind === "audioinput" && d.deviceId !== "default" && d.deviceId !== "communications");
    if (deviceId && !devices.some((d) => d.deviceId === deviceId)) deviceId = "";
  }
  refreshDevices();

  async function toggle() {
    busy = true;
    try {
      if (!rec) {
        rec = await Recorder.start(deviceId);
        await refreshDevices();
      } else {
        const r = rec;
        rec = null;
        level = -90;
        const wav = await r.stop();
        const res = await studio.upload(wav, $state.snapshot(params), prefix);
        setCounts(res.counts);
        onuploaded(res.session);
        toast(
          plural(res.session.clips.length, "Saved {duration}s, split into {n} clip.", "Saved {duration}s, split into {n} clips.", {
            duration: res.session.duration,
          }),
        );
      }
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  async function uploadFiles(files: File[]) {
    if (!files.length || busy || rec) return;
    busy = true;
    uploading = { done: 0, total: files.length };
    let clips = 0, ok = 0;
    for (const file of files) {
      try {
        const wav = await fileToWav(file);
        const res = await studio.upload(wav, $state.snapshot(params), prefix, file.name);
        setCounts(res.counts);
        onuploaded(res.session);
        clips += res.session.clips.length;
        ok++;
      } catch (e) {
        toastError(t("{file}: could not be read ({error})", { file: file.name, error: e instanceof Error ? e.message : String(e) }));
      }
      uploading.done++;
    }
    if (ok) toast(plural(ok, "{n} file uploaded, {clips} clips to sort.", "{n} files uploaded, {clips} clips to sort.", { clips }));
    uploading = null;
    busy = false;
  }

  function ondrop(e: DragEvent) {
    e.preventDefault();
    dragging = false;
    uploadFiles([...(e.dataTransfer?.files ?? [])].filter((f) => f.type.startsWith("audio/") || /\.(wav|mp3|m4a|aac|ogg|oga|opus|flac|webm)$/i.test(f.name)));
  }

  const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const lit = $derived(Math.round((dbToPercent(level) / 100) * SEGMENTS));
</script>

<div class="panel rec" class:live={!!rec} class:drop={dragging} role="region" aria-label={t("Recorder")}
  ondragover={(e) => { e.preventDefault(); if (!rec) dragging = true; }} ondragleave={() => (dragging = false)} {ondrop}>
  <div class="head">
    <span class="eyebrow">{t("Recorder")}</span>
    <span class="chip" class:pos={!!rec}><span class="dot" class:blink={!!rec}></span>{rec ? t("LIVE") : t("IDLE")}</span>
  </div>

  <div class="core">
    <button class="big" class:on={!!rec} disabled={busy} onclick={toggle} aria-label={rec ? t("Stop recording") : t("Start recording")}>
      <span class="ring"></span>
      <Icon name={rec ? "stop" : "mic"} size={30} stroke={2} />
    </button>
    <div class="readout">
      <div class="time mono">{fmt(elapsed)}</div>
      <div class="db mono dim">
        {rec ? `${level.toFixed(0)} dBFS` : uploading ? t("file {n} of {total}…", { n: Math.min(uploading.done + 1, uploading.total), total: uploading.total }) : busy ? t("uploading…") : t("ready")}
      </div>
    </div>
  </div>

  <Scope analyser={rec?.analyser ?? null} height={80} />

  <div class="meter" aria-hidden="true">
    {#each Array(SEGMENTS) as _, i (i)}
      <span class:on={i < lit} class:warm={i >= SEGMENTS - 6} class:hot={i >= SEGMENTS - 2}></span>
    {/each}
  </div>

  <div class="upload">
    <input bind:this={picker} type="file" multiple hidden accept="audio/*,.wav,.mp3,.m4a,.aac,.ogg,.opus,.flac,.webm"
      onchange={(e) => { const input = e.currentTarget; uploadFiles([...(input.files ?? [])]); input.value = ""; }} />
    <button class="small" onclick={() => picker.click()} disabled={busy || !!rec}><Icon name="upload" size={15} /> {t("Upload audio files")}</button>
    <span class="hint faint">{t("or drop them here. Files up to 3 s become one clip, longer ones are split like a recording.")}</span>
  </div>

  <div class="fields">
    <label class="field">
      {t("Speaker / prefix")}
      <input bind:value={prefix} maxlength="40" placeholder={t("e.g. patric")} disabled={!!rec} />
    </label>
    <label class="field">
      {t("Microphone")}
      <select bind:value={deviceId} disabled={!!rec}>
        <option value="">{t("System default")}</option>
        {#each devices as d, i (d.deviceId)}
          <option value={d.deviceId}>{d.label || t("Microphone {n}", { n: i + 1 })}</option>
        {/each}
      </select>
    </label>
  </div>

  <button class="ghost toggle" onclick={() => (showParams = !showParams)}>
    <Icon name="sliders" size={16} /> {t("Split settings")}
    <span class="chev" class:open={showParams}><Icon name="chevron" size={14} /></span>
  </button>
  {#if showParams}
    <div class="params">
      {#each PARAM_FIELDS as f (f.key)}
        <label class="field" title={f.hint}>
          {f.label}
          <input type="number" step={f.step} bind:value={params[f.key]} class="mono" />
        </label>
      {/each}
    </div>
  {/if}
</div>

<style>
  .rec {
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    transition: border-color 0.3s, box-shadow 0.3s;
  }
  .rec.live {
    border-color: rgba(255, 61, 242, 0.5);
    box-shadow: 0 0 40px -10px rgba(255, 61, 242, 0.45);
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .blink {
    animation: blink 1s steps(2) infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0.2;
    }
  }
  .core {
    display: flex;
    align-items: center;
    gap: 18px;
  }
  .big {
    position: relative;
    width: 76px;
    height: 76px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    padding: 0;
    border: 2px solid var(--magenta);
    background: radial-gradient(circle at 35% 30%, rgba(255, 61, 242, 0.35), rgba(80, 20, 90, 0.4));
    box-shadow: var(--glow-magenta);
    color: #fff;
  }
  .big:hover:not(:disabled) {
    background: radial-gradient(circle at 35% 30%, rgba(255, 61, 242, 0.5), rgba(80, 20, 90, 0.5));
    border-color: var(--magenta);
  }
  .big.on {
    border-color: var(--cyan);
    background: radial-gradient(circle at 35% 30%, rgba(41, 240, 255, 0.35), rgba(10, 60, 90, 0.4));
    box-shadow: var(--glow-cyan);
  }
  .ring {
    position: absolute;
    inset: -8px;
    border-radius: 50%;
    border: 1px solid rgba(255, 61, 242, 0.35);
  }
  .big.on .ring {
    border-color: var(--cyan);
    animation: pulse 1.4s ease-out infinite;
  }
  @keyframes pulse {
    from {
      transform: scale(0.9);
      opacity: 0.9;
    }
    to {
      transform: scale(1.35);
      opacity: 0;
    }
  }
  .time {
    font-size: 34px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-shadow: 0 0 18px rgba(155, 107, 255, 0.6);
  }
  .db {
    font-size: 13px;
  }
  .meter {
    display: grid;
    grid-template-columns: repeat(24, 1fr);
    gap: 3px;
  }
  .meter span {
    height: 8px;
    border-radius: 2px;
    background: rgba(155, 107, 255, 0.12);
    transition: background 0.06s;
  }
  .meter span.on {
    background: var(--cyan);
    box-shadow: 0 0 8px rgba(41, 240, 255, 0.7);
  }
  .meter span.on.warm {
    background: var(--amber);
    box-shadow: 0 0 8px rgba(255, 181, 71, 0.7);
  }
  .meter span.on.hot {
    background: var(--danger);
    box-shadow: 0 0 8px rgba(255, 79, 123, 0.8);
  }
  .rec.drop {
    border-color: var(--cyan);
    box-shadow: 0 0 22px -8px rgba(41, 240, 255, 0.9);
  }
  .upload {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
  }
  .upload button {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  .upload .hint {
    font-size: 12px;
    line-height: 1.45;
  }
  .fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    align-self: flex-start;
    padding: 4px 6px;
  }
  .chev {
    display: inline-flex;
    transition: transform 0.2s;
  }
  .chev.open {
    transform: rotate(90deg);
  }
  .params {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }
</style>
