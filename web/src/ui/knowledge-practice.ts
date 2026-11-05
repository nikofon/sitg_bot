import { element, replaceChildren } from "./dom";
import type {
  KnowledgeActions,
  KnowledgeFactSummary,
  KnowledgeI18n,
} from "./knowledge";
import {
  closeDialog,
  dialogFrame,
  openDialog,
  renderFactDetail,
} from "./knowledge-shared";

/**
 * Flashcard practice over the saved Knowledge library: a settings window
 * (persisted device-locally), then a run of N randomly selected distinct
 * facts shown one at a time. Each card starts concealed on the chosen side
 * (name or description); activating it reveals the complete fact through
 * the normal detail contract and renderer, without editing controls.
 */

/** Which fact side is shown concealed before the reveal. */
export type PracticeSide = "title" | "description";

export interface PracticeSettings {
  /** Positive integer number of cards per run. */
  count: number;
  /** The side shown before the card is revealed. */
  side: PracticeSide;
}

export const PRACTICE_COUNT_MIN = 1;
export const PRACTICE_COUNT_MAX = 1000;

export const DEFAULT_PRACTICE_SETTINGS: PracticeSettings = { count: 10, side: "title" };

const practiceSides: readonly PracticeSide[] = ["title", "description"];

/**
 * Device-local settings storage. Only these two settings are stored, in
 * localStorage, so they survive app visits on the same device; no fact
 * content, no account or cloud synchronization. Corrupt, invalid or
 * unavailable data falls back to defaults; write failures are ignored
 * (the run still works, settings just do not persist).
 */
export const PRACTICE_SETTINGS_STORAGE_KEY = "sitg:miniapp:knowledge:practice-settings";

export function isValidPracticeCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value)
    && value >= PRACTICE_COUNT_MIN && value <= PRACTICE_COUNT_MAX;
}

export class PracticeSettingsStore {
  private readonly storage: Storage | undefined;

  constructor(storage?: Storage) {
    try {
      this.storage = storage ?? window.localStorage;
    } catch {
      this.storage = undefined;
    }
  }

  read(): PracticeSettings {
    try {
      const raw = this.storage?.getItem(PRACTICE_SETTINGS_STORAGE_KEY);
      if (!raw) return { ...DEFAULT_PRACTICE_SETTINGS };
      const value: unknown = JSON.parse(raw);
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        return { ...DEFAULT_PRACTICE_SETTINGS };
      }
      const candidate = value as Record<string, unknown>;
      const side = practiceSides.includes(candidate.side as PracticeSide)
        ? (candidate.side as PracticeSide)
        : DEFAULT_PRACTICE_SETTINGS.side;
      return {
        count: isValidPracticeCount(candidate.count)
          ? candidate.count
          : DEFAULT_PRACTICE_SETTINGS.count,
        side,
      };
    } catch {
      return { ...DEFAULT_PRACTICE_SETTINGS };
    }
  }

  write(settings: PracticeSettings): void {
    try {
      this.storage?.setItem(PRACTICE_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
    } catch {
      // Unavailable storage never blocks a practice run.
    }
  }
}

/** Parses the settings count field; null for anything not a valid count. */
export function parsePracticeCount(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return isValidPracticeCount(parsed) ? parsed : null;
}

/** Current library filter snapshot the run draws from. */
export interface PracticeScope {
  search?: string;
  author?: string;
  tags?: string[];
}

/**
 * Loads every page of the scoped saved library, not just the loaded first
 * page: fact pages are capped at 25 with an opaque cursor, so the run keeps
 * following `next_cursor` until it ends. Duplicate fact ids are skipped
 * defensively; a repeated cursor stops a corrupt chain and returns only
 * what was actually collected.
 */
export async function loadPracticeFacts(
  actions: KnowledgeActions,
  scope: PracticeScope,
  isActive: () => boolean,
): Promise<KnowledgeFactSummary[]> {
  const items: KnowledgeFactSummary[] = [];
  const seenIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  do {
    if (!isActive()) return items;
    const page = await actions.loadFacts({
      ...(cursor ? { cursor } : {}),
      ...(scope.search ? { search: scope.search } : {}),
      ...(scope.author ? { author: scope.author } : {}),
      ...(scope.tags?.length ? { tags: scope.tags } : {}),
      order: "title_asc",
    });
    for (const fact of page.items) {
      if (!seenIds.has(fact.id)) {
        seenIds.add(fact.id);
        items.push(fact);
      }
    }
    cursor = page.next_cursor;
    if (cursor !== null) {
      if (seenCursors.has(cursor)) return items;
      seenCursors.add(cursor);
    }
  } while (cursor !== null);
  return items;
}

