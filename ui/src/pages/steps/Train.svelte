<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../../components/Icon.svelte";
  import { jobs, train } from "../../lib/api";
  import { duration } from "../../lib/format";
  import { guide, refreshGuide } from "../../lib/guide.svelte";
  import { i18n, locale, t } from "../../lib/i18n.svelte";
  import { href } from "../../lib/router.svelte";
  import { app, toast, toastError } from "../../lib/state.svelte";
  import type { Project, TrainRun, TrainStatus, ValPoint } from "../../lib/types";

  let { project }: { project: Project } = $props();

  let status = $state<TrainStatus | null>(null);
  let profile = $state("fast");
  let targetFaph = $state(0.25);
  let busy = $state(false);
  let showLog = $state(false);
  let open = $state<string | null>(null);
  let detail = $state<TrainRun | null>(null);

  const job = $derived(status?.job ?? null);
  const running = $derived(!!job && job.alive && (job.state === "running" || job.state === "starting"));
  const blocker = $derived(status?.active && status.active.id !== job?.id ? status.active : null);
  const positives = $derived(status?.sources.filter((s) => s.truth) ?? []);
  const negatives = $derived(status?.sources.filter((s) => !s.truth) ?? []);
  const history = $derived<ValPoint[]>(job?.history ?? []);
  // Training data that changed since the newest model (trainer/guide.py)
  const changes = $derived(guide.slug === project.slug ? guide.data?.changes ?? [] : []);
  const changed = (id: string) => changes.find((c) => c.id === id);
  const hasModel = $derived(!!status?.runs.some((r) => r.state === "done" && r.calibration));
  // Source labels come from trainer/train/sources.py; borrowed ones carry the other project's slug
  function sourceLabel(s: { id: string; label: string }) {
    const m = s.id.match(/^borrowed:(.+):(recordings|synthetic)$/);
    return m ? t("Borrowed from {slug} ({kind})", { slug: m[1], kind: t(m[2]) }) : t(s.label);
  }
  // English labels read as nouns inside a sentence; German nouns keep their capital letter
  const inline = (s: { id: string; label: string }) => (i18n.lang === "de" ? sourceLabel(s) : sourceLabel(s).toLowerCase());

  const PHASES = [
    { id: "featurize", label: "Features" },
    { id: "train", label: "Training" },
    { id: "export", label: "TFLite export" },
    { id: "calibrate", label: "Calibration" },
  ];
  const phaseIndex = $derived(job?.phase ? (job.phase === "done" ? PHASES.length : PHASES.findIndex((p) => p.id === job.phase)) : -1);

  async function refresh() {
    try {
      const wasRunning = running;
      const s = await train.status();
      status = s;
      if (wasRunning && s.job && !s.job.alive) {
        if (s.job.state === "failed") toastError(s.job.message ?? t("Training failed"));
        else toast(s.job.state === "done" ? t("Training done, the model is ready.") : t("Training stopped."));
        const done = s.runs.find((r) => r.id === s.job?.run);
        if (done?.state === "done") toggle(done.id, true);
        if (app.project?.slug === project.slug) app.project.summary.runs = s.runs.filter((r) => r.files).length;
        refreshGuide(project.slug);
      }
    } catch (e) {
      toastError(e);
    }
  }

  $effect(() => {
    untrack(refresh);
    const id = setInterval(() => (running || blocker) && refresh(), 1500);
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
    if (await act(() => train.start(profile, targetFaph))) {
      showLog = false;
      await refresh();
    }
  }

  async function stop() {
    if (job && confirm(t("Stop the training? The run is discarded, cached features are kept."))) {
      await act(() => jobs.stop(job.id));
      await refresh();
    }
  }

  async function toggle(id: string, force = false) {
    if (open === id && !force) {
      open = null;
      return;
    }
    open = id;
    detail = null;
    const r = await act(() => train.run(id));
    if (r && open === id) detail = r;
  }

  async function remove(r: TrainRun) {
    if (!confirm(t("Delete run {id} and its model files?", { id: r.id }))) return;
    if (await act(() => train.deleteRun(r.id))) {
      if (open === r.id) open = null;
      await refresh();
    }
  }

  const pct = (x?: number) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);
  const when = (iso: string) => new Date(iso).toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" });
  const dur = (s?: number) => (s == null ? "" : duration(s));

  // Sparkline paths for the validation history (x: step, y: 0..1 or 0..maxFaph)
  function path(points: ValPoint[], value: (p: ValPoint) => number, max: number, w = 300, h = 70) {
    const last = points.at(-1)?.step || 1;
    return points.map((p, i) => `${i ? "L" : "M"}${((p.step / last) * w).toFixed(1)},${(h - (Math.min(value(p), max) / max) * h).toFixed(1)}`).join(" ");
  }
  const maxFaph = $derived(Math.max(1, ...history.map((p) => p.faph)));

  // Trade-off curve of a run: x false accepts/h (0..2), y recall
  function curvePath(run: TrainRun, w = 360, h = 160) {
    const pts = (run.calibration?.curve ?? []).filter((p) => p.faph <= 2).sort((a, b) => a.faph - b.faph);
    return pts.map((p, i) => `${i ? "L" : "M"}${((p.faph / 2) * w).toFixed(1)},${(h - p.recall * h).toFixed(1)}`).join(" ");
  }
