import { element, replaceChildren } from "./dom";
import type {
  KnowledgeActions,
  KnowledgeFactSummary,
  KnowledgeI18n,
} from "./knowledge";
import {
  DEFAULT_TAG_COLOUR,
  closeDialog,
  dialogFrame,
  openDialog,
  renderFactDetail,
  tagChip,
} from "./knowledge-shared";
import type { KnowledgeState } from "./knowledge-shared";

/**
 * Flashcard practice over the saved Knowledge library.
 * User flow and persistence: docs/training-knowledge-integration.md, Adapters.
 */

/** Which fact side is shown concealed before the reveal. */
export type PracticeSide = "title" | "description";

export interface PracticeSettings {
  /** Positive integer number of cards per run. */
  count: number;
  /** The side shown before the card is revealed. */
  side: PracticeSide;
  /**
   * Tag names a fact must carry to be eligible. Empty means every fact in
   * the current practice scope; several names mean ALL of them (the same
   * all-selected-tags semantics as the library's tag filter).
   */
  tags: string[];
}

export const PRACTICE_COUNT_MIN = 1;
export const PRACTICE_COUNT_MAX = 1000;
export const PRACTICE_TAGS_MAX = 32;

export const DEFAULT_PRACTICE_SETTINGS: PracticeSettings = {
  count: 10,
  side: "title",
  tags: [],
};

const practiceSides: readonly PracticeSide[] = ["title", "description"];

export const PRACTICE_SETTINGS_STORAGE_KEY = "sitg:miniapp:knowledge:practice-settings";

export function isValidPracticeCount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value)
    && value >= PRACTICE_COUNT_MIN && value <= PRACTICE_COUNT_MAX;
}

/**
 * Unknown tag names must survive restoration: dropping a removed tag would
 * silently widen the intended filter.
 */
export function sanitizePracticeTags(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const names: string[] = [];
  for (const item of value) {
    if (typeof item !== "string") continue;
    const name = item.trim();
    if (name && !names.includes(name)) names.push(name);
  }
  return names;
}

export class PracticeSettingsStore {
  private readonly storage: Storage | undefined;
  private readonly tagsStorageKey: string | undefined;

  constructor(storage?: Storage, playerId?: string | null) {
    if (typeof playerId === "string" && playerId.trim()) {
      this.tagsStorageKey = `${PRACTICE_SETTINGS_STORAGE_KEY}:tags:${encodeURIComponent(playerId)}`;
    }
    try {
      this.storage = storage ?? window.localStorage;
    } catch {
      this.storage = undefined;
    }
  }

  read(): PracticeSettings {
    const settings: PracticeSettings = { ...DEFAULT_PRACTICE_SETTINGS, tags: [] };
    try {
      const raw = this.storage?.getItem(PRACTICE_SETTINGS_STORAGE_KEY);
      const value: unknown = raw ? JSON.parse(raw) : undefined;
      if (value && typeof value === "object" && !Array.isArray(value)) {
        const candidate = value as Record<string, unknown>;
        if (isValidPracticeCount(candidate.count)) settings.count = candidate.count;
        if (practiceSides.includes(candidate.side as PracticeSide)) {
          settings.side = candidate.side as PracticeSide;
        }
      }
    } catch {}
    try {
      const raw = this.tagsStorageKey ? this.storage?.getItem(this.tagsStorageKey) : undefined;
      if (raw) settings.tags = sanitizePracticeTags(JSON.parse(raw));
    } catch {}
    return settings;
  }