/**
 * Distinct random selection: a Fisher-Yates shuffle over positions,
 * so every card in a run is a different fact. Fewer available facts than
 * requested yields all of them (the run reports the shortfall).
 */
export function selectPracticeFacts(
  items: readonly KnowledgeFactSummary[],
  count: number,
): KnowledgeFactSummary[] {
  const pool = [...items];
  const take = Math.max(0, Math.min(count, pool.length));
  for (let index = pool.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1));
    const current = pool[index]!;
    pool[index] = pool[swap]!;
    pool[swap] = current;
  }
  return pool.slice(0, take);
}

export function openPracticeDialog(options: {
  i18n: KnowledgeI18n;
  actions: KnowledgeActions;
  scope: PracticeScope;
  store?: PracticeSettingsStore;
}): void {
  const { i18n, actions, scope } = options;
  const store = options.store ?? new PracticeSettingsStore();
  const { dialog, body } = dialogFrame(i18n.t("knowledge.practice.title"), i18n);
  const settings = store.read();
  const count = element("input", {
    type: "number",
    inputmode: "numeric",
    min: String(PRACTICE_COUNT_MIN),
    max: String(PRACTICE_COUNT_MAX),
    step: "1",
    value: String(settings.count),
    required: true,
    "aria-label": i18n.t("knowledge.practice.count"),
  });
  const side = element("select", { "aria-label": i18n.t("knowledge.practice.side") },
    element("option", { value: "title" }, i18n.t("knowledge.practice.side.title")),
    element("option", { value: "description" }, i18n.t("knowledge.practice.side.description")));
  side.value = settings.side;
  const error = element("p", { className: "error-text", role: "alert" });
  const start = element("button", { type: "submit", className: "primary-button" },
    i18n.t("knowledge.practice.start"));
  const form = element("form", { className: "settings-form" },
    element("label", {}, i18n.t("knowledge.practice.count"), count),
    element("label", {}, i18n.t("knowledge.practice.side"), side),
    element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.practice.scope_hint")),
    start,
    error);
  const showSettings = (): void => {
    error.textContent = "";
    replaceChildren(body, form);
    count.focus();
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const parsed = parsePracticeCount(count.value);
    if (parsed === null) {
      error.textContent = i18n.t("knowledge.practice.count_invalid");
      return;
    }
    const chosen: PracticeSettings = {
      count: parsed,
      side: side.value === "description" ? "description" : "title",
    };
    // Saved between runs and app visits on this device.
    store.write(chosen);
    startRun(dialog, body, chosen, i18n, actions, scope, showSettings);
  });
  openDialog(dialog);
  showSettings();
}

