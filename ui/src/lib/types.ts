export type Label = "positive" | "negative";

export interface SplitParams {
  margin_db: number;
  min_len: number;
  max_len: number;
  merge_gap: number;
  pad: number;
  floor_window: number;
}

export interface Clip {
  name: string;
  start: number;
  length: number;
  clip_start?: number;
  clip_end?: number;
  peak_db: number;
  label: Label | null;
}

export interface Session {
  name: string;
  prefix: string;
  duration: number;
  rejected: number;
  params: SplitParams;
  clips: Clip[];
}

export interface Counts {
  positive: number;
  negative: number;
}

export interface ProjectSettings {
  wake_word: string;
  language: string;
  tts_phrases: string[];
  confusables: string[];
  author: string;
  website: string;
}

export type SoundAlikeLevel = "conflict" | "ok" | "far";

export interface SoundAlike {
  text: string;
  distance: number;
  level: SoundAlikeLevel;
  kind?: "mined" | "word" | "onset" | "inner" | "vowel" | "ending";
}

export interface ProjectSummary extends Counts {
  sessions: number;
  generated: number;
  runs: number;
  models: number;
  borrowed: number;
  confusable: number;
  mined: number;
}

export interface BorrowEntry {
  slug: string;
  recordings: boolean;
  synthetic: boolean;
}

export interface BorrowCandidate {
  slug: string;
  wake_word: string;
  language: string;
  recordings: number;
  synthetic: number;
  distance: number;
  level: "ok" | "close" | "conflict";
  selected: Omit<BorrowEntry, "slug">;
}

export interface Project extends ProjectSettings {
  slug: string;
  created: string;
  updated: string;
  borrow_negatives: BorrowEntry[];
  summary: ProjectSummary;
  /** the guide's recommendation (project list only) */
  next?: Guide["next"] | null;
}

export interface Range {
  lo: number;
  hi: number;
}

export interface GenerateParams {
  count: number;
  speed: Range;
  steps: number;
  design_share: number;
  filter: boolean;
}

export type JobState = "starting" | "running" | "done" | "stopped" | "failed";

export interface Job {
  id: string;
  project: string;
  kind: string;
  started: string;
  params: Partial<GenerateParams>;
  state: JobState;
  alive: boolean;
  done?: number;
  start?: number;
  total?: number;
  rejected?: number;
  filtered?: number;
  negatives?: number;
  rate?: number;
  eta?: number | null;
  /** worker phase; generation: "loading" (model) then "generating" */
  phase?: string;
  /** seconds since the job started (total run time once it ended) */
  elapsed?: number | null;
  /** seconds since the worker's last progress write, while it is alive */
  age?: number | null;
  message?: string;
  log?: string;
}

export interface GenerateStatus {
  engine: { name: string; model: string; license: string; voices: number };
  corpus: { voice: string | null; phrases: string[]; total: number; seconds: number; shards: number };
  negatives: { total: number; seconds: number };
  confusables: { phrases: string[]; total: number; seconds: number };
  job: Job | null;
  confusable_job: Job | null;
  active: Job | null;
}

export interface TrainSource {
  id: string;
  label: string;
  truth: boolean;
  sampling_weight: number;
  penalty_weight: number;
  truncation: string;
  clips: number;
  note: string;
  cached: number;
  parts: number;
  prebuilt: boolean;
}

export interface ValPoint {
  step: number;
  recall: number;
  recall_no_faph: number;
  faph: number;
  accuracy: number;
  precision: number;
  auc: number;
  avr: number;
}

export interface CurvePoint {
  cutoff: number;
  recall: number;
  faph: number;
}

export interface Calibration {
  probability_cutoff: number;
  sliding_window_size: number;
  recall: number;
  false_accepts_per_hour: number;
  target_false_accepts_per_hour: number;
  met_target: boolean;
  ambient_hours: number;
  sources: { id: string; label: string; truth: boolean; clips: number; recall?: number; trigger_rate?: number }[];
  curve?: CurvePoint[];
}

export interface TrainRun {
  id: string;
  profile: string;
  steps: number;
  state: string;
  started: string;
  finished?: string;
  seconds?: number;
  error?: string;
  wake_word?: string;
  target_faph?: number;
  calibration?: Calibration;
  files?: { model: string; manifest: string; size: number };
  history?: ValPoint[];
  sources?: TrainSource[];
}

export interface TrainJob extends Job {
  phase?: "featurize" | "train" | "export" | "calibrate" | "done";
  run?: string;
  steps?: number;
  step?: number;
  history?: ValPoint[];
  featurize?: { done: number; total: number; rate: number; eta: number | null };
}

export interface TrainStatus {
  sources: TrainSource[];
  problems: string[];
  profiles: Record<string, { label: string; steps: number; hint: string }>;
  runs: TrainRun[];
  job: TrainJob | null;
  active: Job | null;
}

/** Trigger scores of a run's newest mining scan: false accepts on real speech for any cutoff >= min_cutoff */
export interface SpeechScores {
  id: string;
  hours: number;
  min_cutoff: number | null;
  window: number | null;
  sources: string[];
  scores: number[];
}

export interface ExportRun extends TrainRun {
  calibration: Calibration & { curve: CurvePoint[] };
  files: { model: string; manifest: string; size: number };
  speech: SpeechScores | null;
}

