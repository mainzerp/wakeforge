<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../../components/Icon.svelte";
  import { models, projects } from "../../lib/api";
  import { guide, refreshGuide } from "../../lib/guide.svelte";
  import { locale, t } from "../../lib/i18n.svelte";
  import { href } from "../../lib/router.svelte";
  import { app, setProject, toast, toastError } from "../../lib/state.svelte";
  import type { CurvePoint, ExportRun, ModelExport, ModelsStatus, Project } from "../../lib/types";

  let { project }: { project: Project } = $props();

  let status = $state<ModelsStatus | null>(null);
  let runId = $state<string | null>(null);
  let idx = $state(0);
  let axis = $state<"ambient" | "speech">("ambient");
  let busy = $state(false);
  let host = $state(storedHost() ?? `${location.protocol}//${location.hostname}:8790`);
  let target = $state<"ha" | "git" | "pc">("ha");
  // Manifest credits (project.json): every new download writes the saved values
  let author = $state(untrack(() => project.author ?? ""));
  let website = $state(untrack(() => project.website ?? ""));
  const creditsDirty = $derived(author !== (project.author ?? "") || website !== (project.website ?? ""));
  // Git: raw.githubusercontent.com is checked for changes on every compile (github:// is cached for a day)
  let gitRepo = $state(untrack(() => githubRepo(project.website)));
  let gitBranch = $state("main");
  let gitPath = $state("models");

  const run = $derived<ExportRun | null>(status?.runs.find((r) => r.id === runId) ?? null);
  // Operating points sorted by cutoff: the slider walks from "detects more" to "fewer false alarms"
  const points = $derived<CurvePoint[]>(run ? [...run.calibration.curve].sort((a, b) => a.cutoff - b.cutoff) : []);
  const point = $derived<CurvePoint | null>(points[Math.min(idx, points.length - 1)] ?? null);
  const current = $derived<ModelExport | null>(status?.exports.find((e) => e.id === status?.current) ?? null);
  const changes = $derived(guide.slug === project.slug ? guide.data?.changes ?? [] : []);
  const isCalibrated = $derived(!!run && !!point && point.cutoff === run.calibration.probability_cutoff);
  const isCurrent = $derived(!!current && !!run && !!point && current.run === run.id && current.probability_cutoff === point.cutoff &&
    (current.author ?? "") === (project.author ?? "") && (current.website ?? "") === (project.website ?? ""));

  function githubRepo(url?: string): string {
    const m = url?.match(/github\.com\/([^/\s]+\/[^/#?\s]+)/);
    return m ? m[1].replace(/\.git$/, "") : "";
  }

  async function refresh() {
    try {
      status = await models.status();
      if (!status.runs.some((r) => r.id === runId) && status.runs.length) selectRun(status.runs[0]);
    } catch (e) {
      toastError(e);
    }
  }

  $effect(() => {
    untrack(refresh);
  });

  function selectRun(r: ExportRun) {
    runId = r.id;
    const sorted = [...r.calibration.curve].sort((a, b) => a.cutoff - b.cutoff);
    idx = Math.max(0, sorted.findIndex((p) => p.cutoff === r.calibration.probability_cutoff));
    if (!r.speech) axis = "ambient";
  }

  function pick(p: CurvePoint) {
    idx = points.indexOf(p);
  }

  /** False accepts per hour on the mining scan's speech; null below the scan's cutoff (lower scores were not kept). */
  function speechFaph(r: ExportRun | null, cutoff: number): number | null {
    const s = r?.speech;
    if (!s || s.min_cutoff == null || cutoff < s.min_cutoff) return null;
    return s.scores.filter((x) => x > cutoff).length / s.hours;
  }

  function download(id: string) {
    const a = document.createElement("a");
    a.href = models.zipUrl(id);
    a.download = "";
    a.click();
  }

  /** One click: export the chosen operating point (unless it is the current export already) and download the .zip */
  async function exportModel() {
    if (!run || !point) return;
    if (isCurrent && current) return download(current.id);
    busy = true;
    try {
      const e = await models.create(run.id, point.cutoff);
      download(e.id);
      toast(t("Exported with cutoff {cutoff}.", { cutoff: e.probability_cutoff }));
      await refresh();
      if (app.project?.slug === project.slug) app.project.summary.models = status?.exports.length ?? 0;
      refreshGuide(project.slug);
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  async function saveCredits() {
    busy = true;
    try {
      const p = await projects.update(project.slug, { author: author.trim(), website: website.trim() });
      if (app.project?.slug === p.slug) setProject(p);
      author = p.author ?? "";
      website = p.website ?? "";
      toast(t("Saved; the next download carries it."));
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  async function remove(e: ModelExport) {
    const msg = e.id === status?.current
      ? t("Delete export {id}? It is the current model; the previous export takes its place.", { id: e.id })
      : t("Delete export {id}?", { id: e.id });
    if (!confirm(msg)) return;
    busy = true;
    try {
      await models.remove(e.id);
      await refresh();
      if (app.project?.slug === project.slug) app.project.summary.models = status?.exports.length ?? 0;
      refreshGuide(project.slug);
    } catch (err) {
      toastError(err);
    } finally {
      busy = false;
    }
  }

  // The ESPHome host is a per-browser convenience (the LAN address of this PC)
  function storedHost(): string | null {
    try {
      return localStorage.getItem("wakeforge.esphomeHost");
    } catch {
      return null;
    }
  }
  function saveHost() {
    try {
      localStorage.setItem("wakeforge.esphomeHost", host);
    } catch {
      // not stored: the default comes back next time
    }
  }
  const localHost = $derived(/^https?:\/\/(localhost|127\.|\[::1\])/.test(host));

  const manifestName = $derived(current?.files.manifest ?? run?.files.manifest ?? `${project.slug}.json`);
  const yamlFiles = $derived(`micro_wake_word:\n  models:\n    - model: wakeforge/${manifestName}`);
  const rawUrl = $derived.by(() => {
    const folder = gitPath.trim().replace(/^\/+|\/+$/g, "");
    return `https://raw.githubusercontent.com/${gitRepo.trim() || "<owner>/<repo>"}/${gitBranch.trim() || "main"}/${folder ? `${folder}/` : ""}${manifestName}`;
  });
  const yamlGit = $derived(`micro_wake_word:\n  models:\n    - model: ${rawUrl}`);
  const yamlUrl = $derived(`micro_wake_word:\n  models:\n    - model: ${host.replace(/\/+$/, "")}${models.currentUrl(manifestName)}`);

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t("Copied."));
    } catch {
      toastError(t("Copying is blocked here; select the text and copy it by hand."));
    }
  }

  const pct = (x?: number | null) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);
  const rate = (x?: number | null) => (x == null ? "–" : x.toFixed(2));
  const when = (iso: string) => new Date(iso).toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" });

  // Chart: recall (y) against false accepts per hour (x) on the ambient set or the mining scan's speech
  const W = 360;
  const H = 160;
  const xOf = (p: CurvePoint) => (axis === "ambient" ? p.faph : speechFaph(run, p.cutoff));
  const xMax = $derived.by(() => {
    if (axis === "ambient") return 2;
    const top = Math.max(0, ...points.map((p) => speechFaph(run, p.cutoff) ?? 0));
    return [2, 5, 10, 20, 50].find((n) => n >= top) ?? 50;
  });
  const shown = $derived(points.filter((p) => {
    const x = xOf(p);
    return x != null && x <= xMax;
  }));
  // Recall usually spans only 80-100 %: zoom the y axis to the shown points (in 5 % steps)
  const yMin = $derived(Math.max(0, Math.floor((Math.min(1, ...shown.map((p) => p.recall)) - 0.01) * 20) / 20));
  const px = (p: CurvePoint) => ((xOf(p) ?? 0) / xMax) * W;
  const py = (p: CurvePoint) => H - ((p.recall - yMin) / (1 - yMin || 1)) * H;
  const line = $derived([...shown].sort((a, b) => (xOf(a) ?? 0) - (xOf(b) ?? 0) || a.recall - b.recall)
    .map((p, i) => `${i ? "L" : "M"}${px(p).toFixed(1)},${py(p).toFixed(1)}`).join(" "));
  const ticks = $derived([0, 0.25, 0.5, 0.75, 1].map((f) => +(f * xMax).toFixed(2)));
</script>

{#if status}
<div class="page">
  {#if !status.runs.length}
    <section class="panel empty">
      <p class="dim">{t("No trained model yet. Train one first; every finished run can be exported here.")}</p>
      <a class="btn primary" href={href.project(project.slug, "train")}><Icon name="pulse" size={15} /> {t("Go to training")}</a>
    </section>
  {:else}
    <section class="panel models">
      <div class="head">
        <span class="eyebrow">{t("Models")}</span>
        <span class="faint small">{t("measured at each run's calibrated operating point")}</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>{t("Run")}</th><th class="num">{t("Recall")}</th><th class="num">{t("FA/h ambient")}</th>
            <th class="num">{t("FA/h speech")}</th><th class="num">{t("Cutoff")}</th><th></th>
          </tr>
        </thead>
        <tbody>
          {#each status.runs as r (r.id)}
            {@const c = r.calibration}
            <tr class="pick" class:sel={r.id === runId} onclick={() => selectRun(r)}>
              <td><span class="radio" class:on={r.id === runId}></span>{when(r.started)} <span class="chip">{r.profile} · {(r.steps / 1000).toFixed(0)}k</span></td>
              <td class="num mono">{pct(c.recall)}</td>
              <td class="num mono" class:warn={!c.met_target}>{rate(c.false_accepts_per_hour)}</td>
              <td class="num mono">{r.speech ? rate(speechFaph(r, c.probability_cutoff)) : "–"}</td>
              <td class="num mono">{c.probability_cutoff} · w{c.sliding_window_size}</td>
              <td>
                {#if current?.run === r.id}<span class="chip pos"><span class="dot"></span>{t("exported")}</span>{/if}
                {#if r.id === status.runs[0].id}<span class="chip">{t("newest")}</span>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if changes.length}
        <p class="hint warn">{t("The newest model does not know the latest training data yet. You can export it anyway, or train again first.")}</p>
      {/if}
    </section>

    {#if run && point}
      <div class="top">
        <section class="panel point">
          <span class="eyebrow">{t("Operating point")}</span>
          <div class="slider">
            <input type="range" min="0" max={points.length - 1} step="1" bind:value={idx} aria-label={t("Probability cutoff")} />
            <div class="ends faint small"><span>{t("detects more")}</span><span>{t("fewer false alarms")}</span></div>
          </div>
          <div class="metrics">
            <div><span class="label">{t("Cutoff")}</span><strong class="mono">{point.cutoff}</strong><span class="faint small">{t("window {n}", { n: run.calibration.sliding_window_size })}</span></div>
            <div><span class="label">{t("Recall")}</span><strong class="mono pos">{pct(point.recall)}</strong><span class="faint small">{t("held-out wake word clips")}</span></div>
            <div><span class="label">{t("FA/h ambient")}</span><strong class="mono neg">{rate(point.faph)}</strong><span class="faint small">{t("{hours} h dinner party audio", { hours: run.calibration.ambient_hours })}</span></div>
            <div>
              <span class="label">{t("FA/h speech")}</span>
              {#if run.speech && speechFaph(run, point.cutoff) != null}
                <strong class="mono neg">{rate(speechFaph(run, point.cutoff))}</strong><span class="faint small">{t("{hours} h real speech (mining)", { hours: Math.round(run.speech.hours * 10) / 10 })}</span>
              {:else if run.speech}
                <strong class="mono faint">–</strong><span class="faint small">{t("scanned from cutoff {c} up only", { c: run.speech.min_cutoff ?? "?" })}</span>
              {:else}
                <strong class="mono faint">–</strong><a class="small" href={href.project(project.slug, "mining")}>{t("scan this model")}</a>
              {/if}
            </div>
          </div>
          <p class="hint faint">
            {#if isCalibrated}
              {t("This is the cutoff the calibration picked for {target} false accepts per hour.", { target: run.calibration.target_false_accepts_per_hour })}
            {:else}
              {t("Raise the cutoff if the device wakes up by itself, lower it if it misses you.")}
              <button class="ghost small" onclick={() => selectRun(run)}>{t("Back to calibrated ({c})", { c: run.calibration.probability_cutoff })}</button>
            {/if}
          </p>
          <div class="actions">
            <button class="primary" onclick={exportModel} disabled={busy}>
              <Icon name="download" size={16} /> {t("Download model (.zip)")}
            </button>
            <span class="faint small">{t("manifest with cutoff {c} + .tflite", { c: point.cutoff })}</span>
          </div>
        </section>

        <section class="panel chart">
          <div class="head">
            <span class="eyebrow">{t("Recall vs. false accepts per hour")}</span>
            <div class="toggle">
              <button class="small" class:on={axis === "ambient"} onclick={() => (axis = "ambient")}>{t("ambient")}</button>
              <button class="small" class:on={axis === "speech"} onclick={() => (axis = "speech")} disabled={!run.speech}
                title={run.speech ? "" : t("Scan this model in the mining step first")}>{t("speech")}</button>
            </div>
          </div>
          <svg class="curve" viewBox="-36 -10 410 200">
            <line x1="0" y1={H} x2={W} y2={H} class="axis" />
            <line x1="0" y1="0" x2="0" y2={H} class="axis" />
            {#each ticks as v (v)}<text x={(v / xMax) * W} y={H + 16} class="tick">{v}</text>{/each}
            {#each [0, 0.5, 1] as v (v)}<text x="-6" y={H - v * H + 4} class="tick end">{Math.round((yMin + v * (1 - yMin)) * 100)}%</text>{/each}
            {#if axis === "ambient" && run.calibration.target_false_accepts_per_hour <= xMax}
              <line x1={(run.calibration.target_false_accepts_per_hour / xMax) * W} y1="0" x2={(run.calibration.target_false_accepts_per_hour / xMax) * W} y2={H} class="target" />
            {/if}
            <path d={line} class="l-cyan" />
            {#each shown as p (p.cutoff)}
              <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (the slider is the keyboard way) -->
              <circle cx={px(p)} cy={py(p)} r="7" class="hit" onclick={() => pick(p)}><title>{t("cutoff {c}", { c: p.cutoff })}</title></circle>
              {#if p.cutoff === run.calibration.probability_cutoff}<circle cx={px(p)} cy={py(p)} r="7" class="cal" />{/if}
            {/each}
            {#if shown.includes(point)}<circle cx={px(point)} cy={py(point)} r="5" class="sel" />{/if}
          </svg>
          <p class="faint small">
            {axis === "ambient"
              ? t("Ambient: CHiME-6 dev and DiPCo dinner party recordings. Dashed line: the training's target. Ringed: the calibrated point.")
              : t("Speech: the mining scan of this model, every trigger that was not the wake word. Only cutoffs from {c} up were scanned.", { c: run.speech?.min_cutoff ?? "?" })}
          </p>
        </section>
      </div>
    {/if}

    <section class="panel esphome">
      <div class="head">
        <span class="eyebrow">{t("Into ESPHome")}</span>
        <div class="toggle">
          <button class="small" class:on={target === "ha"} onclick={() => (target = "ha")}>{t("Home Assistant folder")}</button>
          <button class="small" class:on={target === "git"} onclick={() => (target = "git")}>{t("Git repository")}</button>
          <button class="small" class:on={target === "pc"} onclick={() => (target = "pc")}>{t("From this PC")}</button>
        </div>
      </div>
      {#if target === "ha"}
        <ol class="howto">
          <li>{@html t("Unpack the .zip into a folder <code>wakeforge</code> next to the device's YAML (in Home Assistant: <code>/config/esphome/wakeforge/</code>).")}</li>
          <li>{t("Reference the manifest in the device's YAML, then install:")}</li>
        </ol>
        <div class="code">
          <pre class="mono">{yamlFiles}</pre>
          <button class="small" onclick={() => copy(yamlFiles)}>{t("Copy")}</button>
        </div>
        <p class="hint faint">{t("For a new model, replace the two files and install again.")}</p>
      {:else if target === "git"}
        <div class="row3">
          <label class="field"><span>{t("Repository (owner/name)")}</span><input class="mono" bind:value={gitRepo} placeholder="owner/repo" spellcheck="false" /></label>
          <label class="field"><span>{t("Branch")}</span><input class="mono" bind:value={gitBranch} spellcheck="false" /></label>
          <label class="field"><span>{t("Folder")}</span><input class="mono" bind:value={gitPath} spellcheck="false" /></label>
        </div>
        <ol class="howto">
          <li>{t("Put both files from the .zip into that folder of the repository, commit and push. The repository must be public.")}</li>
          <li>{t("Reference the manifest in the device's YAML, then install:")}</li>
        </ol>
        <div class="code">
          <pre class="mono">{yamlGit}</pre>
          <button class="small" onclick={() => copy(yamlGit)}>{t("Copy")}</button>
        </div>
        <p class="hint faint">{t("ESPHome checks this URL for changes on every compile: for a new model, push the two files and install again. Others can use your model with the same lines.")}</p>
      {:else}
        <label class="field">
          <span>{t("Address of this PC as ESPHome sees it")}</span>
          <input class="mono" bind:value={host} onchange={saveHost} spellcheck="false" />
        </label>
        {#if localHost}
          <p class="hint warn">{t("ESPHome runs on another machine: put this PC's LAN address here (e.g. http://192.168.1.20:8790).")}</p>
        {/if}
        <div class="code">
          <pre class="mono">{yamlUrl}</pre>
          <button class="small" onclick={() => copy(yamlUrl)}>{t("Copy")}</button>
        </div>
        <p class="hint faint">
          {@html t("The URL always serves the newest export, and ESPHome checks it for changes on every compile: export, then install again. The container only listens on 127.0.0.1 by default; set <code>WAKEFORGE_BIND=0.0.0.0</code> in <code>.env</code> and run <code>docker compose up -d</code> to open the port to your network (no login, trusted networks only).")}
        </p>
      {/if}
      <p class="hint faint">{@html t("To try another cutoff without a new download, add <code>probability_cutoff: 0.95</code> under the model entry; it overrides the manifest.")}</p>
      <div class="credits">
        <span class="label">{t("Manifest credits")} <span class="faint">{t("(optional, travel with the model, e.g. when you publish it)")}</span></span>
        <div class="row3">
          <label class="field"><span>{t("Author")}</span><input bind:value={author} maxlength="60" placeholder={t("optional")} /></label>
          <label class="field"><span>{t("Website")}</span><input bind:value={website} maxlength="200" placeholder={t("https://… (optional)")} /></label>
          <button class="small" onclick={saveCredits} disabled={!creditsDirty || busy}>{t("Save")}</button>
        </div>
      </div>
    </section>

    <section class="panel exports">
      <span class="eyebrow">{t("Exports")}</span>
      {#if !status.exports.length}
        <p class="dim">{t("Exported models appear here. The newest one is the current model.")}</p>
      {/if}
      {#each status.exports as e (e.id)}
        <div class="exprow">
          <span class="when">{when(e.created)}</span>
          {#if e.id === status.current}<span class="chip pos"><span class="dot"></span>{t("current")}</span>{/if}
          <span class="metric"><span class="faint">{t("cutoff")}</span> <span class="mono">{e.probability_cutoff} · w{e.sliding_window_size}</span></span>
          <span class="metric"><span class="faint">{t("recall")}</span> <strong class="mono">{pct(e.recall)}</strong></span>
          <span class="metric"><span class="faint">{t("FA/h ambient")}</span> <strong class="mono">{rate(e.false_accepts_per_hour)}</strong></span>
          {#if e.speech_false_accepts_per_hour != null}
            <span class="metric"><span class="faint">{t("FA/h speech")}</span> <strong class="mono">{rate(e.speech_false_accepts_per_hour)}</strong></span>
          {/if}
          <span class="faint small mono">{t("run {id}", { id: e.run })}</span>
          <span class="spacer"></span>
          <a class="btn small" href={models.fileUrl(e.id, e.files.manifest)} download>.json</a>
          <a class="btn small" href={models.fileUrl(e.id, e.files.model)} download>.tflite</a>
          <a class="btn small" href={models.zipUrl(e.id)} download>.zip</a>
          <button class="danger small" onclick={() => remove(e)} disabled={busy} aria-label={t("Delete")}><Icon name="trash" size={14} /></button>
        </div>
      {/each}
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
    grid-template-columns: minmax(320px, 460px) 1fr;
    gap: 22px;
    align-items: start;
  }
  .panel {
    padding: 20px 22px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .empty {
    align-items: flex-start;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .small {
    font-size: 12.5px;
    margin: 0;
  }
  .hint {
    margin: 0;
    font-size: 12.5px;
    line-height: 1.5;
  }
  .warn,
  .hint.warn {
    color: var(--amber);
  }
  .pos {
    color: var(--cyan);
  }
  .neg {
    color: var(--magenta);
  }
  a {
    color: var(--cyan);
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
    padding: 4px 8px;
    border-bottom: 1px solid var(--line);
  }
  td {
    padding: 8px;
    border-bottom: 1px solid rgba(150, 130, 255, 0.07);
  }
  td .chip {
    margin-left: 6px;
  }
  .num {
    text-align: right;
  }
  tr.pick {
    cursor: pointer;
  }
  tr.pick:hover td {
    background: rgba(155, 107, 255, 0.06);
  }
  tr.sel td {
    background: rgba(41, 240, 255, 0.06);
  }
  .radio {
    display: inline-block;
    width: 12px;
    height: 12px;
    margin-right: 10px;
    border-radius: 50%;
    border: 1px solid var(--line-strong);
    vertical-align: -1px;
  }
  .radio.on {
    border-color: var(--cyan);
    background: var(--cyan);
    box-shadow: var(--glow-cyan);
  }
  .slider {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .slider input {
    width: 100%;
    padding: 0;
    accent-color: var(--cyan);
  }
  .ends {
    display: flex;
    justify-content: space-between;
  }
  .metrics {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  .metrics div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .metrics strong {
    font-size: 22px;
    font-weight: 600;
  }
  .label {
    font-size: 12px;
    color: var(--text-dim);
  }
  .hint .ghost {
    margin-left: 4px;
    color: var(--cyan);
  }
  .actions button,
  .btn,
  .exprow button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .toggle {
    display: inline-flex;
    gap: 4px;
  }
  .toggle button.on {
    border-color: var(--cyan);
    color: var(--cyan);
    background: rgba(41, 240, 255, 0.1);
  }
  .curve {
    width: 100%;
    max-width: 560px;
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
  .hit {
    fill: transparent;
    cursor: pointer;
  }
  .hit:hover {
    fill: rgba(41, 240, 255, 0.25);
  }
  .cal {
    fill: none;
    stroke: var(--amber);
    stroke-width: 1.5;
    pointer-events: none;
  }
  .sel {
    fill: var(--magenta);
    filter: drop-shadow(0 0 6px rgba(255, 61, 242, 0.8));
    pointer-events: none;
  }
  .howto {
    margin: 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    font-size: 13.5px;
    line-height: 1.5;
  }
  :global(.esphome code) {
    font-family: var(--font-mono);
    font-size: 12px;
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
  }
  .code {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .code pre {
    flex: 1;
    margin: 0;
    padding: 12px 14px;
    border-radius: var(--radius-sm);
    background: rgba(0, 0, 0, 0.35);
    font-size: 12.5px;
    overflow-x: auto;
    user-select: all;
  }
  .field input {
    max-width: 420px;
  }
  .actions {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .row3 {
    display: flex;
    align-items: flex-end;
    gap: 12px;
    flex-wrap: wrap;
  }
  .row3 .field {
    flex: 1 1 160px;
  }
  .row3 .field input {
    max-width: none;
    width: 100%;
  }
  .credits {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
  }
  .exprow {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 10px 14px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.02);
  }
  .when {
    font-weight: 600;
  }
  .metric {
    font-size: 13px;
  }
  .spacer {
    flex: 1;
  }
  .btn {
    text-decoration: none;
    color: var(--text);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    padding: 5px 10px;
    font-size: 13px;
    background: rgba(255, 255, 255, 0.04);
  }
  .btn.primary {
    border-color: var(--cyan);
    background: linear-gradient(135deg, rgba(41, 240, 255, 0.18), rgba(155, 107, 255, 0.18));
  }
  @media (max-width: 1050px) {
    .top {
      grid-template-columns: 1fr;
    }
  }
</style>