  write(settings: PracticeSettings): void {
    try {
      this.storage?.setItem(PRACTICE_SETTINGS_STORAGE_KEY,
        JSON.stringify({ count: settings.count, side: settings.side }));
    } catch {
      // Unavailable storage never blocks a practice run.
    }
    try {
      if (this.tagsStorageKey) {
        this.storage?.setItem(this.tagsStorageKey, JSON.stringify(settings.tags));
      }
    } catch {}
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
 * Combines the library scope's tags with the practice tag selection: every
 * listed tag must be present on a fact (the API's all-selected-tags
 * semantics), so the two sources are merged by union, de-duplicated.
 */
export function mergePracticeTags(
  scopeTags: string[] | undefined,
  settingTags: readonly string[],
): string[] | undefined {
  if (!scopeTags?.length && !settingTags.length) return undefined;
  return [...new Set([...(scopeTags ?? []), ...settingTags])];
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
  /** Live tag palette of the library view, for the tag selection field. */
  tagPalette?: KnowledgeState;
  store?: PracticeSettingsStore;
  playerId?: string | null;
}): void {
  const { i18n, actions, scope } = options;
  const tagPalette = options.tagPalette;
  const store = options.store ?? new PracticeSettingsStore(undefined, options.playerId);
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

  // Tag eligibility selection: same palette and chip rendering as the
  // library's tag widgets, read-only here (no palette maintenance).
  const selectedTags = new Set<string>(settings.tags);
  const tagNotes = element("div", { className: "knowledge-fact-meta" });
  const tagList = element("div", {
    className: "knowledge-tag-options knowledge-practice-tag-options",
    role: "group",
    "aria-label": i18n.t("knowledge.practice.tags"),
  });
  const syncTagOptions = (): void => {
    replaceChildren(tagList);
    const notes: string[] = [];
    if (tagPalette?.tagPoolFailed) notes.push(i18n.t("knowledge.practice.tags_load_failed"));
    // Saved tags missing from the palette stay selectable: removing them
    // silently would widen the intended filter.
    const names = [...new Set([...(tagPalette?.tagPool.keys() ?? []), ...selectedTags])]
      .sort((a, b) => a.localeCompare(b));
    let removed = false;
    for (const name of names) {
      const entry = tagPalette?.tagPool.get(name);
      if (!entry) removed = true;
      const input = element("input", { type: "checkbox", value: name });
      input.checked = selectedTags.has(name);
      input.addEventListener("change", () => {
        if (input.checked) selectedTags.add(name);
        else selectedTags.delete(name);
      });
      tagList.append(element("div", { className: "knowledge-tag-option" },
        element("label", { className: "checkbox-label" }, input,
          tagChip(entry ?? { name, colour: DEFAULT_TAG_COLOUR }))));
    }
    if (removed) notes.push(i18n.t("knowledge.practice.tags_removed"));
    if (!names.length && !tagPalette?.tagPoolFailed) {
      tagList.append(element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.tags.field_empty")));
    }
    tagNotes.replaceChildren(...notes.map((note) =>
      element("p", { className: "knowledge-fact-meta", role: "note" }, note)));
  };
  const tagField = element("fieldset", { className: "knowledge-tag-select" },
    element("legend", {}, i18n.t("knowledge.practice.tags")),
    tagList,
    tagNotes,
    element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.practice.tags_hint")));
  syncTagOptions();
  // The palette loads (or failed) asynchronously: refresh once it settles.
  void tagPalette?.tagPoolReady.then(syncTagOptions).catch(() => { syncTagOptions(); });

  const form = element("form", { className: "settings-form" },
    element("label", {}, i18n.t("knowledge.practice.count"), count),
    element("label", {}, i18n.t("knowledge.practice.side"), side),
    tagField,
    element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.practice.scope_hint")),
    start,
    error);
  const showSettings = (errorKey?: "knowledge.practice.tags_invalid"): void => {
    error.textContent = errorKey ? i18n.t(errorKey, { max: PRACTICE_TAGS_MAX }) : "";
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
      // Sorted for stable persistence; order never affects eligibility.
      tags: [...selectedTags].sort((a, b) => a.localeCompare(b)),
    };
    if (startRun(dialog, body, chosen, i18n, actions, scope, showSettings)) {
      store.write(chosen);
    }
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
  showSettings: (errorKey?: "knowledge.practice.tags_invalid") => void,
): boolean {
  const isActive = (): boolean => dialog.isConnected && dialog.open && !actions.signal?.aborted;
  // Tag eligibility is applied before sampling, on every page request:
  // the API filters by all selected tags, so the merged scope guarantees
  // only facts carrying every chosen tag can enter the run.
  const effectiveScope: PracticeScope = {
    ...scope,
    tags: mergePracticeTags(scope.tags, settings.tags),
  };
  if ((effectiveScope.tags?.length ?? 0) > PRACTICE_TAGS_MAX) {
    showSettings("knowledge.practice.tags_invalid");
    return false;
  }
  replaceChildren(body,
    element("p", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.practice.loading")));
  void loadPracticeFacts(actions, effectiveScope, isActive).then((items) => {
    if (!isActive()) return;
    if (!items.length) {
      renderNotice(body, i18n, dialog, "knowledge.practice.empty", "error-text");
      return;
    }
    renderRun(dialog, body, settings, i18n, actions, scope, showSettings, items);
  }).catch(() => {
    if (!isActive()) return;
    renderNotice(body, i18n, dialog, "knowledge.error", "error-text");
  });
  return true;
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
  scope: PracticeScope,
  showSettings: (errorKey?: "knowledge.practice.tags_invalid") => void,
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
        className: "secondary-button",
      }, i18n.t("knowledge.practice.restart"));
      restart.addEventListener("click", () => { showSettings(); });
      // Quick restart: another run with the last played count, side and tags
      // immediately — no settings window, fresh random draw, concealed cards.
      const quickRestart = element("button", {
        type: "button",
        className: "primary-button",
      }, i18n.t("knowledge.practice.quick_restart"));
      quickRestart.addEventListener("click", () => {
        startRun(dialog, body, settings, i18n, actions, scope, showSettings);
      });
      replaceChildren(body,
        element("p", { className: "knowledge-practice-done" }, i18n.t("knowledge.practice.done")),
        element("div", { className: "settings-actions" }, quickRestart, restart, closeRow(i18n, dialog)));
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
