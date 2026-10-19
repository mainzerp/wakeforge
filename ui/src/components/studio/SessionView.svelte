<script lang="ts">
  import { untrack } from "svelte";
  import ClipCard from "./ClipCard.svelte";
  import Icon from "../Icon.svelte";
  import Waveform, { type Region } from "../Waveform.svelte";
  import { studio } from "../../lib/api";
  import { plural, t } from "../../lib/i18n.svelte";
  import { player } from "../../lib/player.svelte";
  import { clipBounds, formatSession, labelCounts } from "../../lib/session";
  import { setCounts, toast, toastError } from "../../lib/state.svelte";
  import type { Clip, Label, Session, SplitParams } from "../../lib/types";

  let {
    session,
    params,
    onupdate,
    onreload,
  }: {
    session: Session;
    params: SplitParams;
    onupdate: (s: Session) => void;
    onreload: (select?: string) => Promise<void>;
  } = $props();

  const REC_KEY = "__recording__";
  const REGION_COLORS = { positive: "#29f0ff", negative: "#ff3df2", none: "#9b6bff" };

  let focus = $state<string | null>(null);
  let autoplay = $state(false);
  let prefix = $state(untrack(() => session.prefix));
  let busy = $state(false);

  const n = $derived(labelCounts(session));
  const regions = $derived<Region[]>(
    session.clips.map((c) => {
      const [start, end] = clipBounds(c, session.params?.pad);
      return { id: c.name, start, end, color: REGION_COLORS[c.label ?? "none"], active: c.name === focus };
    }),
  );

  $effect(() => () => player.stop());

  const url = (c: Clip) => studio.clipUrl(session.name, c.name);
  const find = (name: string | null) => session.clips.find((c) => c.name === name) ?? null;

  function nextUnsorted(after: string | null): Clip | null {
    const i = after ? session.clips.findIndex((c) => c.name === after) : -1;
    return session.clips.slice(i + 1).find((c) => !c.label) ?? null;
  }

  function play(c: Clip | null) {
    if (!c) {
      autoplay = false;
      player.stop();
      const left = labelCounts(session).unsorted;
      toast(left ? plural(left, "End of list, {n} clip still unsorted.", "End of list, {n} clips still unsorted.") : t("All clips sorted."));
      return;
    }
    focus = c.name;
    player.play(c.name, url(c), {
      onEnded: () => {
        if (autoplay && focus === c.name) setTimeout(() => autoplay && focus === c.name && play(nextUnsorted(c.name)), 350);
      },
    });
  }

  function toggleAutoplay() {
    if (autoplay) {
      autoplay = false;
      player.stop();
      return;
    }
    autoplay = true;
    const cur = find(focus);
    play(cur && !cur.label ? cur : nextUnsorted(null));
  }

  function advance(from: string, next: Clip | null) {
    if (focus !== from) return;
    if (autoplay) play(next);
    else {
      player.stop();
      focus = next?.name ?? null;
    }
  }

  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    busy = true;
    try {
      return await fn();
    } catch (e) {
      toastError(e);
    } finally {
      busy = false;
    }
  }

  function apply(res: { session: Session; counts: { positive: number; negative: number } }) {
    setCounts(res.counts);
    onupdate(res.session);
  }

  async function setLabel(c: Clip, label: Label) {
    const value = c.label === label ? null : label;
    const next = nextUnsorted(c.name);
    const res = await run(() => studio.label(session.name, [c.name], value));
    if (!res) return;
    apply(res);
    if (value) advance(c.name, next);
  }

  async function remove(c: Clip) {
    const next = nextUnsorted(c.name);
    const res = await run(() => studio.deleteClip(session.name, c.name));
    if (!res) return;
    apply(res);
    advance(c.name, next);
  }

  async function markUnsorted(label: Label) {
    const question =
      label === "positive"
        ? plural(n.unsorted, "Mark {n} unsorted clip as positive?", "Mark {n} unsorted clips as positive?")
        : plural(n.unsorted, "Mark {n} unsorted clip as negative?", "Mark {n} unsorted clips as negative?");
    if (!confirm(question)) return;
    const res = await run(() => studio.labelUnsorted(session.name, label));
    if (res) {
      apply(res);
      toast(
        label === "positive"
          ? plural(n.unsorted, "Marked {n} clip as positive.", "Marked {n} clips as positive.")
          : plural(n.unsorted, "Marked {n} clip as negative.", "Marked {n} clips as negative."),
      );
    }
  }

  async function rename() {
    const res = await run(() => studio.rename(session.name, prefix));
    if (res) {
      apply(res);
      prefix = res.session.prefix;
      toast(res.session.prefix ? t("Clips renamed to \"{prefix}\".", { prefix: res.session.prefix }) : t("Prefix removed."));
    }
  }

  async function resplit() {
    const sorted = n.positive + n.negative;
    if (
      sorted &&
      !confirm(
        plural(
          sorted,
          "Re-splitting resets sorting and removes this take's {n} sorted clip from the sample sets. Continue?",
          "Re-splitting resets sorting and removes this take's {n} sorted clips from the sample sets. Continue?",
        ),
      )
    )
      return;
    player.stop();
    const res = await run(() => studio.split(session.name, $state.snapshot(params)));
    if (res) {
      apply(res);
      focus = null;
      toast(plural(res.session.clips.length, "Split into {n} clip.", "Split into {n} clips."));
    }
  }

  async function removeSession() {
    if (
      !confirm(
        t("Delete take {take} including the full recording? Clips already sorted into the sample sets are kept.", {
          take: formatSession(session.name),
        }),
      )
    )
      return;
    player.stop();
    const res = await run(() => studio.deleteSession(session.name));
    if (res) {
      setCounts(res.counts);
      await onreload();
    }
  }

  function playRecording(t: number) {
    focus = null;
    autoplay = false;
    player.play(REC_KEY, studio.recordingUrl(session.name), { range: [t, session.duration] });
  }

  function step(dir: 1 | -1) {
    const i = session.clips.findIndex((c) => c.name === focus);
    const c = session.clips[Math.max(0, Math.min(session.clips.length - 1, i < 0 ? 0 : i + dir))];
    if (c) play(c);
  }

  let keyBusy = false;
  async function onkey(e: KeyboardEvent) {
    if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test((e.target as HTMLElement).tagName)) return;
    const c = find(focus);
    const actions: Record<string, () => unknown> = {
      arrowright: () => step(1),
      j: () => step(1),
      arrowleft: () => step(-1),
      k: () => step(-1),
      escape: () => ((autoplay = false), player.stop()),
      ...(c && {
        p: () => setLabel(c, "positive"),
        n: () => setLabel(c, "negative"),
        d: () => remove(c),
        delete: () => remove(c),
        r: () => play(c),
        " ": () => play(c),
      }),
    };
    const action = actions[e.key.toLowerCase()];
    if (!action) return;
    e.preventDefault();
    if (keyBusy) return;
    keyBusy = true;
    try {
      await action();
    } finally {
      keyBusy = false;
    }
  }
