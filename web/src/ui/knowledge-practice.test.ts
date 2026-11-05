import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeActions,
  KnowledgeFactDetail,
  KnowledgeFactSummary,
  KnowledgeFactsResource,
  KnowledgeViewData,
} from "./knowledge";
import { renderKnowledge } from "./knowledge";
import {
  DEFAULT_PRACTICE_SETTINGS,
  PRACTICE_SETTINGS_STORAGE_KEY,
  PracticeSettingsStore,
  parsePracticeCount,
} from "./knowledge-practice";

const messages: Record<string, string> = {
  "knowledge.title": "Knowledge library",
  "knowledge.tab.library": "Library",
  "knowledge.tab.packets": "Packets",
  "knowledge.tab.imports": "Imports",
  "knowledge.search": "Search facts",
  "knowledge.author_search": "Search by author",
  "knowledge.order.title_asc": "Title A–Z",
  "knowledge.order.title_desc": "Title Z–A",
  "knowledge.order.updated_desc": "Recently updated",
  "knowledge.tag_filter": "Filter by tag",
  "knowledge.total": "{total} facts",
  "knowledge.empty": "No facts yet.",
  "knowledge.loading": "Loading…",
  "knowledge.error": "Something went wrong.",
  "knowledge.load_more": "Load more",
  "knowledge.refresh": "Refresh",
  "knowledge.create": "Add fact",
  "knowledge.share": "Share library",
  "knowledge.fact.links": "No source links.",
  "knowledge.fact.author": "Author: {author}",
  "knowledge.fact.read_only": "Read-only shared fact",
  "knowledge.statistic.none": "No statistics",
  "common.close": "Close",
  "knowledge.practice": "Flashcard practice",
  "knowledge.practice.title": "Flashcard practice",
  "knowledge.practice.count": "Cards per run",
  "knowledge.practice.count_invalid": "Enter a whole number from 1 to 1000.",
  "knowledge.practice.side": "Show first",
  "knowledge.practice.side.title": "Name",
  "knowledge.practice.side.description": "Description",
  "knowledge.practice.scope_hint": "Cards are drawn randomly from the facts currently matching your search, author and tag filters, across all library pages.",
  "knowledge.practice.start": "Start practice",
  "knowledge.practice.loading": "Collecting cards…",
  "knowledge.practice.progress": "Card {index} of {total}",
  "knowledge.practice.reveal_hint": "Tap the card to reveal the fact",
  "knowledge.practice.next": "Next card",
  "knowledge.practice.finish": "Finish",
  "knowledge.practice.done": "Practice run complete.",
  "knowledge.practice.restart": "Practice again",
  "knowledge.practice.empty": "No facts match your filters yet — nothing to practice with.",
  "knowledge.practice.fewer": "Only {count} facts available, so the run uses all of them.",
  "knowledge.practice.no_description": "This fact has no description.",
};

const i18n = {
  t: (key: string, parameters: Record<string, unknown> = {}): string =>
    (messages[key] ?? key).replace(/\{(\w+)\}/g, (_: string, name: string): string => {
      const value = parameters[name];
      return typeof value === "string" || typeof value === "number" ? String(value) : `{${name}}`;
    }),
};

const fact = (id: number, overrides: Partial<KnowledgeFactSummary> = {}): KnowledgeFactSummary => ({
  id: `fact-${id}`,
  title: `Title ${id}`,
  description: `Description ${id}`,
  tag_names: [],
  origin: "manual",
  updated_at: "2026-10-01",
  statistics: { status: "none" },
  ...overrides,
});

const detail = (id: string): KnowledgeFactDetail => ({
  id,
  title: `Title ${id.replace("fact-", "")}`,
  description: `Full description of ${id}`,
  links: [{ url: "https://example.com/source", title: "Source" }],
  tags: [{ id: "tag-1", name: "history", colour: "#2e7d32" }],
  origin: "manual",
  author: "Ada",
  content_version: 1,
  editable: false,
  images: [],
  statistics: { status: "none" },
});

const view = (items: KnowledgeFactSummary[]): KnowledgeViewData => ({
  facts: {
    state: items.length ? "ready" : "empty",
    items,
    total: items.length,
    next_cursor: null,
    order: "title_asc",
    search: "",
  },
  sharing: { grants: [], shared: [], links: [] },
  dissection: { packets: [], requests: [], next_cursor: null },
});

