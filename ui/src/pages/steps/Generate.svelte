<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../../components/Icon.svelte";
  import MiniClip from "../../components/MiniClip.svelte";
  import ConfusableRun from "../../components/project/ConfusableRun.svelte";
  import JobStats from "../../components/project/JobStats.svelte";
  import { generate, jobs } from "../../lib/api";
  import { locale, plural, t } from "../../lib/i18n.svelte";
  import { href } from "../../lib/router.svelte";
  import { app, toast, toastError } from "../../lib/state.svelte";
  import type { GenClip, GenerateParams, GenerateStatus, Project } from "../../lib/types";

  let { project }: { project: Project } = $props();

  const DEFAULTS: GenerateParams = {
    count: 25000,
    speed: { lo: 0.85, hi: 1.2 },
    steps: 16,
    design_share: 0.15,
    filter: true,
  };
  const PRESETS = [10000, 25000, 50000];

  let status = $state<GenerateStatus | null>(null);
  let params = $state<GenerateParams>(structuredClone(DEFAULTS));
  let advanced = $state(false);
  let showLog = $state(false);
  let busy = $state(false);
  let samples = $state<GenClip[]>([]);

  const job = $derived(status?.job ?? null);
  const running = $derived(!!job && job.alive && (job.state === "running" || job.state === "starting"));
  const otherJob = $derived(status?.active && status.active.id !== job?.id ? status.active : null);
  const total = $derived(status?.corpus.total ?? 0);
  const pct = $derived(job?.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : 0);
  const startPct = $derived(job?.total ? ((job.start ?? 0) / job.total) * 100 : 0);
  const phrasesChanged = $derived(
    !!status?.corpus.total && project.tts_phrases.some((p) => !status!.corpus.phrases.includes(p)),
  );

  async function refresh(given?: GenerateStatus) {
    try {
      const wasRunning = running;
      const s = given ?? (await generate.status());
      if (!status && s.job?.params?.speed) params = { ...structuredClone(DEFAULTS), ...s.job.params } as GenerateParams;
      status = s;
      if (app.project?.slug === project.slug) {
        app.project.summary.generated = s.corpus.total;
        app.project.summary.confusable = s.confusables.total;
      }
      if (wasRunning && s.job && !s.job.alive) {
        if (s.job.state === "failed") toastError(s.job.message ? t(s.job.message) : t("Generation failed"));
        else toast(s.job.state === "done" ? t("Done: {n} synthetic clips.", { n: s.corpus.total }) : t("Generation stopped."));
        if (s.corpus.total) drawSamples();
      }
    } catch (e) {
      toastError(e);
    }
  }

  $effect(() => {
    untrack(() => refresh());
    const id = setInterval(() => (running || otherJob) && refresh(), 1000);
    return () => clearInterval(id);
  });

  async function act<T>(fn: () => Promise<T>) {
    busy = true;
    try {
      return await fn();
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  async function start() {
    const j = await act(() => generate.start($state.snapshot(params)));
    if (j) {
      showLog = false;
      await refresh();
    }
  }

  async function stop() {
    if (job) await act(() => jobs.stop(job.id));
    await refresh();
  }

  async function reset() {
    if (!confirm(t("Delete all {n} synthetic clips of \"{word}\"? Own recordings are not affected.", { n: total, word: project.wake_word }))) return;
    const s = await act(() => generate.reset());
    if (s) {
      status = s;
      samples = [];
      negSamples = [];
      if (app.project) app.project.summary.generated = 0;
      toast(t("Synthetic corpus deleted."));
    }
  }

  async function drawSamples() {
    const res = await act(() => generate.samples(12));
    if (res) samples = res.clips;
  }

  let negSamples = $state<GenClip[]>([]);
  async function drawNegatives() {
    const res = await act(() => generate.samples(12, "negative"));
    if (res) negSamples = res.clips;
  }
  const clipDetail = (c: GenClip) => `${c.seconds}s${c.distance != null ? ` · Δ ${c.distance.toFixed(2)}` : ""}`;

  const hours = (s: number) => (s / 3600).toFixed(1);
  const voiceLabel = (v: string) => v.replace(/^(bank|design):/, "");
  const filterRate = $derived(
    job && (job.done ?? 0) - (job.start ?? 0) + (job.filtered ?? 0) > 0
      ? Math.round(((job.filtered ?? 0) / ((job.done ?? 0) - (job.start ?? 0) + (job.filtered ?? 0))) * 100)
      : null,
  );
  const startLabel = $derived(
    total >= params.count ? t("Target reached") : total ? t("Continue to {n}", { n: params.count }) : t("Generate {n} clips", { n: params.count }),
  );

  $effect(() => {
    if (status && status.corpus.total && !samples.length) untrack(drawSamples);
  });
</script>

{#if status}
<div class="page">
  {#if !status.engine.voices}
    <section class="panel note">
      <span class="chip warn"><span class="dot"></span>{t("No reference voices")}</span>
      <p>{t("There is no voice bank for this language yet, so every clip uses a designed voice (less natural variety).")}</p>
    </section>
  {/if}
  <div class="top">
    <section class="panel run">
      <div class="head">
        <span class="eyebrow">{t("Run")}</span>
        <span class="chip mono" title="{status.engine.model} ({status.engine.license})">
          {status.engine.name} · {plural(status.engine.voices, "{n} voice", "{n} voices")}
        </span>
      </div>

      <div class="field-block">
        <span class="label">{t("Target clips")}</span>
        <div class="presets">
          {#each PRESETS as p (p)}
            <button class="small" class:sel={params.count === p} onclick={() => (params.count = p)} disabled={running}>{p / 1000}k</button>
          {/each}
          <input type="number" class="mono" min="100" step="1000" bind:value={params.count} disabled={running} aria-label={t("Target clips")} />
        </div>
        <p class="hint faint">{t("Generation tops the corpus up to this number, so you can always continue later.")}</p>
        {#if total < status.targets.positives && !running}
          <p class="hint warn">{t("{n} clips so far: models hold up from {min} on (fewer missed more on the device).", { n: total, min: status.targets.positives })}</p>
        {/if}
      </div>

      <div class="field-block">
        <span class="label">{t("Tempo")} <span class="faint">{t("(speed factor, higher = faster)")}</span></span>
        <div class="range">
          <input type="number" class="mono" step="0.05" min="0.5" max="2" bind:value={params.speed.lo} disabled={running} aria-label={t("Tempo min")} />
          <span class="faint">{t("to")}</span>
          <input type="number" class="mono" step="0.05" min="0.5" max="2" bind:value={params.speed.hi} disabled={running} aria-label={t("Tempo max")} />
        </div>
      </div>

      <label class="check">
        <input type="checkbox" bind:checked={params.filter} disabled={running} />
        <span>
          {t("Phonetic check")}
          <span class="faint">{t("(keep close clips, drop the gray zone, store clearly different ones as negative candidates)")}</span>
        </span>
      </label>

      <button class="ghost toggle" onclick={() => (advanced = !advanced)}>
        <Icon name="sliders" size={16} /> {t("Advanced")}
        <span class="chev" class:open={advanced}><Icon name="chevron" size={14} /></span>
      </button>
      {#if advanced}
        <div class="adv">
          <span class="label">{t("Quality")} <span class="faint">{t("(diffusion steps; 32 is ~2× slower)")}</span></span>
          <div class="presets">
            {#each [16, 32] as s (s)}
              <button class="small" class:sel={params.steps === s} onclick={() => (params.steps = s)} disabled={running}>
                {s === 16 ? t("Fast (16)") : t("Best (32)")}
              </button>
            {/each}
          </div>
          <span class="label">{t("Designed voices")} <span class="faint">{t("(share of batches without a reference voice)")}</span></span>
          <div class="range">
            <input type="range" min="0" max="1" step="0.05" bind:value={params.design_share} disabled={running} aria-label={t("Designed voices share")} />
            <span class="mono">{Math.round(params.design_share * 100)}%</span>
          </div>
          <button class="ghost small" onclick={() => (params = { ...structuredClone(DEFAULTS), count: params.count })} disabled={running}>
            {t("Restore defaults")}
          </button>
        </div>
      {/if}

      <div class="actions">
        {#if running}
          <button class="danger" onclick={stop} disabled={busy}><Icon name="stop" size={15} /> {t("Stop")}</button>
        {:else}
          <button class="primary" onclick={start} disabled={busy || !!otherJob || total >= params.count}>
            <Icon name="sparkles" size={16} /> {startLabel}
          </button>
        {/if}
      </div>
      {#if otherJob}
        <p class="hint warn">{t("The GPU is busy: {kind} job of \"{project}\" is running.", { kind: otherJob.kind, project: otherJob.project })}</p>
      {/if}
      {#if phrasesChanged}
        <p class="hint warn">{t("New spellings were added since the last run; they are used for newly generated clips only.")}</p>
      {/if}
      <p class="hint faint">
        {t("Spellings: {list}", { list: project.tts_phrases.join(", ") })} ·
        <a class="small-link" href={href.project(project.slug, "wakeword")}>{t("check pronunciation in step 1")}</a>
      </p>
    </section>

    <section class="panel progress">
      <div class="head">
        <span class="eyebrow">{t("Corpus")}</span>
        {#if job}
          <span class="chip {job.state === 'failed' ? 'neg' : running ? 'pos' : ''}">
            <span class="dot" class:blink={running}></span>{t(job.state)}
          </span>
        {/if}
      </div>
      <div class="big">
        <div><strong class="mono">{(running ? Math.max(total, job?.done ?? 0) : total).toLocaleString(locale())}</strong><span class="dim">{t("clips")}</span></div>
        <div><strong class="mono">{hours(status.corpus.seconds)}</strong><span class="dim">{t("hours of audio")}</span></div>
        <div class="neg">
          <strong class="mono">{(running ? Math.max(status.negatives.total, job?.negatives ?? 0) : status.negatives.total).toLocaleString(locale())}</strong>
          <span class="dim">{t("negative candidates")}</span>
        </div>
      </div>

      {#if job}
        <div class="bar" aria-label={t("Progress {pct}%", { pct: pct.toFixed(0) })}>
          <span class="prev" style:width="{startPct}%"></span>
          <span class="fill" class:live={running} style:left="{startPct}%" style:width="{Math.max(0, pct - startPct)}%"></span>
        </div>
        <JobStats {job} {running} dropTitle={t("Dropped by length or in the gray zone of the phonetic check")}
          dropNote={filterRate != null ? t("{pct}% gray zone", { pct: filterRate }) : ""} />
        {#if job.state === "failed" && job.message}
          <p class="hint err">{t(job.message)}</p>
        {/if}
        <button class="ghost toggle" onclick={() => (showLog = !showLog)}>
          {t("Log")} <span class="chev" class:open={showLog}><Icon name="chevron" size={14} /></span>
        </button>
        {#if showLog}
          <pre class="log mono">{job.log || t("(empty)")}</pre>
        {/if}
      {:else}
        <p class="dim">{t("No run yet. OmniVoice makes ~5 clips/s on the RTX 3090, so 20k clips take about an hour.")}</p>
      {/if}
      {#if total && !running}
        <button class="danger small reset" onclick={reset} disabled={busy}><Icon name="trash" size={14} /> {t("Delete corpus")}</button>
      {/if}
    </section>
  </div>

  {#if total}
    <section class="panel block">
      <div class="head">
        <div>
          <span class="eyebrow">{t("Listen to the corpus")}</span>
          <p class="dim sub">{t("Random clips from the generated set. Click to play.")}</p>
        </div>
        <button class="small" onclick={drawSamples} disabled={busy}><Icon name="replay" size={14} /> {t("Draw 12")}</button>
      </div>
      <div class="grid">
        {#each samples as c (c.url)}
          <MiniClip url={c.url} label={voiceLabel(c.voice)} detail={clipDetail(c)} note={c.phrase ?? ""} />
        {/each}
      </div>
    </section>
  {/if}

  <ConfusableRun {project} {status} onstatus={(s) => refresh(s)} />

  {#if status.negatives.total}
    <section class="panel block">
      <div class="head">
        <div>
          <span class="eyebrow">{t("Negative candidates")}</span>
          <p class="dim sub">
            {t("TTS clips that came out clearly different from every spelling (Δ ≥ 0.70, wake word not contained). They can serve as extra negatives in training; listen to make sure none of them sounds like the wake word.")}
          </p>
        </div>
        <button class="small" onclick={drawNegatives} disabled={busy}><Icon name="replay" size={14} /> {t("Draw 12")}</button>
      </div>
      <div class="grid">
        {#each negSamples as c (c.url)}
          <MiniClip url={c.url} label={voiceLabel(c.voice)} detail={clipDetail(c)} note={t("asked for: {phrase}", { phrase: c.phrase ?? "" })} miss />
        {/each}
      </div>
    </section>
  {/if}
</div>
{/if}

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 22px;
  }
  .top {
    display: grid;
    grid-template-columns: minmax(320px, 420px) 1fr;
    gap: 22px;
    align-items: start;
  }
  .run,
  .progress,
  .block,
  .note {
    padding: 20px 22px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .label {
    font-size: 12.5px;
    color: var(--text-dim);
  }
  .field-block,
  .adv {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .presets {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .presets input {
    width: 110px;
  }
  .sel {
    border-color: var(--cyan);
    color: var(--cyan);
    background: rgba(41, 240, 255, 0.1);
  }
  .range {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .range input {
    width: 90px;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
  }
  .check input {
    accent-color: var(--cyan);
  }
  .range input[type="range"] {
    width: 180px;
    accent-color: var(--cyan);
    padding: 0;
    border: 0;
    background: none;
  }
  .hint {
    font-size: 12.5px;
    margin: 0;
    line-height: 1.5;
  }
  .hint.warn {
    color: var(--amber);
  }
  .hint.err {
    color: var(--danger);
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
  .actions button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .big {
    display: flex;
    gap: 40px;
  }
  .big div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .big strong {
    font-size: 34px;
    font-weight: 600;
    color: var(--cyan);
    text-shadow: var(--glow-cyan);
  }
  .big .neg strong {
    color: var(--magenta);
    text-shadow: var(--glow-magenta);
  }
  .bar {
    position: relative;
    height: 12px;
    border-radius: 6px;
    background: rgba(155, 107, 255, 0.14);
    overflow: hidden;
  }
  .bar span {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 0;
  }
  .prev {
    background: rgba(155, 107, 255, 0.45);
  }
  .fill {
    background: linear-gradient(90deg, var(--cyan), var(--magenta));
    box-shadow: 0 0 14px rgba(41, 240, 255, 0.6);
    transition: width 0.6s ease-out;
  }
  .fill.live {
    background-size: 200% 100%;
    animation: flow 1.5s linear infinite;
    background-image: linear-gradient(90deg, var(--cyan), var(--magenta), var(--cyan));
  }
  @keyframes flow {
    to {
      background-position: -200% 0;
    }
  }
  .blink {
    animation: blink 1s steps(2) infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0.2;
    }
  }
  .log {
    margin: 0;
    max-height: 220px;
    overflow: auto;
    padding: 12px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: rgba(5, 4, 16, 0.6);
    font-size: 11.5px;
    white-space: pre-wrap;
    color: var(--text-dim);
  }
  .reset {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .sub {
    margin: 4px 0 0;
    font-size: 13px;
  }
  .small-link {
    color: var(--cyan);
    font-size: 13px;
    text-decoration: none;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
  }
  .note p {
    margin: 0;
    color: var(--text-dim);
    line-height: 1.55;
  }
  @media (max-width: 1050px) {
    .top {
      grid-template-columns: 1fr;
    }
  }
</style>
