<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../../components/Icon.svelte";
  import HitReview from "../../components/mining/HitReview.svelte";
  import { jobs, mining } from "../../lib/api";
  import { duration, finishAt } from "../../lib/format";
  import { guide, refreshGuide } from "../../lib/guide.svelte";
  import { locale, plural, t } from "../../lib/i18n.svelte";
  import { href } from "../../lib/router.svelte";
  import { app, toast, toastError } from "../../lib/state.svelte";
  import type { MinedSoundAlike, MiningRun, MiningStatus, Project } from "../../lib/types";

  let { project }: { project: Project } = $props();

  const NEAR_MISS = 0.1;

  let status = $state<MiningStatus | null>(null);
  let model = $state("");
  let chosen = $state<string[]>([]);
  let cutoff = $state<number | null>(null);
  let auto = $state(true);
  let selected = $state<string | null>(null);
  let busy = $state(false);
  let showLog = $state(false);
  // Phrases of the false triggers that would make good sound-alikes, and which of them are ticked
  let alikes = $state<MinedSoundAlike[]>([]);
  let picked = $state<string[]>([]);

  const job = $derived(status?.job ?? null);
  const running = $derived(!!job && job.alive && (job.state === "running" || job.state === "starting"));
  const blocker = $derived(status?.active && status.active.id !== job?.id ? status.active : null);
  const current = $derived(status?.models.find((m) => m.run === model) ?? null);
  const hours = $derived(status?.sources.filter((s) => chosen.includes(s.id)).reduce((a, s) => a + s.hours, 0) ?? 0);
  // The last job's report, unless its scan was deleted since (a failed job without a scan still shows).
  const report = $derived(job && (running || !job.mine || status?.runs.some((r) => r.id === job.mine)) ? job : null);
  const pct = $derived(job?.total_s ? Math.min(100, ((job.done_s ?? 0) / job.total_s) * 100) : 0);
  const selectedRun = $derived(status?.runs.find((r) => r.id === selected) ?? null);

  async function refresh() {
    try {
      const wasRunning = running;
      const s = await mining.status();
      if (!status) {
        model = s.models[0]?.run ?? "";
        chosen = s.sources.map((x) => x.id);
        selected = s.runs[0]?.id ?? null;
      }
      // On open and after a scan (its check labels triggers): the offered sound-alikes may have changed
      if (!status || (wasRunning && !s.job?.alive)) loadAlikes();
      status = s;
      if (wasRunning && s.job && !s.job.alive) {
        if (s.job.state === "failed") toastError(s.job.message ? serverText(s.job.message) : t("Mining failed"));
        else if (s.job.state !== "done") toast(t("Mining stopped."));
        else if (s.job.triage?.negative != null) toast(t("Done: {negative} false triggers labeled automatically, {review} to check.", { negative: s.job.triage.negative, review: s.job.triage.review }));
        else toast(plural(s.job.hits ?? 0, "Done: {n} trigger to review.", "Done: {n} triggers to review."));
        if (s.job.mine) selected = s.job.mine;
        refreshGuide(project.slug);
      }
    } catch (e) {
      toastError(e);
    }
  }

  $effect(() => {
    untrack(() => refresh());
    const id = setInterval(() => (running || blocker) && refresh(), 1000);
    return () => clearInterval(id);
  });

  // The threshold follows the chosen model until it is edited.
  $effect(() => {
    const m = current;
    untrack(() => (cutoff = m ? m.cutoff : null));
  });

  async function loadAlikes() {
    try {
      setAlikes((await mining.soundAlikes()).candidates);
    } catch (e) {
      toastError(e);
    }
  }

  function setAlikes(list: MinedSoundAlike[]) {
    const known = new Set(alikes.map((a) => a.text));
    // New phrases start ticked; the user's choice for the ones already shown stays
    picked = list.filter((a) => (known.has(a.text) ? picked.includes(a.text) : true)).map((a) => a.text);
    alikes = list;
  }

  async function takeAlikes() {
    const add = alikes.filter((a) => picked.includes(a.text)).map((a) => a.text);
    const reject = alikes.filter((a) => !picked.includes(a.text)).map((a) => a.text);
    const res = await act(() => mining.takeSoundAlikes(add, reject));
    if (!res) return;
    alikes = res.candidates;
    picked = res.candidates.map((a) => a.text);
    if (app.project?.slug === project.slug) {
      app.project.confusables = res.project.confusables;
      app.project.confusables_rejected = res.project.confusables_rejected;
    }
    if (add.length) toast(plural(add.length, "{n} sound-alike added. Generate its clips, then train again.", "{n} sound-alikes added. Generate their clips, then train again."));
    else toast(t("Dismissed; these phrases are not offered again."));
    refreshGuide(project.slug);
  }

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
    if (!current) return;
    const c = cutoff != null && Math.abs(cutoff - current.cutoff) > 1e-9 ? cutoff : null;
    if (await act(() => mining.start(model, chosen, c, auto))) {
      showLog = false;
      await refresh();
    }
  }

  async function check(r: MiningRun) {
    if (await act(() => mining.check(r.id))) {
      selected = r.id;
      showLog = false;
      await refresh();
    }
  }

  async function stop() {
    if (job) await act(() => jobs.stop(job.id));
    await refresh();
  }

  async function remove(r: MiningRun) {
    const kept = r.counts.negative + r.counts.positive;
    const keep = kept ? ` ${plural(kept, "The {n} clip you already labeled stays in training.", "The {n} clips you already labeled stay in training.")}` : "";
    if (!confirm(t("Delete the scan of {date}?", { date: r.started.replace("T", " ") }) + keep)) return;
    if (await act(() => mining.deleteRun(r.id))) {
      if (selected === r.id) selected = null;
      await refresh();
    }
  }

  function onrun(r: MiningRun, totals: MiningStatus["totals"]) {
    if (!status) return;
    status.runs = status.runs.map((x) => (x.id === r.id ? r : x));
    status.totals = totals;
    if (app.project?.slug === project.slug) app.project.summary.mined = totals.negatives;
    refreshGuide(project.slug);
    loadAlikes();
  }

  // Where the mining loop stands: scan -> automatic check -> you check the rest -> sound-alikes -> train again -> scan again.
  // $derived so the labels follow the UI language.
  const LOOP = $derived([
    { id: "scan", label: t("Scan"), text: t("the newest model listens to hours of real speech") },
    { id: "check", label: t("Automatic check"), text: t("clear false triggers are labeled for you") },
    { id: "review", label: t("You check the rest"), text: t("listen to what the check flagged") },
    { id: "alikes", label: t("Sound-alikes"), text: t("what fired becomes words to generate") },
    { id: "train", label: t("Train again"), text: t("only a new model learns from the clips") },
    { id: "rescan", label: t("Scan again"), text: t("1-2 rounds are enough, then it hardly improves") },
  ]);
  const minedNew = $derived(
    guide.slug === project.slug ? guide.data?.changes.filter((c) => c.id.startsWith("mined-")).reduce((a, c) => a + c.now - c.before, 0) ?? 0 : 0,
  );
  const openTotal = $derived(status?.runs.reduce((a, r) => a + r.counts.open, 0) ?? 0);
  // After the recommended rounds further scans find hardly anything new: the loop ends, the device test begins
  const miningDone = $derived(!!status && status.rounds >= status.recommended_rounds);
  // A round starts with the scan of a new model and ends with training again
  const round = $derived(
    status ? status.rounds + (status.runs.some((r) => r.run === status!.models[0]?.run && r.state === "done") ? 0 : 1) : 1,
  );
  const stage = $derived(
    running ? (job?.phase === "triage" ? "check" : "scan")
    : openTotal ? "review"
    : alikes.length ? "alikes"
    : minedNew > 0 ? "train"
    : status?.runs.length ? "rescan"
    : "scan",
  );

  const when = (iso: string) => new Date(iso).toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" });
  const modelLabel = (run: string) => {
    const m = status?.models.find((x) => x.run === run);
    return m ? `${serverText(m.profile)} · ${when(m.started)}` : run;
  };
  const sourceLabel = (id: string) => serverText(status?.sources.find((s) => s.id === id)?.label ?? id);

  // Static English from the server (job states and messages, source labels, profile names); anything else passes through.
  function serverText(text: string): string {
    const known: Record<string, string> = {
      starting: t("starting"),
      running: t("running"),
      done: t("done"),
      stopped: t("stopped"),
      failed: t("failed"),
      fast: t("fast"),
      thorough: t("thorough"),
      "listing the audio": t("listing the audio"),
      "starting the automatic check": t("starting the automatic check"),
      "loading the phone recognizer": t("loading the phone recognizer"),
      "checking the phones": t("checking the phones"),
      "automatic check skipped: the py-omni launcher is missing": t("automatic check skipped: the py-omni launcher is missing"),
      "Kartoffelphon (German podcasts + LibriVox)": t("Kartoffelphon (German podcasts + LibriVox)"),
      "with transcripts; also the source of the voice bank": t("with transcripts; also the source of the voice bank"),
    };
    if (known[text]) return known[text];
    let m: RegExpMatchArray | null;
    if ((m = text.match(/^starting (\d+) workers$/))) return t("starting {n} workers", { n: Number(m[1]) });
    if ((m = text.match(/^transcribing (\d+) triggers without a transcript \(Whisper\)$/)))
      return t("transcribing {n} triggers without a transcript (Whisper)", { n: Number(m[1]) });
    if ((m = text.match(/^automatic check failed \(exit (-?\d+)\), see log; review the triggers by hand or run it again$/)))
      return t("automatic check failed (exit {code}), see log; review the triggers by hand or run it again", { code: m[1] });
    return text;
  }
