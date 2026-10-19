<script lang="ts">
  import RecorderPanel from "../components/studio/RecorderPanel.svelte";
  import SessionList from "../components/studio/SessionList.svelte";
  import SessionView from "../components/studio/SessionView.svelte";
  import { studio } from "../lib/api";
  import { t } from "../lib/i18n.svelte";
  import { setCounts, toastError } from "../lib/state.svelte";
  import { DEFAULT_PARAMS, type Session, type SplitParams } from "../lib/types";

  let sessions = $state<Session[]>([]);
  let selected = $state<string | null>(null);
  let params = $state<SplitParams>({ ...DEFAULT_PARAMS });
  let loaded = $state(false);
  const current = $derived(sessions.find((s) => s.name === selected) ?? null);

  async function refresh(select?: string) {
    try {
      const data = await studio.sessions();
      sessions = data.sessions;
      setCounts(data.counts);
      const name = select ?? (sessions.some((s) => s.name === selected) ? selected : sessions[0]?.name ?? null);
      choose(name);
    } catch (e) {
      toastError(e);
    } finally {
      loaded = true;
    }
  }

  function choose(name: string | null) {
    selected = name;
    const s = sessions.find((x) => x.name === name);
    if (s?.params) params = { ...DEFAULT_PARAMS, ...s.params };
  }

  function update(session: Session) {
    const i = sessions.findIndex((s) => s.name === session.name);
    if (i >= 0) sessions[i] = session;
    else sessions = [session, ...sessions];
    selected = session.name;
  }

  refresh();
</script>

<div class="studio">
  <aside>
    <RecorderPanel bind:params onuploaded={(s) => update(s)} />
    <SessionList {sessions} {selected} onselect={choose} />
  </aside>
  <section>
    {#if current}
      {#key current.name}
        <SessionView session={current} {params} onupdate={update} onreload={refresh} />
      {/key}
    {:else if loaded}
      <div class="panel empty">
        <h2>{t("No takes yet")}</h2>
        <p class="dim">{t("Hit record, say the wake word a few dozen times with short pauses, then stop. Clips appear here, ready to sort.")}</p>
        <p class="dim">{t("You can also upload audio files, e.g. voice memos of other people saying the wake word.")}</p>
      </div>
    {/if}
  </section>
</div>

<style>
  .studio {
    display: grid;
    grid-template-columns: minmax(300px, 360px) 1fr;
    gap: 22px;
    align-items: start;
  }
  aside {
    display: flex;
    flex-direction: column;
    gap: 22px;
    position: sticky;
    top: 20px;
  }
  section {
    min-width: 0;
  }
  .empty {
    padding: 48px;
    text-align: center;
  }
  .empty p {
    max-width: 440px;
    margin: 12px auto 0;
    line-height: 1.5;
  }
  @media (max-width: 1050px) {
    .studio {
      grid-template-columns: 1fr;
    }
    aside {
      position: static;
    }
  }
</style>
