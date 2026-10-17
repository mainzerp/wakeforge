<script lang="ts">
  import { untrack } from "svelte";
  import Icon from "../components/Icon.svelte";
  import BorrowNegatives from "../components/project/BorrowNegatives.svelte";
  import NextStep from "../components/project/NextStep.svelte";
  import Stepper from "../components/project/Stepper.svelte";
  import Studio from "./Studio.svelte";
  import Export from "./steps/Export.svelte";
  import Generate from "./steps/Generate.svelte";
  import Mining from "./steps/Mining.svelte";
  import Train from "./steps/Train.svelte";
  import WakeWord from "./steps/WakeWord.svelte";
  import { projects } from "../lib/api";
  import { guide, refreshGuide } from "../lib/guide.svelte";
  import { locale, t } from "../lib/i18n.svelte";
  import { href } from "../lib/router.svelte";
  import { STEPS, isStep, nextStep, stepIndex, type StepId } from "../lib/steps";
  import { app, setProject, toastError } from "../lib/state.svelte";
  import { LANGUAGES } from "../lib/types";

  let { slug, step }: { slug: string; step: StepId | null } = $props();

  let missing = $state(false);
  const project = $derived(app.project?.slug === slug ? app.project : null);
  const current = $derived(step);
  const info = $derived(current ? STEPS[stepIndex(current)] : null);
  const prev = $derived(current ? STEPS[stepIndex(current) - 1] : undefined);
  // The footer follows the guide when it points elsewhere (the workflow loops: mining leads back to training).
  const advice = $derived(guide.slug === slug ? guide.data?.next : null);
  const next = $derived(
    current
      ? advice && advice.step !== current && STEPS.some((s) => s.id === advice.step)
        ? STEPS.find((s) => s.id === advice.step)
        : STEPS[stepIndex(current) + 1]
      : undefined,
  );
  const s = $derived(project?.summary);
  const wakeClips = $derived(s ? s.generated + s.positive : 0);
  const negClips = $derived(s ? s.negative + s.confusable + s.mined + s.borrowed : 0);

  $effect(() => {
    const s = slug;
    untrack(() => {
      missing = false;
      if (app.project?.slug === s) return;
      setProject(null);
      projects
        .get(s)
        .then((p) => slug === s && setProject(p))
        .catch((e) => {
          missing = true;
          toastError(e);
        });
    });
  });

  // Opening a project without a step goes where the guide points (a running job, the loop's next stage, ...).
  $effect(() => {
    if (!project || step) return;
    const p = project;
    untrack(async () => {
      await refreshGuide(p.slug);
      const advised = guide.slug === p.slug ? guide.data?.next.step : undefined;
      if (!step) location.replace(href.project(p.slug, advised && isStep(advised) ? advised : nextStep(p)));
    });
  });

  // Guide: on every step change and every few seconds (jobs finish, labels change in the background).
  $effect(() => {
    const sl = slug;
    void current;
    untrack(() => refreshGuide(sl));
    const id = setInterval(() => refreshGuide(sl), 5000);
    return () => clearInterval(id);
  });
</script>