</script>

{#if status}
<div class="page">
  <p class="rounds hint">
    {#if miningDone}{plural(status.rounds, "{n} round done:", "{n} rounds done:")}{:else}{t("Round {n} of {max}:", { n: round, max: status.recommended_rounds })}{/if}
    {t("one or two rounds of scanning, reviewing and training again make a model robust; after that it hardly improves.")}
  </p>
  <ol class="loop" aria-label={t("How mining works")}>
    {#each LOOP as l, i (l.id)}
      <li class:now={stage === l.id} class:past={LOOP.findIndex((x) => x.id === stage) > i}>
        <span class="n mono">{i + 1}</span>
        <span class="t"><strong>{l.label}</strong><span>{l.text}</span></span>
      </li>
    {/each}
  </ol>

  <div class="top">
    <section class="panel run">
      <span class="eyebrow">{t("Scan")}</span>
      {#if !status.models.length}
        <p class="dim">
          {t("No trained model yet. Mining runs its streaming model over real speech.")}
          <a href={href.project(project.slug, "train")}>{t("Train one first")}</a>.
        </p>
      {:else}
        <label class="field">
          <span class="label">{t("Model")}</span>
          <select bind:value={model} disabled={running}>
            {#each status.models as m (m.run)}
              <option value={m.run}>
                {serverText(m.profile)} · {when(m.started)} · {t("cutoff {cutoff} / window {window}", { cutoff: m.cutoff.toFixed(2), window: m.window })}{m.recall != null ? ` · ${t("recall {pct}%", { pct: (m.recall * 100).toFixed(1) })}` : ""}
              </option>
            {/each}
          </select>
        </label>

        <div class="field">
          <span class="label">{t("Audio to scan")}</span>
          {#each status.sources as s (s.id)}
            <label class="check">
              <input type="checkbox" value={s.id} bind:group={chosen} disabled={running} />
              <span>{serverText(s.label)} <span class="mono faint">{s.hours.toFixed(1)} h · {s.id === "kartoffelphon" ? plural(s.files, "{n} utterance", "{n} utterances") : plural(s.files, "{n} file", "{n} files")}</span>
                {#if s.note}<span class="faint"> · {serverText(s.note)}</span>{/if}</span>
            </label>
          {:else}
            <p class="hint warn">{t("No audio found.")}</p>
          {/each}
          <p class="hint faint">
            {t("More audio (TV, radio, your rooms): put .wav/.flac/.mp3/.ogg files into this folder:")} <span class="mono">data/datasets/mining/&lt;name&gt;/</span>
          </p>
        </div>

        <label class="check">
          <input type="checkbox" bind:checked={auto} disabled={running} />
          <span>
            {t("This audio does not contain the wake word")}
            <span class="faint">{t("(then the automatic check labels the clear false triggers itself and only leaves the suspicious ones for you; turn it off for recordings of your own rooms)")}</span>
          </span>
        </label>

        {#if current}
          <div class="field">
            <span class="label">{t("Trigger threshold")} <span class="faint">{t("(window average above which a trigger is collected)")}</span></span>
            <div class="presets">
              <button class="small" class:sel={cutoff === current.cutoff} onclick={() => (cutoff = current.cutoff)} disabled={running}>
                {t("Device ({cutoff})", { cutoff: current.cutoff.toFixed(2) })}
              </button>
              <button class="small" class:sel={cutoff === Math.round((current.cutoff - NEAR_MISS) * 100) / 100}
                onclick={() => (cutoff = Math.round((current.cutoff - NEAR_MISS) * 100) / 100)} disabled={running}>
                {t("Near misses ({cutoff})", { cutoff: (current.cutoff - NEAR_MISS).toFixed(2) })}
              </button>
              <input type="number" class="mono" min="0.3" max="1" step="0.01" bind:value={cutoff} disabled={running} aria-label={t("Trigger threshold")} />
            </div>
            <p class="hint faint">
              {t("The device cutoff finds what would fire on the ESP. A lower threshold also collects near misses: more to listen to, harder negatives.")}
            </p>
          </div>
        {/if}

        <div class="actions">
          {#if running}
            <button class="danger" onclick={stop} disabled={busy}><Icon name="stop" size={15} /> {t("Stop")}</button>
          {:else}
            <button class="primary" onclick={start} disabled={busy || !!blocker || !current || !chosen.length || cutoff == null}>
              <Icon name="radar" size={16} /> {t("Scan {hours} h", { hours: hours.toFixed(1) })}
            </button>
          {/if}
        </div>
        {#if blocker}
          <p class="hint warn">{t("Busy: {kind} job of \"{project}\" is running.", { kind: blocker.kind, project: blocker.project })}</p>
        {/if}
        <p class="hint faint">{t("Runs on the CPU like the device does (~1800× realtime with 12 workers): 33 h take about a minute.")}</p>
      {/if}
    </section>

    <section class="panel progress">
      <div class="head">
        <span class="eyebrow">{t("Mined clips")}</span>
        {#if report}
          <span class="chip {report.state === 'failed' ? 'neg' : running ? 'pos' : ''}"><span class="dot" class:blink={running}></span>{serverText(report.state)}</span>
        {/if}
      </div>
      <div class="big">
        <div class="neg"><strong class="mono">{status.totals.negatives.toLocaleString(locale())}</strong><span class="dim">{t("false triggers")}</span></div>
        <div><strong class="mono">{status.totals.positives.toLocaleString(locale())}</strong><span class="dim">{t("real wake words")}</span></div>
      </div>
      {#if miningDone && !running && !openTotal}
        <div class="retrain done">
          <span>{plural(status.rounds, "{n} mining round done: further rounds hardly improve the model. Export it and test it on the device; scan your own audio only if it fires on something specific.", "{n} mining rounds done: further rounds hardly improve the model. Export it and test it on the device; scan your own audio only if it fires on something specific.")}</span>
          <a class="go" href={href.project(project.slug, "export")}>{t("Export")} <Icon name="next" size={15} /></a>
        </div>
      {:else if minedNew > 0 && !running && !alikes.length}
        <div class="retrain">
          <span><strong>{minedNew.toLocaleString(locale())}</strong> {t("of them are not in any model yet: they only help after the next training run.")}</span>
          <a class="go" href={href.project(project.slug, "train")}>{t("Train again")} <Icon name="next" size={15} /></a>
        </div>
      {:else}
        <p class="hint faint">
          {t("Labeled clips join the next training run as their own sources.")}
          <a href={href.project(project.slug, "train")}>{t("Open the training step")}</a>
        </p>
      {/if}
      {#if report}
        {#if running && report.phase === "loading"}
          <p class="dim small">{report.message ? serverText(report.message) : t("Preparing")} · {duration(report.elapsed)}</p>
        {:else if report.phase === "triage" && report.triage}
          {@const tr = report.triage}
          <div class="bar" aria-label={t("Automatic check")}><span class="fill" class:live={running} style:width="{tr.total ? (tr.done / tr.total) * 100 : 0}%"></span></div>
          <div class="stats mono">
            {#if report.total_s}<span>{plural(report.hits ?? 0, "{hours} h scanned · {n} trigger", "{hours} h scanned · {n} triggers", { hours: ((report.done_s ?? 0) / 3600).toFixed(1) })}</span>{/if}
            <span><strong>{tr.done}</strong> / {tr.total} {t("checked")}</span>
            {#if tr.negative != null}<span class="trig">{t("{n} labeled automatically", { n: tr.negative })}</span><span class="open">{t("{n} to check", { n: tr.review })}</span>{#if tr.duplicate}<span class="faint">{t("{n} already labeled in another scan", { n: tr.duplicate })}</span>{/if}{/if}
            {#if running}<span>{t("running for {time}", { time: duration(report.elapsed) })}</span>{:else if report.elapsed != null}<span>{t("{state} after {time}", { state: serverText(report.state), time: duration(report.elapsed) })}</span>{/if}
          </div>
          {#if report.message}<p class="dim small">{serverText(report.message)}</p>{/if}
        {:else}
          <div class="bar" aria-label={t("Progress {pct}%", { pct: pct.toFixed(0) })}><span class="fill" class:live={running} style:width="{pct}%"></span></div>
          <div class="stats mono">
            <span><strong>{((report.done_s ?? 0) / 3600).toFixed(1)}</strong> / {((report.total_s ?? 0) / 3600).toFixed(1)} h</span>
            <span>{t("{rate}× realtime", { rate: report.rate ?? 0 })}</span>
            <span class="trig">{plural(report.hits ?? 0, "{n} trigger", "{n} triggers")}</span>
            {#if running}
              <span>{t("ETA {time}", { time: duration(report.eta) })}</span>
              <span>{t("done ≈ {time}", { time: finishAt(report.eta) })}</span>
            {:else if report.elapsed != null}
              <span>{t("{state} after {time}", { state: serverText(report.state), time: duration(report.elapsed) })}</span>
            {/if}
          </div>
        {/if}
        {#if report.state === "failed" && report.message}<p class="hint err">{serverText(report.message)}</p>{/if}
        <button class="ghost toggle" onclick={() => (showLog = !showLog)}>
          {t("Log")} <span class="chev" class:open={showLog}><Icon name="chevron" size={14} /></span>
        </button>
        {#if showLog}<pre class="log mono">{report.log || t("(empty)")}</pre>{/if}
      {/if}
    </section>
  </div>

  {#if alikes.length && !running}
    {@const rest = alikes.length - picked.length}
    <section class="panel block alikes">
      <div class="head">
        <span class="eyebrow">{t("Sound-alikes from false triggers")}</span>
        <button class="ghost small" onclick={() => (picked = picked.length ? [] : alikes.map((a) => a.text))}>
          {picked.length ? t("Untick all") : t("Tick all")}
        </button>
      </div>
      <p class="hint">
        {t("These phrases sat where the model fired and sound close to the wake word. As sound-alikes the voices speak each of them hundreds of times, which teaches the model far more than the few real clips. Phrases that contain the wake word itself are left out, they would contradict the positives.")}
      </p>
      <div class="picks">
        {#each alikes as a (a.text)}
          <label class="pick" class:on={picked.includes(a.text)} title={t("Phone distance to the wake word: {d} (lower is closer)", { d: a.distance.toFixed(2) })}>
            <input type="checkbox" value={a.text} bind:group={picked} />
            <span>{a.text}</span>
            {#if a.count > 1}<span class="mono faint">{a.count}×</span>{/if}
          </label>
        {/each}
      </div>
      <div class="actions row">
        <button class="primary" onclick={takeAlikes} disabled={busy}>
          {#if !picked.length}{t("Dismiss all {n}", { n: alikes.length })}
          {:else if !rest}{t("Add all {n} as sound-alikes", { n: alikes.length })}
          {:else}{t("Add {n}, dismiss {m}", { n: picked.length, m: rest })}{/if}
        </button>
        <span class="hint faint">{t("Unticked phrases are not offered again. The list itself is in the wake word step.")}</span>
      </div>
    </section>
  {/if}

  {#if status.runs.length}
    <section class="panel block">
      <span class="eyebrow">{t("Scans")}</span>
      <table class="runs">
        <thead>
          <tr><th>{t("Started")}</th><th>{t("Model")}</th><th>{t("Audio")}</th><th>{t("Threshold")}</th><th>{t("Triggers")}</th><th title={t("Triggers at the model's own cutoff per hour of audio")}>{t("At device cutoff")}</th><th>{t("Review")}</th><th></th></tr>
        </thead>
        <tbody>
          {#each status.runs as r (r.id)}
            <tr class:sel={selected === r.id} onclick={() => (selected = r.id)}>
              <td class="mono">{when(r.started)}</td>
              <td>{modelLabel(r.run)}</td>
              <td>{r.sources.map(sourceLabel).join(", ")} <span class="mono faint">{r.hours_done.toFixed(1)} h</span></td>
              <td class="mono">{r.mine_cutoff.toFixed(2)}</td>
              <td class="mono">{r.hits.toLocaleString(locale())}{r.overflow ? ` ${t("(+{n} not kept)", { n: r.overflow })}` : ""}</td>
              <td class="mono">{r.device_hits} · {r.faph != null ? `${r.faph.toFixed(2)}/h` : "–"}</td>
              <td class="mono">
                {#if r.counts.open}<span class="open">{t("{n} to check", { n: r.counts.open })}</span>{:else}<span class="ok">{t("done")}</span>{/if}
                <span class="faint"> · {r.counts.negative} neg{r.counts.auto ? ` (${r.counts.auto} auto)` : ""} · {r.counts.positive} pos</span>
                {#if r.hits && !r.triaged && !(running && job?.mine === r.id)}
                  <button class="small check-btn" onclick={(e) => { e.stopPropagation(); check(r); }} disabled={busy || running || !!blocker}
                    title={t("Label the clear false triggers automatically, flag the suspicious ones")}>{t("Check automatically")}</button>
                {/if}
              </td>
              <td>
                <button class="ghost small" title={t("Delete scan")} onclick={(e) => { e.stopPropagation(); remove(r); }} disabled={busy || (running && job?.mine === r.id)}>
                  <Icon name="trash" size={14} />
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/if}

  {#if selectedRun && !(running && job?.mine === selectedRun.id)}
    {#key selectedRun.id}
      <HitReview run={selectedRun} {onrun} />
    {/key}
  {/if}
</div>
{/if}

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 22px;
  }
  .loop {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 8px;
  }
  .loop li {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line);
    background: rgba(5, 4, 16, 0.35);
    color: var(--text-dim);
    font-size: 12.5px;
    line-height: 1.4;
  }
  .loop li.past {
    opacity: 0.55;
  }
  .loop li.now {
    border-color: var(--cyan);
    background: rgba(41, 240, 255, 0.08);
    color: var(--text);
    box-shadow: 0 0 18px -8px rgba(41, 240, 255, 0.8);
  }
  .loop .n {
    flex: none;
    display: grid;
    place-items: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: 1px solid var(--line-strong);
    font-size: 11px;
  }
  .loop li.now .n {
    border-color: var(--cyan);
    color: var(--cyan);
  }
  .loop .t {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .loop strong {
    font-weight: 600;
    color: var(--text);
  }
  .retrain {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    border: 1px solid rgba(255, 190, 70, 0.45);
    background: rgba(255, 190, 70, 0.07);
    font-size: 13px;
    line-height: 1.5;
  }
  .retrain.done {
    border-color: rgba(41, 240, 255, 0.45);
    background: rgba(41, 240, 255, 0.07);
  }
  .retrain.done .go {
    border-color: var(--cyan);
    color: var(--cyan);
  }
  .rounds {
    margin: 0 0 -10px;
    color: var(--text-dim);
  }
  .retrain strong {
    color: var(--amber);
  }
  .retrain .go {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 13px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--amber);
    color: var(--amber);
    text-decoration: none;
    white-space: nowrap;
  }
  .retrain .go:hover {
    background: rgba(255, 190, 70, 0.12);
  }
  .top {
    display: grid;
    grid-template-columns: minmax(340px, 480px) 1fr;
    gap: 22px;
    align-items: start;
  }
  .run,
  .progress,
  .block {
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
  .field {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .label {
    font-size: 12.5px;
    color: var(--text-dim);
  }
  select {
    width: 100%;
  }
  .check {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
    line-height: 1.45;
  }
  .check input {
    accent-color: var(--cyan);
    margin-top: 3px;
  }
  .presets {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .presets input {
    width: 90px;
  }
  .sel {
    border-color: var(--cyan);
    color: var(--cyan);
    background: rgba(41, 240, 255, 0.1);
  }
  .hint {
    font-size: 12.5px;
    margin: 0;
    line-height: 1.5;
  }
  .hint a,
  .dim a {
    color: var(--cyan);
  }
  .hint.warn {
    color: var(--amber);
  }
  .hint.err {
    color: var(--danger);
  }
  .small {
    margin: 0;
    font-size: 13px;
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
  .fill {
    position: absolute;
    inset: 0 auto 0 0;
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
  .stats {
    display: flex;
    gap: 22px;
    flex-wrap: wrap;
    font-size: 13px;
    color: var(--text-dim);
  }
  .stats strong {
    color: var(--text);
    font-weight: 600;
  }
  .trig {
    color: var(--magenta);
  }
  .blink {
    animation: blink 1s steps(2) infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0.2;
    }
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
  .runs {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .runs th {
    text-align: left;
    font-weight: 500;
    color: var(--text-dim);
    font-size: 12px;
    padding: 6px 10px;
    border-bottom: 1px solid var(--line);
  }
  .runs td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
  }
  .runs tbody tr {
    cursor: pointer;
    transition: background 0.15s;
  }
  .runs tbody tr:hover {
    background: rgba(155, 107, 255, 0.08);
  }
  .runs tr.sel {
    background: rgba(41, 240, 255, 0.08);
    box-shadow: inset 3px 0 0 var(--cyan);
  }
  .open {
    color: var(--amber);
  }
  .check-btn {
    margin-left: 8px;
    padding: 3px 8px;
    font-size: 12px;
  }
  .ok {
    color: var(--cyan);
  }
  .picks {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .pick {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--line);
    font-size: 13px;
    cursor: pointer;
    color: var(--text-dim);
    transition: border-color 0.15s, background 0.15s;
  }
  .pick input {
    accent-color: var(--cyan);
    margin: 0;
  }
  .pick.on {
    border-color: var(--cyan);
    background: rgba(41, 240, 255, 0.08);
    color: var(--text);
  }
  .actions.row {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  @media (max-width: 1050px) {
    .top {
      grid-template-columns: 1fr;
    }
    .loop {
      grid-template-columns: 1fr 1fr;
    }
    .runs {
      display: block;
      overflow-x: auto;
    }
  }
</style>
