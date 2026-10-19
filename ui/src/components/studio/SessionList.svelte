<script lang="ts">
  import type { Session } from "../../lib/types";
  import { formatSession, labelCounts } from "../../lib/session";
  import { plural, t } from "../../lib/i18n.svelte";

  let { sessions, selected, onselect }: { sessions: Session[]; selected: string | null; onselect: (name: string) => void } = $props();
</script>

<div class="panel list">
  <div class="head">
    <span class="eyebrow">{t("Takes")}</span>
    <span class="mono faint">{sessions.length}</span>
  </div>
  {#if !sessions.length}
    <p class="faint none">{t("Nothing recorded yet.")}</p>
  {/if}
  <ul>
    {#each sessions as s (s.name)}
      {@const n = labelCounts(s)}
      {@const total = Math.max(1, s.clips.length)}
      <li>
        <button class:active={s.name === selected} onclick={() => onselect(s.name)}>
          <div class="row">
            <span class="when mono">{formatSession(s.name)}</span>
            {#if s.prefix}<span class="chip">{s.prefix}</span>{/if}
          </div>
          {#if s.source}<div class="source faint" title={s.source}>{s.source}</div>{/if}
          <div class="bar">
            <span class="p" style:width="{(n.positive / total) * 100}%"></span>
            <span class="n" style:width="{(n.negative / total) * 100}%"></span>
          </div>
          <div class="row meta mono">
            <span>{plural(s.clips.length, "{n} clip · {duration}s", "{n} clips · {duration}s", { duration: s.duration })}</span>
            <span class:todo={n.unsorted > 0}>{n.unsorted ? t("{n} open", { n: n.unsorted }) : t("sorted")}</span>
          </div>
        </button>
      </li>
    {/each}
  </ul>
</div>

<style>
  .list {
    padding: 16px;
  }
  .head {
    display: flex;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .source {
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
  }
  .none {
    margin: 6px 0;
    font-size: 13px;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 38vh;
    overflow-y: auto;
  }
  li button {
    width: 100%;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border-color: var(--line);
    background: rgba(5, 4, 16, 0.35);
  }
  li button.active {
    border-color: var(--violet);
    background: rgba(155, 107, 255, 0.12);
    box-shadow: var(--glow-violet);
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .when {
    font-size: 13px;
  }
  .meta {
    font-size: 11.5px;
    color: var(--text-faint);
  }
  .todo {
    color: var(--amber);
  }
  .bar {
    display: flex;
    height: 4px;
    border-radius: 4px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.06);
  }
  .bar .p {
    background: var(--cyan);
    box-shadow: 0 0 8px var(--cyan);
  }
  .bar .n {
    background: var(--magenta);
    box-shadow: 0 0 8px var(--magenta);
  }
</style>
