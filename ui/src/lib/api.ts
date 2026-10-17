import { slug } from "./state.svelte";
import type {
  BorrowCandidate,
  BorrowEntry,
  ConfusableParams,
  Counts,
  GenClip,
  GenerateParams,
  GenerateStatus,
  Guide,
  Job,
  Label,
  MiningHit,
  MiningLabel,
  MiningRun,
  MiningStatus,
  ModelExport,
  ModelsStatus,
  Project,
  ProjectSettings,
  Session,
  SetupStatus,
  SetupUse,
  SoundAlike,
  SplitParams,
  TrainRun,
  TrainStatus,
} from "./types";

type SessionResult = { session: Session; counts: Counts };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.detail || res.statusText);
  return data as T;
}

const send = <T>(method: string, path: string, body: unknown) =>
  request<T>(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const post = <T>(path: string, body: unknown) => send<T>("POST", path, body);

export const projects = {
  list: () => request<{ projects: Project[] }>("/api/projects"),
  get: (slug: string) => request<Project>(`/api/projects/${encodeURIComponent(slug)}`),
  create: (settings: Partial<ProjectSettings>) => post<Project>("/api/projects", settings),
  update: (slug: string, settings: Partial<ProjectSettings> | { borrow_negatives: BorrowEntry[] }) =>
    send<Project>("PATCH", `/api/projects/${encodeURIComponent(slug)}`, settings),
  guide: (slug: string) => request<Guide>(`/api/projects/${encodeURIComponent(slug)}/guide`),
  borrowCandidates: (slug: string) => request<{ projects: BorrowCandidate[] }>(`/api/projects/${encodeURIComponent(slug)}/borrow`),
};

const projectBase = () => `/api/projects/${encodeURIComponent(slug())}`;

export const generate = {
  status: () => request<GenerateStatus>(`${projectBase()}/generate`),
  start: (params: GenerateParams) => post<Job>(`${projectBase()}/generate/start`, params),
  reset: () => post<GenerateStatus>(`${projectBase()}/generate/reset`, {}),
  samples: (n = 12, kind: "positive" | "negative" | "confusable" = "positive") =>
    request<{ clips: GenClip[] }>(`${projectBase()}/generate/samples?n=${n}&kind=${kind}`),
  startConfusables: (params: ConfusableParams) => post<Job>(`${projectBase()}/generate/confusables/start`, params),
  resetConfusables: () => post<GenerateStatus>(`${projectBase()}/generate/confusables/reset`, {}),
};

export const train = {
  status: () => request<TrainStatus>(`${projectBase()}/train`),
  start: (profile: string, target_faph: number) => post<Job>(`${projectBase()}/train/start`, { profile, target_faph }),
  run: (id: string) => request<TrainRun>(`${projectBase()}/train/runs/${id}`),
  deleteRun: (id: string) => post<{ ok: boolean }>(`${projectBase()}/train/runs/${id}/delete`, {}),
  fileUrl: (id: string, name: string) => `${projectBase()}/train/runs/${id}/files/${encodeURIComponent(name)}`,
};

export const mining = {
  status: () => request<MiningStatus>(`${projectBase()}/mining`),
  start: (run: string, sources: string[], cutoff: number | null, auto: boolean) =>
    post<Job>(`${projectBase()}/mining/start`, { run, sources, cutoff, auto }),
  check: (id: string) => post<Job>(`${projectBase()}/mining/runs/${id}/check`, {}),
  run: (id: string) => request<{ run: MiningRun; hits: MiningHit[] }>(`${projectBase()}/mining/runs/${id}`),
  label: (id: string, hits: number[], label: MiningLabel | null) =>
    post<{ run: MiningRun; totals: MiningStatus["totals"] }>(`${projectBase()}/mining/runs/${id}/label`, { hits, label }),
  deleteRun: (id: string) => post<{ ok: boolean }>(`${projectBase()}/mining/runs/${id}/delete`, {}),
  clipUrl: (id: string, n: number) => `${projectBase()}/mining/runs/${id}/clips/${n}.wav`,
};

export const models = {
  status: () => request<ModelsStatus>(`${projectBase()}/models`),
  create: (run: string, cutoff: number) => post<ModelExport>(`${projectBase()}/models`, { run, cutoff }),
  remove: (id: string) => post<{ ok: boolean }>(`${projectBase()}/models/${id}/delete`, {}),
  fileUrl: (id: string, name: string) => `${projectBase()}/models/${id}/files/${encodeURIComponent(name)}`,
  zipUrl: (id: string) => `${projectBase()}/models/${id}/zip`,
  /** always the newest export: the URL for ESPHome's YAML */
  currentUrl: (name: string) => `${projectBase()}/models/current/${encodeURIComponent(name)}`,
};

export const setupApi = {
  status: (speechShards?: number) => request<SetupStatus>(`/api/setup${speechShards ? `?speech_shards=${speechShards}` : ""}`),
  quick: () => request<{ ready_for: Record<SetupUse, boolean> }>("/api/setup/quick"),
  start: (items: string[], speech_shards: number) => post<Job>("/api/setup/start", { items, speech_shards }),
  stop: () => post<Job>("/api/setup/stop", {}),
  exportUrl: (slug: string) => `/api/projects/${encodeURIComponent(slug)}/export`,
  importProject: (file: File, slug?: string) =>
    request<Project>(`/api/projects/import${slug ? `?slug=${encodeURIComponent(slug)}` : ""}`, {
      method: "POST",
      headers: { "Content-Type": "application/zip" },
      body: file,
    }),
};

export const tts = {
  /** project: also suggest the closest words of its mined false triggers */
  soundAlikes: (language: string, phrases: string[], words: string[], suggest = 0, project?: string) =>
    post<{ words: SoundAlike[]; suggestions: SoundAlike[] }>("/api/tts/sound-alikes", { language, phrases, words, suggest, project }),
  preview: (language: string, phrases: string[], n = 2) =>
    post<{ engine: string; clips: GenClip[] }>("/api/tts/preview", { language, phrases, n }),
};

export const jobs = {
  active: () => request<{ job: Job | null }>("/api/jobs/active"),
  stop: (id: string) => post<Job>(`${projectBase()}/jobs/${id}/stop`, {}),
};

const base = () => `${projectBase()}/studio`;

export const studio = {
  sessions: () => request<{ sessions: Session[]; counts: Counts }>(`${base()}/sessions`),
  upload: (wav: Blob, params: SplitParams, prefix: string) =>
    request<SessionResult>(`${base()}/upload`, {
      method: "POST",
      headers: { "Content-Type": "audio/wav", "X-Split-Params": JSON.stringify(params), "X-Prefix": encodeURIComponent(prefix) },
      body: wav,
    }),
  split: (session: string, params: SplitParams) => post<SessionResult>(`${base()}/split`, { session, params }),
  rename: (session: string, prefix: string) => post<SessionResult>(`${base()}/rename`, { session, prefix }),
  label: (session: string, clips: string[], label: Label | null) => post<SessionResult>(`${base()}/label`, { session, clips, label }),
  labelUnsorted: (session: string, label: Label) => post<SessionResult>(`${base()}/label_unsorted`, { session, label }),
  deleteClip: (session: string, clip: string) => post<SessionResult>(`${base()}/delete_clip`, { session, clip }),
  deleteSession: (session: string) => post<{ ok: boolean; counts: Counts }>(`${base()}/delete_session`, { session }),
  recordingUrl: (session: string) => `${base()}/files/${session}/recording.wav`,
  clipUrl: (session: string, clip: string) => `${base()}/files/${session}/clips/${encodeURIComponent(clip)}`,
};