/** Paged loadFacts: cursor "1" is the second page, null/undefined the first. */
const pagedActions = (
  pages: KnowledgeFactSummary[][],
  overrides: Partial<KnowledgeActions> = {},
): KnowledgeActions => ({
  loadFacts: vi.fn((params: { cursor?: string | null; search?: string; author?: string; tags?: string[] }) => {
    const index = params.cursor ? Number(params.cursor) : 0;
    const items = pages[index] ?? [];
    const resource: KnowledgeFactsResource = {
      state: items.length ? "ready" : "empty",
      items,
      total: pages.flat().length,
      next_cursor: index + 1 < pages.length ? String(index + 1) : null,
      order: "title_asc",
    };
    return Promise.resolve(resource);
  }),
  loadFact: vi.fn((id: string) => Promise.resolve(detail(id))),
  dissect: vi.fn().mockResolvedValue({}),
  loadTags: vi.fn().mockResolvedValue({ items: [] }),
  ...overrides,
});

const render = (
  pages: KnowledgeFactSummary[][] = [[fact(1), fact(2), fact(3)]],
  actions: KnowledgeActions = pagedActions(pages),
) => {
  const root = renderKnowledge(view(pages.flat()), i18n as never, actions);
  document.body.append(root);
  const button = (text: string, scope: ParentNode = document): HTMLButtonElement =>
    [...scope.querySelectorAll("button")].find((item) => item.textContent === text)!;
  return { root, actions, button };
};

const flush = async (): Promise<void> => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
};

const openPractice = (setup: ReturnType<typeof render>): HTMLDialogElement => {
  setup.button("Flashcard practice").click();
  const dialog = document.querySelector<HTMLDialogElement>("dialog.knowledge-dialog")!;
  expect(dialog).toBeDefined();
  return dialog;
};

const submitSettings = (dialog: HTMLDialogElement, count: string, side?: string): void => {
  const input = dialog.querySelector<HTMLInputElement>("input[type=number]")!;
  input.value = count;
  if (side) {
    const select = dialog.querySelector<HTMLSelectElement>("select")!;
    select.value = side;
  }
  dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
};

const prompt = (dialog: HTMLDialogElement): HTMLButtonElement =>
  dialog.querySelector<HTMLButtonElement>(".knowledge-practice-prompt")!;

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("practice settings store", () => {
  it("returns defaults when nothing is stored", () => {
    expect(new PracticeSettingsStore().read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
  });

  it("round-trips settings", () => {
    const store = new PracticeSettingsStore();
    store.write({ count: 7, side: "description" });
    expect(new PracticeSettingsStore().read()).toEqual({ count: 7, side: "description" });
  });

  it("falls back per field on invalid stored values", () => {
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY,
      JSON.stringify({ count: 0, side: "bogus", extra: "ignored" }));
    expect(new PracticeSettingsStore().read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY, JSON.stringify({ count: 3.5 }));
    expect(new PracticeSettingsStore().read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY, JSON.stringify({ count: 4, side: "description" }));
    expect(new PracticeSettingsStore().read()).toEqual({ count: 4, side: "description" });
  });

  it("ignores corrupt stored JSON and unavailable storage", () => {
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY, "{not json");
    expect(new PracticeSettingsStore().read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY, "[1,2]");
    expect(new PracticeSettingsStore().read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
    const throwing: Storage = {
      ...window.localStorage,
      getItem: () => { throw new Error("unavailable"); },
      setItem: () => { throw new Error("unavailable"); },
    };
    const store = new PracticeSettingsStore(throwing);
    expect(store.read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
    expect(() => store.write({ count: 5, side: "title" })).not.toThrow();
  });

  it("uses defaults and allows practice when acquiring localStorage throws", async () => {
    const setup = render();
    const storage = window.localStorage;
    const acquisition = vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("Storage access denied", "SecurityError");
    });
    try {
      const store = new PracticeSettingsStore();
      expect(store.read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
      expect(() => store.write({ count: 5, side: "description" })).not.toThrow();
      expect(store.read()).toEqual(DEFAULT_PRACTICE_SETTINGS);
      const supplied = new PracticeSettingsStore(storage);
      supplied.write({ count: 4, side: "description" });
      expect(supplied.read()).toEqual({ count: 4, side: "description" });

      const dialog = openPractice(setup);
      expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.value)
        .toBe(String(DEFAULT_PRACTICE_SETTINGS.count));
      expect(dialog.querySelector<HTMLSelectElement>("select")!.value)
        .toBe(DEFAULT_PRACTICE_SETTINGS.side);
      submitSettings(dialog, "1", "description");
      await flush();
      expect(prompt(dialog)).not.toBeNull();
      prompt(dialog).click();
      await flush();
      expect(dialog.querySelector(".knowledge-practice-revealed")!.textContent)
        .toContain("Full description of fact-");
    } finally {
      acquisition.mockRestore();
    }
  });

  it("parses count inputs strictly", () => {
    expect(parsePracticeCount("5")).toBe(5);
    expect(parsePracticeCount(" 12 ")).toBe(12);
    expect(parsePracticeCount("0")).toBeNull();
    expect(parsePracticeCount("-3")).toBeNull();
    expect(parsePracticeCount("2.5")).toBeNull();
    expect(parsePracticeCount("abc")).toBeNull();
    expect(parsePracticeCount("1001")).toBeNull();
  });
});

