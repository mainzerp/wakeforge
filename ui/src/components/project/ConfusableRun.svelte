<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../Icon.svelte";
  import MiniClip from "../MiniClip.svelte";
  import JobStats from "./JobStats.svelte";
  import { generate, jobs } from "../../lib/api";
  import { locale, plural, t } from "../../lib/i18n.svelte";
  import { href } from "../../lib/router.svelte";
  import { app, toast, toastError } from "../../lib/state.svelte";
  import type { ConfusableParams, GenClip, GenerateStatus, Project } from "../../lib/types";

  let {
    project,
    status,
    onstatus,
  }: { project: Project; status: GenerateStatus; onstatus: (s?: GenerateStatus) => void } = $props();

  const PRESETS = [2000, 5000, 10000];
  let params = $state<ConfusableParams>(
    untrack(() => ({ count: 5000, speed: { lo: 0.85, hi: 1.2 }, steps: 16, design_share: 0.15, ...status.confusable_job?.params })),
  );
  let busy = $state(false);
  let showLog = $state(false);
  let samples = $state<GenClip[]>([]);

  const job = $derived(status.confusable_job);
  const running = $derived(!!job && job.alive && (job.state === "running" || job.state === "starting"));
  const blocker = $derived(status.active && status.active.id !== job?.id ? status.active : null);
  const total = $derived(status.confusables.total);
  const shown = $derived(running ? Math.max(total, job?.done ?? 0) : total);
  const pct = $derived(job?.total ? Math.min(100, ((job.done ?? 0) / job.total) * 100) : 0);
  const startPct = $derived(job?.total ? ((job.start ?? 0) / job.total) * 100 : 0);
  const words = $derived(project.confusables);
  const newWords = $derived(total > 0 && words.some((w) => !status.confusables.phrases.includes(w)));

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
    if (await act(() => generate.startConfusables($state.snapshot(params)))) {
      showLog = false;
      onstatus();
    }
  }

  async function stop() {
    if (job) await act(() => jobs.stop(job.id));
    onstatus();
  }

  async function reset() {
    if (!confirm(t("Delete all {n} sound-alike clips?", { n: total }))) return;
    const s = await act(() => generate.resetConfusables());
    if (s) {
      samples = [];
      if (app.project) app.project.summary.confusable = 0;
      onstatus(s);
      toast(t("Sound-alike clips deleted."));
    }
  }

  async function draw() {
    const res = await act(() => generate.samples(12, "confusable"));
    if (res) samples = res.clips;
  }

  let wasRunning = false;
  $effect(() => {
    const r = running;
    untrack(() => {
      if (wasRunning && !r && job) {
        if (job.state === "failed") toastError(job.message ? t(job.message) : t("Sound-alike generation failed"));
        else toast(job.state === "done" ? t("Done: {n} sound-alike clips.", { n: total }) : t("Sound-alike generation stopped."));
        if (total) draw();
      }
      wasRunning = r;
    });
  });

  $effect(() => {
    if (total && !samples.length) untrack(draw);
  });

  const voice = (v: string) => v.replace(/^(bank|design):/, "");
  const label = $derived(
    total >= params.count ? t("Target reached") : total ? t("Continue to {n}", { n: params.count }) : t("Generate {n} clips", { n: params.count }),
  );
</script>

