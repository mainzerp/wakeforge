import { plural, t } from "./i18n.svelte";
import type { Project } from "./types";

export interface Step {
  id: string;
  label: string;
  icon: string;
  sub: string;
  /** what to do on the step's page and why, in the order the page expects it */
  how: string;
  available: boolean;
  optional?: boolean;
}

export const STEPS = [
  {
    id: "wakeword", label: "Wake word", icon: "type", available: true,
    sub: "Name, language and how the TTS voices should pronounce it.",
    how: "Add the spellings the voices should read (a phonetic spelling like “Dscharwis” helps), run the pronunciation check and listen. Then let “Suggest” find sound-alike words: the model learns to ignore them.",
  },
  {
    id: "generate", label: "Synthetic samples", icon: "sparkles", available: true,
    sub: "Hundreds of cloned German voices (OmniVoice) saying your wake word, checked phonetically.",
    how: "Generate the wake word clips first (20,000 is a good default, about 1.5 hours), then the sound-alike negatives below. Both run in the background; listen to a few clips to make sure they sound right.",
  },
  {
    id: "record", label: "Own recordings", icon: "mic", available: true, optional: true,
    sub: "Record takes, split them, sort every clip.",
    how: "Optional, but real voices help most: record a take in which you (and others) say the wake word a few times with pauses, it is split into clips automatically. Mark each clip Pos (the wake word) or Neg (anything else, e.g. similar words). New clips count from the next training run.",
  },
  {
    id: "train", label: "Training", icon: "pulse", available: true,
    sub: "Featurize, train on the GPU and calibrate the on-device operating point.",
    how: "Fast (about 20 minutes) gives a first model to try, Thorough (about 40 minutes) is the one to keep. Every run picks its own cutoff for the false alarm target; the export step hands it to ESPHome. Train again whenever new clips arrived, e.g. after mining.",
  },
  {
    id: "mining", label: "Mining", icon: "radar", available: true, optional: true,
    sub: "Run a trained model over hours of real speech, listen to what triggers it and feed the false triggers back.",
    how: "Scan with the newest model; an automatic check labels the clear false triggers and leaves the doubtful ones for you. Then train again and scan the new model: repeat until hardly anything triggers.",
  },
  {
    id: "export", label: "Export", icon: "download", available: true,
    sub: "Pick an operating point and ship it to ESPHome.",
    how: "Choose a model (normally the newest) and slide the cutoff: higher means fewer false alarms but more misses. The calibrated point is a good start; if the model was scanned in the mining step, you also see its false alarms on real speech. Download it as .zip (manifest + model) and put both files next to the device's ESPHome YAML.",
  },
] as const satisfies readonly Step[];

export type StepId = (typeof STEPS)[number]["id"];

export const isStep = (id: string | undefined): id is StepId => STEPS.some((s) => s.id === id);
export const stepIndex = (id: StepId) => STEPS.findIndex((s) => s.id === id);

export function stepState(id: StepId, p: Project): { done: boolean; metric: string } {
  const s = p.summary;
  switch (id) {
    case "wakeword":
      return { done: !!p.wake_word, metric: p.wake_word };
    case "generate":
      return { done: s.generated > 0, metric: plural(s.generated, "{n} clip", "{n} clips") };
    case "record":
      return { done: s.positive > 0, metric: t("{pos} pos · {neg} neg", { pos: s.positive, neg: s.negative + s.borrowed }) };
    case "train":
      return { done: s.runs > 0, metric: plural(s.runs, "{n} model", "{n} models") };
    case "mining":
      return { done: s.mined > 0, metric: s.mined ? t("{n} mined", { n: s.mined }) : t("after training") };
    case "export":
      return { done: s.models > 0, metric: plural(s.models, "{n} model", "{n} models") };
  }
}

/** The step to work on next: first open step that already works, else the first open one. Mining is never "next". */
export function nextStep(p: Project): StepId {
  const open = STEPS.filter((s) => s.id !== "mining" && !stepState(s.id, p).done);
  return (open.find((s) => s.available) ?? open[0] ?? STEPS[STEPS.length - 1]).id;
}