{#if missing}
  <section class="panel empty">
    <h2>{t("Project not found")}</h2>
    <p class="dim"><a href={href.home}>{t("Back to all wake words")}</a></p>
  </section>
{:else if project && current && info}
  <div class="project">
    <header>
      <div>
        <a class="crumb" href={href.home}><Icon name="grid" size={14} /> {t("All wake words")}</a>
        <h1>{project.wake_word}</h1>
        <div class="meta">
          <span class="chip">{LANGUAGES[project.language] ? t(LANGUAGES[project.language]) : project.language}</span>
          {#each project.tts_phrases.filter((p) => p !== project.wake_word) as p (p)}<span class="chip pos" title={t("TTS spelling")}>{p}</span>{/each}
        </div>
      </div>
      <div class="stats">
        <div class="stat pos" title={t("{synthetic} synthetic + {own} own recordings", { synthetic: project.summary.generated, own: project.summary.positive })}>
          <span class="eyebrow">{t("Wake word clips")}</span><strong class="mono">{wakeClips.toLocaleString(locale())}</strong>
        </div>
        <div class="stat neg" title={t("{alike} sound-alikes + {mined} mined false triggers + {own} own recordings + {borrowed} borrowed; plus the shared speech and noise datasets", { alike: project.summary.confusable, mined: project.summary.mined, own: project.summary.negative, borrowed: project.summary.borrowed })}>
          <span class="eyebrow">{t("Negative clips")}</span><strong class="mono">{negClips.toLocaleString(locale())}</strong>
        </div>
        <div class="stat" title={t("Finished training runs with a model")}><span class="eyebrow">{t("Models")}</span><strong class="mono">{project.summary.runs}</strong></div>
      </div>
    </header>

    <Stepper {project} {current} />

    <NextStep slug={project.slug} {current} />

    <div class="step-head">
      <div>
        <span class="eyebrow">{t("Step {n} of {total}", { n: stepIndex(current) + 1, total: STEPS.length })}{"optional" in info && info.optional ? ` · ${t("optional")}` : ""}</span>
        <h2>{t(info.label)}</h2>
        <p class="dim">{t(info.sub)}</p>
        <p class="how">{t(info.how)}</p>
      </div>
    </div>

    {#key project.slug}
      {#if current === "wakeword"}
        <WakeWord {project} />
      {:else if current === "generate"}
        <Generate {project} />
      {:else if current === "train"}
        <Train {project} />
      {:else if current === "mining"}
        <Mining {project} />
      {:else if current === "record"}
        <Studio />
        <BorrowNegatives {project} />
      {:else if current === "export"}
        <Export {project} />
      {/if}
    {/key}

    <footer>
      {#if prev}
        <a class="navbtn" href={href.project(project.slug, prev.id)}><Icon name="back" size={16} /> {t(prev.label)}</a>
      {:else}
        <span></span>
      {/if}
      {#if next}
        <a class="navbtn primary" href={href.project(project.slug, next.id)}>{t("Next: {step}", { step: t(next.label) })} <Icon name="next" size={16} /></a>
      {/if}
    </footer>
  </div>
{/if}

<style>
  .project {
    display: flex;
    flex-direction: column;
    gap: 22px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 24px;
    flex-wrap: wrap;
  }
  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12.5px;
    color: var(--text-dim);
    text-decoration: none;
  }
  .crumb:hover {
    color: var(--cyan);
  }
  h1 {
    font-size: 32px;
    font-weight: 700;
    margin: 8px 0 10px;
    background: linear-gradient(90deg, #fff 30%, #c9b8ff);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .stats {
    display: flex;
    gap: 12px;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 110px;
    padding: 10px 16px;
    border-radius: 14px;
    border: 1px solid var(--line);
    background: var(--panel);
  }
  .stat strong {
    font-size: 24px;
    font-weight: 600;
  }
  .stat.pos strong {
    color: var(--cyan);
    text-shadow: var(--glow-cyan);
  }
  .stat.neg strong {
    color: var(--magenta);
    text-shadow: var(--glow-magenta);
  }
  .step-head h2 {
    font-size: 22px;
    margin-top: 6px;
  }
  .step-head p {
    margin: 6px 0 0;
  }
  .step-head .how {
    max-width: 900px;
    font-size: 13px;
    line-height: 1.55;
    color: var(--text-faint);
  }
  footer {
    display: flex;
    justify-content: space-between;
    padding-top: 18px;
    border-top: 1px solid var(--line);
  }
  .navbtn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 9px 16px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--line-strong);
    color: var(--text);
    text-decoration: none;
    background: rgba(255, 255, 255, 0.04);
    transition: background 0.15s, border-color 0.15s;
  }
  .navbtn:hover {
    background: rgba(155, 107, 255, 0.12);
    border-color: var(--violet);
  }
  .navbtn.primary {
    background: linear-gradient(135deg, rgba(41, 240, 255, 0.18), rgba(155, 107, 255, 0.18));
    border-color: var(--cyan);
    box-shadow: var(--glow-cyan);
  }
  .empty {
    padding: 48px;
    text-align: center;
  }
  .empty a {
    color: var(--cyan);
  }
</style>
