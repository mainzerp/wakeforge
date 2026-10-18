<script lang="ts">
  import Icon from "../components/Icon.svelte";
  import ProjectFields from "../components/project/ProjectFields.svelte";
  import PronunciationCheck from "../components/project/PronunciationCheck.svelte";
  import SoundAlikes from "../components/project/SoundAlikes.svelte";
  import { projects } from "../lib/api";
  import { t } from "../lib/i18n.svelte";
  import { go, href } from "../lib/router.svelte";
  import { STEPS, nextStep } from "../lib/steps";
  import { toast, toastError } from "../lib/state.svelte";
  import { LANGUAGES, type ProjectSettings } from "../lib/types";

  const PAGES = [
    { id: "name", title: "Name your wake word", text: "This is the word your device listens for. It also becomes the name in ESPHome." },
    { id: "phrases", title: "Teach the pronunciation", text: "Check how the synthetic voices should spell it, and which similar words the model must ignore. You can change both any time." },
    { id: "review", title: "Ready to forge", text: "Here is what happens next. Every step stays reachable from the step bar." },
  ] as const;

  let settings = $state<ProjectSettings>({ wake_word: "", language: "de", tts_phrases: [], confusables: [], author: "", website: "" });
  let page = $state(0);
  let busy = $state(false);
  let lastName = "";

  const current = $derived(PAGES[page]);
  const canNext = $derived(page !== 0 || !!settings.wake_word.trim());

  function next() {
    if (!canNext) return;
    if (page === 0) {
      const word = settings.wake_word.trim();
      if (!settings.tts_phrases.length || (settings.tts_phrases.length === 1 && settings.tts_phrases[0] === lastName)) settings.tts_phrases = [word];
      lastName = word;
    }
    if (page < PAGES.length - 1) page++;
    else create();
  }

  async function create() {
    busy = true;
    try {
      const p = await projects.create($state.snapshot(settings));
      toast(t("Project \"{name}\" created.", { name: p.wake_word }));
      go(href.project(p.slug, nextStep(p)));
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  function onkey(e: KeyboardEvent) {
    if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT" && !(e.target as HTMLElement).closest("form")) {
      e.preventDefault();
      next();
    }
  }
</script>

<svelte:window onkeydown={onkey} />

<div class="wizard">
  <div class="progress" aria-label={t("Step {n} of {total}", { n: page + 1, total: PAGES.length })}>
    {#each PAGES as p, i (p.id)}
      <button class="seg" class:done={i < page} class:on={i === page} disabled={i > page || busy} onclick={() => (page = i)} aria-label={t(p.title)}></button>
    {/each}
  </div>

  <section class="panel card">
    <span class="eyebrow">{t("New wake word")} · {page + 1}/{PAGES.length}</span>
    <h2>{t(current.title)}</h2>
    <p class="dim lead">{t(current.text)}</p>

    <div class="body">
      {#if current.id === "review"}
        <div class="summary">
          <div class="big">{settings.wake_word}</div>
          <div class="chips">
            <span class="chip">{t(LANGUAGES[settings.language] ?? settings.language)}</span>
            {#each settings.tts_phrases as p (p)}<span class="chip pos">{p}</span>{/each}
          </div>
        </div>
        <ol class="plan">
          {#each STEPS.slice(1) as s, i (s.id)}
            <li>
              <span class="num mono">{String(i + 2).padStart(2, "0")}</span>
              <Icon name={s.icon} size={18} />
              <div>
                <strong>{t(s.label)}</strong>
                {#if "optional" in s && s.optional}<span class="tag">{t("optional")}</span>{/if}
                <p class="dim">{t(s.sub)}</p>
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <ProjectFields bind:settings sections={[current.id]} hero={current.id === "name"} />
        {#if current.id === "phrases"}
          <PronunciationCheck language={settings.language} phrases={settings.tts_phrases} />
          <SoundAlikes bind:words={settings.confusables} language={settings.language} phrases={settings.tts_phrases} auto="add" />
        {/if}
      {/if}
    </div>

    <footer>
      {#if page === 0}
        <a class="ghost-link" href={href.home}>{t("Cancel")}</a>
      {:else}
        <button class="ghost" onclick={() => page--} disabled={busy}><Icon name="back" size={16} /> {t("Back")}</button>
      {/if}
      <button class="primary" onclick={next} disabled={!canNext || busy}>
        {page === PAGES.length - 1 ? t("Create project") : t("Continue")}
        <Icon name={page === PAGES.length - 1 ? "check" : "next"} size={16} />
      </button>
    </footer>
  </section>
</div>

<style>
  .wizard {
    max-width: 760px;
    margin: 24px auto 0;
  }
  .progress {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 18px;
  }
  .seg {
    height: 5px;
    padding: 0;
    border: none;
    border-radius: 4px;
    background: rgba(155, 107, 255, 0.18);
  }
  .seg:disabled {
    opacity: 1;
  }
  .seg.done {
    background: var(--violet);
  }
  .seg.on {
    background: var(--cyan);
    box-shadow: var(--glow-cyan);
  }
  .card {
    padding: 30px 32px 24px;
  }
  h2 {
    font-size: 26px;
    margin-top: 10px;
  }
  .lead {
    margin: 8px 0 26px;
    line-height: 1.5;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 150px;
  }
  footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 30px;
    padding-top: 18px;
    border-top: 1px solid var(--line);
  }
  footer button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .ghost-link {
    color: var(--text-dim);
    text-decoration: none;
    padding: 8px 6px;
  }
  .ghost-link:hover {
    color: var(--text);
  }
  .summary {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 20px;
    border-radius: 14px;
    border: 1px solid var(--line-strong);
    background: rgba(5, 4, 16, 0.5);
  }
  .big {
    font-family: var(--font-display);
    font-size: 34px;
    font-weight: 700;
    background: linear-gradient(90deg, var(--cyan), var(--magenta));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .plan {
    list-style: none;
    padding: 0;
    margin: 8px 0 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .plan li {
    display: grid;
    grid-template-columns: 28px 22px 1fr;
    gap: 12px;
    align-items: start;
    padding: 10px 4px;
    color: var(--text-dim);
  }
  .plan li :global(svg) {
    margin-top: 2px;
    color: var(--violet);
  }
  .num {
    color: var(--text-faint);
    margin-top: 2px;
  }
  .plan strong {
    color: var(--text);
    font-weight: 600;
  }
  .plan p {
    margin: 3px 0 0;
    font-size: 13px;
  }
  .tag {
    margin-left: 8px;
    font-size: 11px;
    color: var(--amber);
    font-family: var(--font-mono);
  }
</style>