</script>

{#if status}
<div class="page">
  <div class="top">
    <section class="panel run">
      <span class="eyebrow">{t("New run")}</span>
      <div class="profiles">
        {#each Object.entries(status.profiles) as [id, p] (id)}
          <button class="profile" class:sel={profile === id} onclick={() => (profile = id)} disabled={running}>
            <strong>{t(p.label)}</strong>
            <span class="mono">{t("{n}k steps", { n: (p.steps / 1000).toFixed(0) })}</span>
            <span class="faint">{t(p.hint)}</span>
          </button>
        {/each}
      </div>
      <label class="field">
        <span>{t("Target false accepts per hour")} <span class="faint">{t("(picks cutoff + window after training)")}</span></span>
        <input type="number" class="mono" min="0.05" max="5" step="0.05" bind:value={targetFaph} disabled={running} />
      </label>
      {#if hasModel && !running}
        {#if changes.length}
          <div class="callout warn">
            <strong>{t("New data since the last model")}</strong>
            <ul>
              {#each changes as c (c.id)}
                <li><span class="mono">{c.now - c.before > 0 ? "+" : ""}{(c.now - c.before).toLocaleString(locale())}</span> {inline(c)}</li>
              {/each}
            </ul>
            <span>{t("Clips only take effect in a new run. Train again, then scan the new model in the mining step.")}</span>
          </div>
        {:else}
          <p class="hint faint">{t("The newest model already contains all current training data.")}</p>
        {/if}
      {/if}
      <div class="actions">
        {#if running}
          <button class="danger" onclick={stop} disabled={busy}><Icon name="stop" size={15} /> {t("Stop")}</button>
        {:else}
          <button class="primary" onclick={start} disabled={busy || !!blocker || status.problems.length > 0}>
            <Icon name="pulse" size={16} /> {t("Train {profile} model", { profile: t(status.profiles[profile]?.label ?? "").toLowerCase() })}
          </button>
        {/if}
      </div>
      {#each status.problems as p (p)}
        <p class="hint err">{t(p)}</p>
      {/each}
      {#if blocker}
        <p class="hint warn">{t("The GPU is busy: {kind} job of \"{project}\" is running.", { kind: blocker.kind, project: blocker.project })}</p>
      {/if}
    </section>

    <section class="panel progress">
      <div class="head">
        <span class="eyebrow">{t("Progress")}</span>
        {#if job}
          <span class="chip {job.state === 'failed' ? 'neg' : running ? 'pos' : ''}"><span class="dot" class:blink={running}></span>{t(job.state)}</span>
        {/if}
      </div>
      {#if job}
        <ol class="phases">
          {#each PHASES as p, i (p.id)}
            <li class:done={i < phaseIndex} class:now={i === phaseIndex && running}>{t(p.label)}</li>
          {/each}
        </ol>
        {#if job.phase === "featurize" && job.featurize}
          {@const f = job.featurize}
          <div class="bar"><span class="fill live" style:width="{f.total ? (f.done / f.total) * 100 : 100}%"></span></div>
          <div class="stats mono">
            <span>{t("{done} / {total} clips", { done: f.done, total: f.total })}</span>
            <span>{t("{n} clips/s", { n: f.rate })}</span>
            <span>{t("ETA {time}", { time: duration(f.eta) })}</span>
          </div>
        {:else if job.steps}
          <div class="bar"><span class="fill" class:live={running} style:width="{Math.min(100, ((job.step ?? 0) / job.steps) * 100)}%"></span></div>
          <div class="stats mono">
            <span>{t("step {step} / {steps}", { step: job.step ?? 0, steps: job.steps })}</span>
            {#if job.phase === "train"}<span>{t("{n} steps/s", { n: job.rate ?? 0 })}</span><span>{t("ETA {time}", { time: running ? duration(job.eta) : "–" })}</span>{/if}
          </div>
        {/if}
        {#if job.message}<p class="dim small">{t(job.message)}</p>{/if}
        {#if history.length > 1}
          <div class="charts">
            <figure>
              <figcaption>{t("Validation recall")} <span class="mono">{pct(history.at(-1)?.recall)}</span></figcaption>
              <svg viewBox="0 0 300 70" preserveAspectRatio="none"><path d={path(history, (p) => p.recall, 1)} class="l-cyan" /></svg>
            </figure>
            <figure>
              <figcaption>{t("False accepts/h (raw, no window)")} <span class="mono">{history.at(-1)?.faph.toFixed(2)}</span></figcaption>
              <svg viewBox="0 0 300 70" preserveAspectRatio="none"><path d={path(history, (p) => p.faph, maxFaph)} class="l-magenta" /></svg>
            </figure>
          </div>
        {/if}
        {#if job.state === "failed" && job.message}<p class="hint err">{job.message}</p>{/if}
        <button class="ghost toggle" onclick={() => (showLog = !showLog)}>
          {t("Log")} <span class="chev" class:open={showLog}><Icon name="chevron" size={14} /></span>
        </button>
        {#if showLog}<pre class="log mono">{job.log || t("(empty)")}</pre>{/if}
      {:else}
        <p class="dim">{t("No run yet. A fast run takes about half an hour on the RTX 3090, features are cached for the next one.")}</p>
      {/if}
    </section>
  </div>

  <section class="panel data">
    <div class="head">
      <span class="eyebrow">{t("Training data")}</span>
      <span class="faint small">{t("sampling weight = share of each training batch · penalty = loss weight of a mistake")}</span>
    </div>
    <div class="cols">
      {#each [{ title: "Wake word", list: positives, cls: "pos" }, { title: "Not the wake word", list: negatives, cls: "neg" }] as g (g.title)}
        <div>
          <h3 class={g.cls}>{t(g.title)}</h3>
          <table>
            <thead><tr><th>{t("Source")}</th><th class="num">{t("Clips")}</th><th class="num">{t("Weight")}</th><th class="num">{t("Penalty")}</th><th>{t("Features")}</th></tr></thead>
            <tbody>
              {#each g.list as s (s.id)}
                <tr>
                  <td>{sourceLabel(s)}{#if s.note}<span class="faint"> · {t(s.note)}</span>{/if}</td>
                  <td class="num mono">
                    {s.clips.toLocaleString(locale())}
                    {#if hasModel && changed(s.id)}
                      {@const c = changed(s.id)!}
                      <span class="new" title={t("Not in the newest model yet (it had {n})", { n: c.before })}>{t("{n} new", { n: `${c.now - c.before > 0 ? "+" : ""}${(c.now - c.before).toLocaleString(locale())}` })}</span>
                    {/if}
                  </td>
                  <td class="num mono">{s.sampling_weight}</td>
                  <td class="num mono">{s.penalty_weight}</td>
                  <td class="mono faint">{s.prebuilt ? t("prebuilt") : t("{cached}/{parts} cached", { cached: s.cached, parts: s.parts })}</td>
                </tr>
              {:else}
                <tr><td colspan="5" class="faint">{t("none")}</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/each}
    </div>
    <p class="faint small">
      {t("More data:")} <a href={href.project(project.slug, "generate")}>{t("synthetic samples and sound-alikes")}</a> ·
      <a href={href.project(project.slug, "record")}>{t("own recordings and borrowed negatives")}</a>
    </p>
  </section>

  <section class="panel runs">
    <span class="eyebrow">{t("Runs")}</span>
    {#if !status.runs.length}
      <p class="dim">{t("Trained models appear here with their measured recall and false accepts per hour.")}</p>
    {/if}
    {#each status.runs as r (r.id)}
      {@const c = r.calibration}
      <div class="runrow" class:open={open === r.id}>
        <button class="summary" onclick={() => toggle(r.id)}>
          <span class="chev" class:open={open === r.id}><Icon name="chevron" size={14} /></span>
          <span class="when">{when(r.started)}</span>
          <span class="chip">{r.profile} · {(r.steps / 1000).toFixed(0)}k</span>
          <span class="chip {r.state === 'done' ? 'pos' : r.state === 'failed' ? 'neg' : ''}">{t(r.state)}</span>
          {#if c}
            <span class="metric"><span class="faint">{t("recall")}</span> <strong class="mono">{pct(c.recall)}</strong></span>
            <span class="metric"><span class="faint">FA/h</span> <strong class="mono" class:warn={!c.met_target}>{c.false_accepts_per_hour.toFixed(2)}</strong></span>
            <span class="metric"><span class="faint">{t("cutoff")}</span> <span class="mono">{c.probability_cutoff} · w{c.sliding_window_size}</span></span>
          {/if}
          <span class="spacer"></span>
          {#if r.seconds}<span class="faint mono small">{dur(r.seconds)}</span>{/if}
        </button>
        {#if open === r.id}
          <div class="detail">
            {#if r.error}<p class="hint err">{r.error}</p>{/if}
            {#if detail?.id === r.id && detail.calibration}
              {@const dc = detail.calibration}
              <div class="detail-cols">
                <div>
                  <span class="label">{t("Recall vs. false accepts per hour (window {window}, {hours} h ambient)", { window: dc.sliding_window_size, hours: dc.ambient_hours })}</span>
                  <svg class="curve" viewBox="-30 -10 400 195">
                    <line x1="0" y1="160" x2="360" y2="160" class="axis" />
                    <line x1="0" y1="0" x2="0" y2="160" class="axis" />
                    {#each [0, 0.5, 1, 1.5, 2] as t (t)}<text x={(t / 2) * 360} y="176" class="tick">{t}</text>{/each}
                    {#each [0, 0.5, 1] as t (t)}<text x="-6" y={160 - t * 160 + 4} class="tick end">{t * 100}%</text>{/each}
                    <line x1={(dc.target_false_accepts_per_hour / 2) * 360} y1="0" x2={(dc.target_false_accepts_per_hour / 2) * 360} y2="160" class="target" />
                    <path d={curvePath(detail)} class="l-cyan" />
                    <circle cx={(Math.min(dc.false_accepts_per_hour, 2) / 2) * 360} cy={160 - dc.recall * 160} r="5" class="sel" />
                  </svg>
                </div>
                <div>
                  <span class="label">{t("Held-out test clips at the chosen operating point")}</span>
                  <table>
                    <tbody>
                      {#each dc.sources as s (s.id)}
                        <tr>
                          <td>{sourceLabel(s)}</td>
                          <td class="num mono faint">{s.clips}</td>
                          <td class="num mono {s.truth ? 'pos' : 'neg'}">
                            {s.truth ? t("{pct} detected", { pct: pct(s.recall) }) : t("{pct} trigger", { pct: pct(s.trigger_rate) })}
                          </td>
                        </tr>
                      {/each}
                      <tr>
                        <td>{t("Ambient audio (dinner party, DiPCo)")}</td>
                        <td class="num mono faint">{dc.ambient_hours} h</td>
                        <td class="num mono neg">{dc.false_accepts_per_hour.toFixed(2)} / h</td>
                      </tr>
                    </tbody>
                  </table>
                  {#if !dc.met_target}<p class="hint warn">{t("No operating point met {target} FA/h; this is the best fallback.", { target: dc.target_false_accepts_per_hour })}</p>{/if}
                  {#each detail.checkpoints ?? [] as k (k.name)}
                    <p class="hint">
                      {t(k.name === "last" ? "Last checkpoint (step {step})" : "Best validation checkpoint (step {step})", { step: k.step ?? "–" })}:
                      <span class="mono">{pct(k.recall)}</span> {t("recall")}, <span class="mono">{k.false_accepts_per_hour.toFixed(2)}</span> FA/h{#if k.name === detail.checkpoint}, <strong>{t("used as the model")}</strong>{/if}
                    </p>
                  {/each}
                </div>
              </div>
            {:else if !r.error}
              <p class="dim small">{r.state === "running" ? t("Still training.") : t("Loading…")}</p>
            {/if}
            <div class="files">
              {#if r.files}
                <a class="btn primary small" href={train.fileUrl(r.id, r.files.model)} download><Icon name="download" size={14} /> {r.files.model}</a>
                <a class="btn small" href={train.fileUrl(r.id, r.files.manifest)} download><Icon name="download" size={14} /> {r.files.manifest}</a>
                <span class="faint small mono">{(r.files.size / 1024).toFixed(0)} KB</span>
              {/if}
              <span class="spacer"></span>
              {#if r.state !== "running"}
                <button class="danger small" onclick={() => remove(r)} disabled={busy}><Icon name="trash" size={14} /> {t("Delete run")}</button>
              {/if}
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </section>
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
  .data,
  .runs {
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
  .profiles {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .profile {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    min-width: 0;
    text-align: left;
    white-space: normal;
    padding: 12px 14px;
  }
  .profile:disabled {
    opacity: 0.75;
  }
  .profile .faint {
    font-size: 12px;
    line-height: 1.4;
  }
  .profile.sel {
    border-color: var(--cyan);
    box-shadow: var(--glow-cyan);
    background: rgba(41, 240, 255, 0.08);
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 12.5px;
    color: var(--text-dim);
  }
  .field input {
    width: 120px;
  }
  .actions button,
  .toggle,
  .files .btn,
  .files button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .hint {
    margin: 0;
    font-size: 12.5px;
  }
  .hint.warn,
  .warn {
    color: var(--amber);
  }
  .hint.err {
    color: var(--danger);
  }
  .small {
    font-size: 12.5px;
    margin: 0;
  }
  .phases {
    display: flex;
    gap: 8px;
    list-style: none;
    margin: 0;
    padding: 0;
    flex-wrap: wrap;
  }
  .phases li {
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--line);
    font-size: 12.5px;
    color: var(--text-faint);
  }
  .phases li.done {
    color: var(--cyan);
    border-color: rgba(41, 240, 255, 0.4);
  }
  .phases li.now {
    color: var(--text);
    border-color: var(--violet);
    box-shadow: var(--glow-violet);
  }
  .bar {
    position: relative;
    height: 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.06);
    overflow: hidden;
  }
  .bar .fill {
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    background: linear-gradient(90deg, var(--violet), var(--cyan));
    transition: width 0.4s;
  }
  .bar .fill.live {
    box-shadow: var(--glow-cyan);
  }
  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    font-size: 12.5px;
  }
  .charts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  figure {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  figcaption {
    font-size: 12px;
    color: var(--text-dim);
    display: flex;
    justify-content: space-between;
  }
  figure svg {
    width: 100%;
    height: 70px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 8px;
  }
  path {
    fill: none;
    stroke-width: 2;
    vector-effect: non-scaling-stroke;
  }
  .l-cyan {
    stroke: var(--cyan);
    filter: drop-shadow(0 0 4px rgba(41, 240, 255, 0.6));
  }
  .l-magenta {
    stroke: var(--magenta);
    filter: drop-shadow(0 0 4px rgba(255, 61, 242, 0.6));
  }
  .log {
    margin: 0;
    max-height: 260px;
    overflow: auto;
    font-size: 11.5px;
    padding: 10px;
    border-radius: var(--radius-sm);
    background: rgba(0, 0, 0, 0.35);
    white-space: pre-wrap;
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
  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }
  h3 {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 600;
  }
  h3.pos,
  td.pos {
    color: var(--cyan);
  }
  h3.neg,
  td.neg {
    color: var(--magenta);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  th {
    text-align: left;
    font-weight: 500;
    font-size: 11.5px;
    color: var(--text-faint);
    padding: 4px 6px;
    border-bottom: 1px solid var(--line);
  }
  td {
    padding: 6px;
    border-bottom: 1px solid rgba(150, 130, 255, 0.07);
  }
  .callout {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    line-height: 1.5;
  }
  .callout.warn {
    border: 1px solid rgba(255, 190, 70, 0.45);
    background: rgba(255, 190, 70, 0.07);
  }
  .callout strong {
    color: var(--amber);
    font-weight: 600;
  }
  .callout ul {
    margin: 0;
    padding-left: 18px;
  }
  .callout span {
    color: var(--text-dim);
  }
  .new {
    display: block;
    font-size: 11px;
    color: var(--amber);
  }
  .num {
    text-align: right;
  }
  a {
    color: var(--cyan);
  }
  .runrow {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02);
  }
  .runrow.open {
    border-color: var(--line-strong);
  }
  .summary {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    border: none;
    background: none;
    padding: 10px 14px;
    text-align: left;
  }
  .when {
    font-weight: 600;
  }
  .metric {
    font-size: 13px;
  }
  .metric strong {
    color: var(--text);
  }
  .spacer {
    flex: 1;
  }
  .detail {
    padding: 4px 16px 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .detail-cols {
    display: grid;
    grid-template-columns: minmax(300px, 420px) 1fr;
    gap: 24px;
  }
  .label {
    font-size: 12.5px;
    color: var(--text-dim);
    display: block;
    margin-bottom: 8px;
  }
  .curve {
    width: 100%;
    max-width: 420px;
  }
  .axis {
    stroke: var(--line-strong);
  }
  .target {
    stroke: var(--amber);
    stroke-dasharray: 4 4;
  }
  .tick {
    fill: var(--text-faint);
    font-size: 10px;
    text-anchor: middle;
    font-family: var(--font-mono);
  }
  .tick.end {
    text-anchor: end;
  }
  .sel {
    fill: var(--magenta);
    filter: drop-shadow(0 0 6px rgba(255, 61, 242, 0.8));
  }
  .files {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .btn {
    text-decoration: none;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    padding: 6px 12px;
    background: rgba(255, 255, 255, 0.04);
  }
  .btn.primary {
    border-color: var(--cyan);
    background: linear-gradient(135deg, rgba(41, 240, 255, 0.18), rgba(155, 107, 255, 0.18));
  }
  @media (max-width: 1050px) {
    .top,
    .cols,
    .detail-cols {
      grid-template-columns: 1fr;
    }
  }
</style>