</script>

<svelte:window onkeydown={onkey} />

<div class="view">
  <div class="panel top">
    <div class="title">
      <div>
        <span class="eyebrow">{t("Take")}</span>
        <h2 class="mono">{formatSession(session.name)}</h2>
        {#if session.source}<span class="source faint">{t("Uploaded file: {file}", { file: session.source })}</span>{/if}
      </div>
      <div class="chips">
        <span class="chip mono">{session.duration}s</span>
        <span class="chip mono">{plural(session.clips.length, "{n} clip", "{n} clips")}</span>
        {#if session.rejected}<span class="chip warn mono" title={t("Regions shorter or longer than the limits")}>{t("{n} skipped", { n: session.rejected })}</span>{/if}
      </div>
    </div>

    <div class="overview">
      <Waveform
        url={studio.recordingUrl(session.name)}
        height={96}
        buckets={1400}
        dim
        {regions}
        progress={player.key === REC_KEY ? player.time / session.duration : null}
        onregion={(id) => play(find(id))}
        onseek={playRecording}
      />
    </div>

    <div class="controls">
      <div class="group">
        <input bind:value={prefix} maxlength="40" placeholder={t("prefix")} aria-label={t("Clip prefix")} />
        <button onclick={rename} disabled={busy || prefix === session.prefix}><Icon name="tag" size={15} /> {t("Rename")}</button>
        <button onclick={resplit} disabled={busy} title={t("Uses the split settings on the left. Resets sorting.")}><Icon name="split" size={15} /> {t("Re-split")}</button>
      </div>
      <button class="danger" onclick={removeSession} disabled={busy}><Icon name="trash" size={15} /> {t("Delete take")}</button>
    </div>
  </div>

  <div class="panel sorter">
    <div class="bar">
      <button class="primary play" class:on={autoplay} onclick={toggleAutoplay} disabled={!session.clips.length}>
        <Icon name={autoplay ? "stop" : "play"} size={16} />
        {autoplay ? t("Stop") : t("Play unsorted")}
      </button>
      <div class="tally mono">
        <span class="chip warn"><span class="dot"></span>{t("{n} open", { n: n.unsorted })}</span>
        <span class="chip pos"><span class="dot"></span>{n.positive}</span>
        <span class="chip neg"><span class="dot"></span>{n.negative}</span>
      </div>
      <div class="bulk">
        <button class="pos small" disabled={busy || !n.unsorted} onclick={() => markUnsorted("positive")}>{t("All open → Pos")}</button>
        <button class="neg small" disabled={busy || !n.unsorted} onclick={() => markUnsorted("negative")}>{t("All open → Neg")}</button>
      </div>
    </div>
    <p class="keys faint">
      <kbd>P</kbd> {t("positive")} <kbd>N</kbd> {t("negative")} <kbd>D</kbd> {t("delete")} <kbd>R</kbd>/<kbd>Space</kbd> {t("replay")}
      <kbd>←</kbd><kbd>→</kbd> {t("move")} <kbd>Esc</kbd> {t("stop")} · {t("click a label again to undo")}
    </p>

    <div class="grid">
      {#each session.clips as c (c.name)}
        <ClipCard
          clip={c}
          url={url(c)}
          focused={focus === c.name}
          playing={player.key === c.name && player.playing}
          progress={player.key === c.name ? player.progress : null}
          onplay={() => play(c)}
          onlabel={(l) => setLabel(c, l)}
          ondelete={() => remove(c)}
        />
      {/each}
    </div>
  </div>
</div>

<style>
  .source {
    display: block;
    font-size: 12.5px;
    margin-top: 2px;
  }
  .view {
    display: flex;
    flex-direction: column;
    gap: 22px;
  }
  .top {
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .title {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    flex-wrap: wrap;
  }
  h2 {
    font-family: var(--font-mono);
    font-size: 22px;
    margin-top: 4px;
  }
  .chips,
  .tally,
  .bulk,
  .group {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .overview {
    padding: 10px 12px;
    border-radius: 14px;
    background: rgba(5, 4, 16, 0.55);
    border: 1px solid var(--line);
  }
  .controls {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .controls input {
    width: 160px;
  }
  .controls button,
  .play {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .sorter {
    padding: 18px 20px;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .bulk {
    margin-left: auto;
  }
  .play.on {
    border-color: var(--magenta);
    box-shadow: var(--glow-magenta);
    background: rgba(255, 61, 242, 0.15);
  }
  .keys {
    font-size: 12.5px;
    margin: 12px 0 16px;
    line-height: 2;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 12px;
  }
</style>