<section class="panel conf">
  <div class="head">
    <div>
      <span class="eyebrow">{t("Sound-alike negatives")}</span>
      <p class="dim sub">
        {t("The sound-alikes from step 1 spoken by the same voices. Every clip is checked: if it came out like the wake word (or contains it), it is dropped, so these never contradict the positives.")}
      </p>
    </div>
    {#if job}
      <span class="chip {job.state === 'failed' ? 'neg' : running ? 'pos' : ''}"><span class="dot" class:blink={running}></span>{t(job.state)}</span>
    {/if}
  </div>

  {#if !words.length}
    <p class="dim">
      {t("No sound-alikes defined yet.")} <a href={href.project(project.slug, "wakeword")}>{t("Add them in step 1")}</a> {t("(Suggest finds some for you).")}
    </p>
  {:else}
    <div class="cols">
      <div class="col">
        <div class="words">
          {#each words as w (w)}<span class="chip neg">{w}</span>{/each}
        </div>
        <p class="faint small">
          {plural(words.length, "{n} word", "{n} words")} ·
          <a href={href.project(project.slug, "wakeword")}>{t("edit in step 1")}</a>
        </p>
        <div class="presets">
          {#each PRESETS as p (p)}
            <button class="small" class:sel={params.count === p} onclick={() => (params.count = p)} disabled={running}>{p / 1000}k</button>
          {/each}
          <input type="number" class="mono" min="100" step="1000" bind:value={params.count} disabled={running} aria-label={t("Target sound-alike clips")} />
        </div>
        {#if total < status.targets.sound_alikes && !running}
          <p class="hint warn small">{t("{n} clips so far: use at least {min}, so the model learns to ignore these words.", { n: total, min: status.targets.sound_alikes })}</p>
        {/if}
        <div class="actions">
          {#if running}
            <button class="danger" onclick={stop} disabled={busy}><Icon name="stop" size={15} /> {t("Stop")}</button>
          {:else}
            <button class="primary" onclick={start} disabled={busy || !!blocker || total >= params.count}>
              <Icon name="sparkles" size={16} /> {label}
            </button>
          {/if}
        </div>
        {#if blocker}
          <p class="hint warn">{t("The GPU is busy: {kind} job of \"{project}\" is running.", { kind: blocker.kind, project: blocker.project })}</p>
        {/if}
        {#if newWords}
          <p class="hint warn">{t("Sound-alikes were added since the last run; they are used for newly generated clips only.")}</p>
        {/if}
      </div>

      <div class="col">
        <div class="big">
          <div><strong class="mono">{shown.toLocaleString(locale())}</strong><span class="dim">{t("clips")}</span></div>
          <div><strong class="mono">{(status.confusables.seconds / 3600).toFixed(1)}</strong><span class="dim">{t("hours of audio")}</span></div>
        </div>
        {#if job}
          <div class="bar" aria-label={t("Progress {pct}%", { pct: pct.toFixed(0) })}>
            <span class="prev" style:width="{startPct}%"></span>
            <span class="fill" class:live={running} style:left="{startPct}%" style:width="{Math.max(0, pct - startPct)}%"></span>
          </div>
          <JobStats {job} {running} dropTitle={t("Dropped by length, or because they sounded like the wake word")} />
          {#if job.state === "failed" && job.message}
            <p class="hint err">{t(job.message)}</p>
          {/if}
          <button class="ghost toggle" onclick={() => (showLog = !showLog)}>
            {t("Log")} <span class="chev" class:open={showLog}><Icon name="chevron" size={14} /></span>
          </button>
          {#if showLog}
            <pre class="log mono">{job.log || t("(empty)")}</pre>
          {/if}
        {/if}
        {#if total && !running}
          <button class="danger small reset" onclick={reset} disabled={busy}><Icon name="trash" size={14} /> {t("Delete sound-alike clips")}</button>
        {/if}
      </div>
    </div>

    {#if total}
      <div class="listen">
        <div class="listen-head">
          <span class="label">{t("Listen")} <span class="faint">{t("(Δ = distance to the wake word, all clips are > 0.30)")}</span></span>
          <button class="small" onclick={draw} disabled={busy}><Icon name="replay" size={14} /> {t("Draw 12")}</button>
        </div>
        <div class="grid">
          {#each samples as c (c.url)}
            <MiniClip url={c.url} label={voice(c.voice)} detail="{c.seconds}s · Δ {c.distance?.toFixed(2) ?? '–'}" note={c.phrase ?? ""} miss />
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</section>

<style>
  .conf {
    padding: 20px 22px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .head,
  .listen-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
  }
  .listen-head {
    align-items: center;
  }
  .sub {
    margin: 6px 0 0;
    font-size: 13px;
    line-height: 1.5;
    max-width: 720px;
  }
  .cols {
    display: grid;
    grid-template-columns: minmax(300px, 420px) 1fr;
    gap: 28px;
  }
  .col {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .words {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    max-height: 120px;
    overflow: auto;
  }
  .small {
    font-size: 12.5px;
    margin: 0;
  }
  .small a,
  .dim a {
    color: var(--cyan);
  }
  .presets {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .presets input {
    width: 110px;
  }
  .presets .sel {
    border-color: var(--magenta);
    box-shadow: var(--glow-magenta);
  }
  .actions button,
  .reset,
  .listen-head button,
  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .reset {
    align-self: flex-start;
  }
  .toggle {
    align-self: flex-start;
    padding: 4px 8px;
    font-size: 12.5px;
  }
  .chev {
    display: inline-flex;
    transition: transform 0.15s;
  }
  .chev.open {
    transform: rotate(90deg);
  }
  .hint {
    margin: 0;
    font-size: 12.5px;
  }
  .hint.warn {
    color: var(--amber);
  }
  .hint.err {
    color: var(--danger);
  }
  .big {
    display: flex;
    gap: 28px;
  }
  .big div {
    display: flex;
    flex-direction: column;
  }
  .big strong {
    font-size: 28px;
    color: var(--magenta);
    text-shadow: var(--glow-magenta);
  }
  .big span {
    font-size: 12px;
  }
  .bar {
    position: relative;
    height: 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.06);
    overflow: hidden;
  }
  .bar span {
    position: absolute;
    top: 0;
    bottom: 0;
  }
  .bar .prev {
    left: 0;
    background: rgba(255, 61, 242, 0.25);
  }
  .bar .fill {
    background: linear-gradient(90deg, var(--violet), var(--magenta));
  }
  .bar .fill.live {
    box-shadow: var(--glow-magenta);
  }
  .log {
    margin: 0;
    max-height: 220px;
    overflow: auto;
    font-size: 11.5px;
    padding: 10px;
    border-radius: var(--radius-sm);
    background: rgba(0, 0, 0, 0.35);
    white-space: pre-wrap;
  }
  .label {
    font-size: 12.5px;
    color: var(--text-dim);
  }
  .listen {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
    gap: 10px;
  }
  @media (max-width: 1050px) {
    .cols {
      grid-template-columns: 1fr;
    }
  }
</style>