function startRun(
  dialog: HTMLDialogElement,
  body: HTMLElement,
  settings: PracticeSettings,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  scope: PracticeScope,
  showSettings: () => void,
): void {
  const isActive = (): boolean => dialog.isConnected && dialog.open && !actions.signal?.aborted;
  replaceChildren(body,
    element("p", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.practice.loading")));
  void loadPracticeFacts(actions, scope, isActive).then((items) => {
    if (!isActive()) return;
    if (!items.length) {
      renderNotice(body, i18n, dialog, "knowledge.practice.empty", "error-text");
      return;
    }
    renderRun(dialog, body, settings, i18n, actions, showSettings, items);
  }).catch(() => {
    if (!isActive()) return;
    renderNotice(body, i18n, dialog, "knowledge.error", "error-text");
  });
}

function renderNotice(
  body: HTMLElement,
  i18n: KnowledgeI18n,
  dialog: HTMLDialogElement,
  key: "knowledge.practice.empty" | "knowledge.error",
  className: string,
): void {
  replaceChildren(body,
    element("p", { className, role: "alert" }, i18n.t(key)),
    closeRow(i18n, dialog));
}

function closeRow(i18n: KnowledgeI18n, dialog: HTMLDialogElement): HTMLElement {
  return element("div", { className: "settings-actions" },
    element("button", {
      type: "button",
      className: "secondary-button",
      onclick: (() => { closeDialog(dialog); }) as EventListener,
    }, i18n.t("common.close")));
}

function renderRun(
  dialog: HTMLDialogElement,
  body: HTMLElement,
  settings: PracticeSettings,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  showSettings: () => void,
  items: readonly KnowledgeFactSummary[],
): void {
  const cards = selectPracticeFacts(items, settings.count);
  const fewer = cards.length < settings.count;
  let index = 0;
  let cardGeneration = 0;
  const progress = element("p", { className: "knowledge-practice-progress", role: "status",
    "aria-live": "polite" });
  const cardHolder = element("div", { className: "knowledge-practice-card" });
  const next = element("button", { type: "button", className: "primary-button" },
    i18n.t("knowledge.practice.next"));

  const reveal = (fact: KnowledgeFactSummary, generation: number): void => {
    const isCurrent = (): boolean => generation === cardGeneration
      && cardHolder.isConnected && dialog.open && !actions.signal?.aborted;
    if (!isCurrent()) return;
    replaceChildren(cardHolder,
      element("p", { className: "knowledge-fact-meta", role: "status" }, i18n.t("knowledge.loading")));
    void actions.loadFact(fact.id).then((detail) => {
      if (!isCurrent()) return;
      // Same renderer and detail contract as a normal library card: the full
      // fact view without editing controls, private provenance stays hidden.
      const detailBody = element("div", { className: "knowledge-fact-body" });
      renderFactDetail(detail, i18n, detailBody, () => {});
      replaceChildren(cardHolder,
        element("article", { className: "resource-card knowledge-fact knowledge-practice-revealed" },
          element("h3", { className: "knowledge-fact-title" }, detail.title),
          detailBody));
    }).catch(() => {
      if (!isCurrent()) return;
      // Honest failure: no opposite side leaks on a failed detail read.
      const retry = element("button", {
        type: "button",
        className: "secondary-button",
        onclick: (() => { reveal(fact, generation); }) as EventListener,
      }, i18n.t("knowledge.refresh"));
      replaceChildren(cardHolder,
        element("p", { className: "error-text", role: "alert" }, i18n.t("knowledge.error")),
        retry);
    });
  };

  const renderCurrent = (): void => {
    const generation = ++cardGeneration;
    const fact = cards[index]!;
    progress.textContent = i18n.t("knowledge.practice.progress", {
      index: index + 1, total: cards.length,
    });
    next.textContent = index === cards.length - 1
      ? i18n.t("knowledge.practice.finish")
      : i18n.t("knowledge.practice.next");
    // Every card starts concealed: only the chosen side, never the other one.
    const concealed = settings.side === "title"
      ? fact.title
      : (fact.description?.trim() || i18n.t("knowledge.practice.no_description"));
    const prompt = element("button", {
      type: "button",
      className: "knowledge-practice-prompt",
    },
      element("span", { className: "knowledge-practice-prompt-text" }, concealed),
      element("span", { className: "knowledge-practice-reveal-hint" },
        i18n.t("knowledge.practice.reveal_hint")));
    prompt.addEventListener("click", () => { reveal(fact, generation); });
    replaceChildren(cardHolder,
      element("article", { className: "resource-card knowledge-fact knowledge-practice-card" }, prompt));
  };

  next.addEventListener("click", () => {
    if (index === cards.length - 1) {
      cardGeneration += 1;
      const restart = element("button", {
        type: "button",
        className: "primary-button",
      }, i18n.t("knowledge.practice.restart"));
      restart.addEventListener("click", showSettings);
      replaceChildren(body,
        element("p", { className: "knowledge-practice-done" }, i18n.t("knowledge.practice.done")),
        element("div", { className: "settings-actions" }, restart, closeRow(i18n, dialog)));
      return;
    }
    index += 1;
    renderCurrent();
  });

  replaceChildren(body,
    progress,
    fewer ? element("p", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.practice.fewer", { count: cards.length })) : null,
    cardHolder,
    element("div", { className: "settings-actions knowledge-practice-actions" }, next));
  renderCurrent();
}