export interface ModelExport {
  id: string;
  run: string;
  profile: string;
  created: string;
  wake_word: string;
  author?: string;
  website?: string;
  probability_cutoff: number;
  sliding_window_size: number;
  calibrated_cutoff: number;
  recall: number;
  false_accepts_per_hour: number;
  ambient_hours: number;
  speech_false_accepts_per_hour: number | null;
  speech_hours: number | null;
  files: { model: string; manifest: string; size: number };
}

export interface ModelsStatus {
  runs: ExportRun[];
  exports: ModelExport[];
  current: string | null;
}

export interface ConfusableParams {
  count: number;
  speed: Range;
  steps: number;
  design_share: number;
}

export interface GenClip {
  url: string;
  voice: string;
  seconds: number;
  speed?: number;
  phrase?: string | null;
  scales?: number[];
  transcript?: string;
  hit?: boolean | null;
  distance?: number | null;
  band?: "positive" | "gray" | "negative";
}

export const LANGUAGES: Record<string, string> = {
  de: "German",
  en: "English",
  fr: "French",
  es: "Spanish",
  it: "Italian",
  nl: "Dutch",
  pl: "Polish",
  pt: "Portuguese",
  sv: "Swedish",
  da: "Danish",
  no: "Norwegian",
  fi: "Finnish",
  cs: "Czech",
  tr: "Turkish",
  ru: "Russian",
  uk: "Ukrainian",
};

export const DEFAULT_PARAMS: SplitParams = {
  margin_db: 12,
  min_len: 0.3,
  max_len: 2,
  merge_gap: 0.2,
  pad: 0.15,
  floor_window: 5,
};

export interface MiningSource {
  id: string;
  label: string;
  hours: number;
  files: number;
  note: string;
}

export interface MiningModel {
  run: string;
  profile: string;
  started: string;
  cutoff: number;
  window: number;
  recall?: number;
  faph?: number;
}

export type MiningLabel = "negative" | "positive" | "skip";

export interface MiningRun {
  id: string;
  run: string;
  model: string;
  /** the model's calibrated cutoff (what the device fires on) */
  cutoff: number;
  window: number;
  /** the cutoff this scan collected triggers at */
  mine_cutoff: number;
  sources: string[];
  hours_total: number;
  hours_done: number;
  overflow: number;
  /** the audio is declared free of the wake word: the check labels clear false triggers */
  auto?: boolean;
  state: JobState | "unknown";
  started: string;
  finished?: string;
  seconds?: number;
  hits: number;
  counts: Record<MiningLabel | "open" | "auto" | "flagged", number>;
  matches: number;
  /** hits the automatic check has seen */
  triaged: number;
  device_hits: number;
  faph: number | null;
}

export interface MiningHit {
  n: number;
  source: string;
  ref: string;
  offset: number;
  score: number;
  peak: number;
  text: string | null;
  match: boolean;
  /** seconds from the clip start to the trigger */
  pre: number;
  seconds: number;
  label: MiningLabel | null;
  /** the label was set by the automatic check */
  auto: boolean;
  check: MiningCheck | null;
}

export interface MiningCheck {
  phones: string;
  /** phone distance of the best matching stretch to the wake word */
  contained: number;
  text_distance?: number;
  text_words?: string;
  /** Whisper's transcript when the source has none */
  asr?: string;
  verdict: "negative" | "review" | "duplicate";
  reason: string;
}

export interface MiningJob extends Job {
  mine?: string;
  done_s?: number;
  total_s?: number;
  hits?: number;
  triage?: { done: number; total: number; negative?: number; review?: number; duplicate?: number };
}

export interface MiningStatus {
  sources: MiningSource[];
  models: MiningModel[];
  runs: MiningRun[];
  totals: { negatives: number; positives: number };
  job: MiningJob | null;
  active: Job | null;
}

export interface GuideChange {
  id: string;
  label: string;
  before: number;
  now: number;
  truth: boolean;
}

export interface Guide {
  /** the one thing to do next */
  next: {
    step: string;
    title: string;
    text: string;
    action: string | null;
    /** English templates with {name} placeholders, translated in the UI */
    tpl: { title: string; text: string };
    params: Record<string, string | number>;
  };
  /** steps that need attention, e.g. training after new data */
  steps: Record<string, { attention: boolean; note: string; tpl: string; params: Record<string, string | number> }>;
  /** training data that changed since the newest model */
  changes: GuideChange[];
  run: string | null;
  /** the current (newest) export */
  export: string | null;
}

export type SetupUse = "training" | "generation" | "mining";

export interface SetupItem {
  id: string;
  label: string;
  purpose: string;
  needed_for: SetupUse[];
  license: string;
  /** bytes to download */
  download: number;
  /** bytes on disk when ready */
  disk: number;
  /** extra bytes while preparing (one archive) */
  scratch: number;
  files: string[];
  state: "ready" | "partial" | "missing";
  have: number;
  want: number;
}

export interface SetupJob extends Job {
  items?: Record<string, { state: string; label: string; bytes: number; total: number; files: number; message?: string }>;
  done_bytes?: number;
  total_bytes?: number;
}

export interface SetupStatus {
  items: SetupItem[];
  speech_shards: { have: number; total: number; hours_each: number };
  disk: { free: number; total: number };
  needed: { download: number; disk: number };
  ready_for: Record<SetupUse, boolean>;
  job: SetupJob | null;
  active: Job | null;
}