describe("flashcard practice", () => {
  it("restores persisted settings and saves new ones between runs", async () => {
    window.localStorage.setItem(PRACTICE_SETTINGS_STORAGE_KEY,
      JSON.stringify({ count: 7, side: "description" }));
    const setup = render();
    let dialog = openPractice(setup);
    expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.value).toBe("7");
    expect(dialog.querySelector<HTMLSelectElement>("select")!.value).toBe("description");

    submitSettings(dialog, "3", "title");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    expect(JSON.parse(window.localStorage.getItem(PRACTICE_SETTINGS_STORAGE_KEY)!))
      .toEqual({ count: 3, side: "title" });

    // A later visit reopens with the last saved settings.
    [...dialog.querySelectorAll("button")]
      .find((item) => item.getAttribute("aria-label") === "Close")!.click();
    setup.button("Flashcard practice").click();
    dialog = document.querySelector<HTMLDialogElement>("dialog.knowledge-dialog")!;
    expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.value).toBe("3");
    expect(dialog.querySelector<HTMLSelectElement>("select")!.value).toBe("title");
  });

  it("rejects invalid counts without starting a run", () => {
    const setup = render();
    const dialog = openPractice(setup);
    for (const value of ["0", "abc", "2.5", ""]) {
      submitSettings(dialog, value);
      expect(dialog.querySelector("form")!.textContent).toContain("Enter a whole number from 1 to 1000.");
    }
    expect(setup.actions.loadFacts).not.toHaveBeenCalled();
    expect(prompt(dialog)).toBeNull();
  });

  it("conceals the description side only, with a placeholder for missing descriptions", async () => {
    const pages = [[fact(1), fact(2, { description: undefined })]];
    const setup = render(pages);
    const dialog = openPractice(setup);
    submitSettings(dialog, "2", "description");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    const texts: string[] = [];
    for (let step = 0; step < 2; step += 1) {
      const current = prompt(dialog);
      texts.push(current.querySelector(".knowledge-practice-prompt-text")!.textContent!);
      // The concealed card never exposes the opposite side.
      expect(dialog.querySelector(".knowledge-fact-description")).toBeNull();
      expect(dialog.querySelector(".knowledge-fact-title")).toBeNull();
      if (step === 0) setup.button("Next card", dialog).click();
    }
    expect(texts).toContain("Description 1");
    expect(texts).toContain("This fact has no description.");
    for (const text of texts) expect(text).not.toMatch(/^Title /);
  });

  it("conceals the title side only", async () => {
    const setup = render();
    const dialog = openPractice(setup);
    submitSettings(dialog, "1", "title");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    console.log("DBG1", dialog.outerHTML.slice(0, 400));
    console.log("DBG2", !!prompt(dialog));
    expect(prompt(dialog)!.textContent).toContain("Title ");
    expect(prompt(dialog).textContent).not.toContain("Description ");
    expect(dialog.querySelector(".knowledge-fact-description")).toBeNull();
  });

  it("reveals the complete fact through the same renderer as the library card", async () => {
    const setup = render();
    const dialog = openPractice(setup);
    submitSettings(dialog, "1", "title");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    const promptText = prompt(dialog).querySelector(".knowledge-practice-prompt-text")!.textContent!;
    const factId = `fact-${promptText.replace("Title ", "")}`;
    prompt(dialog).click();
    await vi.waitFor(() => {
      expect(dialog.querySelector(".knowledge-practice-revealed")).not.toBeNull();
    });
    expect(setup.actions.loadFact).toHaveBeenCalledWith(factId);

    // Library expansion of the same fact renders the same detail contract.
    setup.root.querySelector<HTMLButtonElement>(`[data-fact-id="${factId}"] .knowledge-fact-header`)!.click();
    await vi.waitFor(() => {
      expect(setup.root.querySelector(`[data-fact-id="${factId}"] .knowledge-fact-body`)!.textContent)
        .toContain(`Full description of ${factId}`);
    });
    const libraryBody = setup.root.querySelector(`[data-fact-id="${factId}"] .knowledge-fact-body`)!;
    const revealedCard = dialog.querySelector<HTMLElement>(".knowledge-practice-revealed")!;
    // Parity: the revealed body matches the normal library card body.
    expect(revealedCard.querySelector(".knowledge-fact-body")!.textContent)
      .toBe(libraryBody.textContent);
    expect(revealedCard.querySelector(".knowledge-fact-title")!.textContent).toBe(promptText);
    expect(revealedCard.querySelector("a")!.getAttribute("href")).toBe("https://example.com/source");
    expect(revealedCard.textContent).toContain("Author: Ada");
  });

  it("selects distinct cards across every library page, one card at a time", async () => {
    const pages = [
      Array.from({ length: 25 }, (_, index) => fact(index + 1)),
      Array.from({ length: 25 }, (_, index) => fact(index + 26)),
      Array.from({ length: 10 }, (_, index) => fact(index + 51)),
    ];
    const setup = render(pages);
    const dialog = openPractice(setup);
    submitSettings(dialog, "60");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    // All three pages were followed through their opaque cursors.
    const calls = (setup.actions.loadFacts as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.map((call) => call[0].cursor ?? null)).toEqual([null, "1", "2"]);

    const seen: string[] = [];
    for (let step = 0; step < 60; step += 1) {
      seen.push(prompt(dialog).querySelector(".knowledge-practice-prompt-text")!.textContent!);
      const next = setup.button(step === 59 ? "Finish" : "Next card", dialog);
      if (step === 59) break;
      next.click();
    }
    expect(seen.length).toBe(60);
    expect(new Set(seen).size).toBe(60);
    expect(seen).toContain("Title 60");
  });

  it("starts every next card concealed with updated progress", async () => {
    const setup = render();
    const dialog = openPractice(setup);
    submitSettings(dialog, "2");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    expect(dialog.querySelector(".knowledge-practice-progress")!.textContent).toBe("Card 1 of 2");

    prompt(dialog).click();
    await vi.waitFor(() => {
      expect(dialog.querySelector(".knowledge-practice-revealed")).toBeDefined();
    });
    setup.button("Next card", dialog).click();

    expect(prompt(dialog)).not.toBeNull();
    expect(dialog.querySelector(".knowledge-practice-revealed")).toBeNull();
    expect(dialog.querySelector(".knowledge-fact-description")).toBeNull();
    expect(dialog.querySelector(".knowledge-practice-progress")!.textContent).toBe("Card 2 of 2");
  });

  it.each(["title", "description"].flatMap((side) =>
    ["success", "failure"].flatMap((outcome) =>
      ["concealed", "revealed", "settings"].map((destination) => ({ side, outcome, destination })))))(
    "ignores late $outcome on the $side side after advancing to $destination",
    async ({ side, outcome, destination }) => {
      const pending = deferred<KnowledgeFactDetail>();
      const loadFact = vi.fn((id: string) => Promise.resolve(detail(id)))
        .mockImplementationOnce(() => pending.promise);
      const setup = render(undefined, pagedActions([[fact(1), fact(2)]], { loadFact }));
      const dialog = openPractice(setup);
      submitSettings(dialog, "2", side);
      await flush();
      prompt(dialog).click();
      const previousId = loadFact.mock.calls[0]![0];
      setup.button("Next card", dialog).click();
      const nextText = prompt(dialog).textContent;
      expect(dialog.querySelector(".knowledge-practice-progress")!.textContent).toBe("Card 2 of 2");

      if (destination === "revealed") {
        prompt(dialog).click();
        await flush();
      } else if (destination === "settings") {
        setup.button("Finish", dialog).click();
        setup.button("Practice again", dialog).click();
        expect(dialog.querySelector("form")).not.toBeNull();
      }
      if (outcome === "success") pending.resolve(detail(previousId));
      else pending.reject(new Error("late failure"));
      await flush();

      expect(dialog.querySelector(".error-text")?.textContent ?? "").toBe("");
      expect(setup.button("Refresh", dialog)).toBeUndefined();
      if (destination === "settings") {
        expect(dialog.querySelector("form")).not.toBeNull();
        expect(dialog.querySelector(".knowledge-practice-revealed")).toBeNull();
        expect(setup.actions.loadFacts).toHaveBeenCalledTimes(1);
      } else {
        if (destination === "concealed") {
          expect(prompt(dialog).textContent).toBe(nextText);
          expect(dialog.querySelector(".knowledge-practice-revealed")).toBeNull();
          prompt(dialog).click();
          await flush();
        }
        const currentId = loadFact.mock.calls[1]![0];
        expect(currentId).not.toBe(previousId);
        expect(dialog.querySelector(".knowledge-practice-revealed")!.textContent)
          .toContain(`Full description of ${currentId}`);
        expect(dialog.querySelector(".knowledge-practice-revealed")!.textContent)
          .not.toContain(`Full description of ${previousId}`);
      }
    },
  );

  it("requires saved settings confirmation before every restarted run and exits via close", async () => {
    const setup = render();
    const dialog = openPractice(setup);
    submitSettings(dialog, "1", "description");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    setup.button("Finish", dialog).click();
    expect(dialog.textContent).toContain("Practice run complete.");

    setup.button("Practice again", dialog).click();
    expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.value).toBe("1");
    expect(dialog.querySelector<HTMLSelectElement>("select")!.value).toBe("description");
    expect(prompt(dialog)).toBeNull();
    expect(setup.actions.loadFacts).toHaveBeenCalledTimes(1);
    submitSettings(dialog, "2", "title");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    expect(setup.actions.loadFacts).toHaveBeenCalledTimes(2);
    expect(dialog.querySelector(".knowledge-practice-progress")!.textContent).toBe("Card 1 of 2");
    expect(prompt(dialog).textContent).toContain("Title ");
    expect(new PracticeSettingsStore().read()).toEqual({ count: 2, side: "title" });

    setup.button("Next card", dialog).click();
    setup.button("Finish", dialog).click();
    setup.button("Practice again", dialog).click();
    expect(dialog.querySelector<HTMLInputElement>("input[type=number]")!.value).toBe("2");
    expect(dialog.querySelector<HTMLSelectElement>("select")!.value).toBe("title");
    expect(setup.actions.loadFacts).toHaveBeenCalledTimes(2);

    // Exit: the dialog's dedicated close control removes the game window.
    const closer = [...dialog.querySelectorAll("button")]
      .find((item) => item.getAttribute("aria-label") === "Close")!;
    closer.click();
    expect(document.querySelector("dialog.knowledge-dialog")).toBeNull();
  });

  it.each([0, 1].flatMap((pageIndex) =>
    ["close control", "native close", "parent abort"].map((exit) => ({ pageIndex, exit }))))(
    "stops pagination after $exit while page $pageIndex is pending",
    async ({ pageIndex, exit }) => {
      const pending = deferred<KnowledgeFactsResource>();
      const controller = new AbortController();
      const page = (index: number): KnowledgeFactsResource => ({
        state: "ready", items: [fact(index + 1)], total: 3,
        next_cursor: String(index + 1), order: "title_asc",
      });
      const loadFacts = vi.fn(() => pending.promise);
      if (pageIndex === 1) loadFacts.mockResolvedValueOnce(page(0));
      const setup = render(undefined, pagedActions([[fact(1)]], {
        loadFacts, signal: controller.signal,
      }));
      const dialog = openPractice(setup);
      submitSettings(dialog, "3");
      await flush();
      expect(loadFacts).toHaveBeenCalledTimes(pageIndex + 1);
      if (exit === "close control") {
        dialog.querySelector<HTMLButtonElement>('[aria-label="Close"]')!.click();
      } else if (exit === "native close") {
        dialog.dispatchEvent(new Event("close"));
      } else {
        controller.abort();
      }
      pending.resolve(page(pageIndex));
      await flush();
      expect(loadFacts).toHaveBeenCalledTimes(pageIndex + 1);
      expect(prompt(dialog)).toBeNull();
      expect(dialog.querySelector(".knowledge-practice-revealed")).toBeNull();
      expect(dialog.textContent).not.toContain("Something went wrong.");
      if (exit !== "parent abort") expect(dialog.isConnected).toBe(false);
    },
  );

  it("reports empty libraries instead of starting a run", async () => {
    const setup = render([[]]);
    const dialog = openPractice(setup);
    submitSettings(dialog, "5");
    await vi.waitFor(() => {
      expect(dialog.textContent).toContain("nothing to practice with");
    });
    expect(prompt(dialog)).toBeNull();
    expect(setup.button("Close", dialog)).toBeDefined();
  });

  it("runs with all available facts when fewer than requested exist", async () => {
    const setup = render();
    const dialog = openPractice(setup);
    submitSettings(dialog, "9");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    expect(dialog.textContent).toContain("Only 3 facts available, so the run uses all of them.");
    expect(dialog.querySelector(".knowledge-practice-progress")!.textContent).toBe("Card 1 of 3");
  });

  it("reports library read failures honestly without leaking the fact", async () => {
    const actions = pagedActions([[fact(1)]], {
      loadFacts: vi.fn(() => Promise.reject(new Error("network"))),
    });
    const setup = render([[fact(1)]], actions);
    const dialog = openPractice(setup);
    submitSettings(dialog, "1");
    await vi.waitFor(() => {
      expect(dialog.textContent).toContain("Something went wrong.");
    });
    expect(prompt(dialog)).toBeNull();
  });

  it("reports failed detail reads with a retry and no opposite-side leak", async () => {
    const actions = pagedActions([[fact(1)]], {
      loadFact: vi.fn((id: string) => Promise.resolve(detail(id)))
        .mockRejectedValueOnce(new Error("denied")),
    });
    const setup = render([[fact(1)]], actions);
    const dialog = openPractice(setup);
    submitSettings(dialog, "1", "description");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    prompt(dialog).click();
    await vi.waitFor(() => {
      expect(dialog.querySelector(".error-text")!.textContent).toContain("Something went wrong.");
    });
    expect(dialog.querySelector(".knowledge-fact-title")).toBeNull();
    expect(setup.button("Refresh", dialog)).toBeDefined();
    setup.button("Refresh", dialog).click();
    await flush();
    expect(dialog.querySelector(".knowledge-practice-revealed")!.textContent)
      .toContain("Full description of fact-1");
    expect(actions.loadFact).toHaveBeenCalledTimes(2);
  });

  it("drills the current search, author and tag filter scope", async () => {
    const setup = render([[fact(1), fact(2)]], pagedActions([[fact(1), fact(2)]], {
      loadTags: vi.fn().mockResolvedValue({
        items: [{ tag_id: "tag-1", name: "history", colour: "#2e7d32" }],
      }),
    }));
    const search = setup.root.querySelector<HTMLInputElement>('[aria-label="Search facts"]')!;
    search.value = "battle";
    search.dispatchEvent(new Event("input"));
    search.dispatchEvent(new Event("change"));
    await flush();
    const author = setup.root.querySelector<HTMLInputElement>('[aria-label="Search by author"]')!;
    author.value = "ada";
    author.dispatchEvent(new Event("input"));
    author.dispatchEvent(new Event("change"));
    await flush();
    const chip = setup.root.querySelector<HTMLButtonElement>(".knowledge-tag-filter-chip")!;
    chip.click();
    await flush();

    const dialog = openPractice(setup);
    submitSettings(dialog, "1");
    await vi.waitFor(() => expect(prompt(dialog)).not.toBeNull());
    const scoped = (setup.actions.loadFacts as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    expect(scoped).toMatchObject({ search: "battle", author: "ada", tags: ["history"] });
  });
});
