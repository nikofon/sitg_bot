import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgePacketCard,
  KnowledgeThemeDetail,
  KnowledgeViewData,
} from "./knowledge";
import { renderKnowledge, type KnowledgeActions } from "./knowledge";

/**
 * Mock-backed browser-behavior tests for the packet dissection composer.
 * These exercise the dialog's user actions against stubbed API actions only;
 * they are not real LLM/DB/browser deployment acceptance.
 */

const messages: Record<string, string> = {
  "knowledge.title": "Knowledge library",
  "knowledge.tab.library": "Library",
  "knowledge.tab.packets": "Packets",
  "knowledge.tab.imports": "Pending imports",
  "knowledge.search": "Search facts",
  "knowledge.author_search": "Search by author",
  "knowledge.order.title_asc": "Title A–Z",
  "knowledge.total": "{total} facts",
  "knowledge.empty": "No facts yet.",
  "knowledge.loading": "Loading…",
  "knowledge.error": "Something went wrong.",
  "knowledge.load_more": "Load more",
  "knowledge.refresh": "Refresh",
  "knowledge.tag_filter": "Filter by tag",
  "knowledge.tags.selected": "Tags",
  "knowledge.tags.field_empty": "No tags yet — create one.",
  "knowledge.share": "Share library",
  "knowledge.packets.jobs": "Dissection jobs",
  "knowledge.packets.loading": "Loading packets…",
  "knowledge.packets.fresh": "Fresh themes: {fresh} / {total}",
  "knowledge.dissection.title": "Packet dissection",
  "knowledge.dissection.submit": "Start dissection",
  "knowledge.dissection.submit_short": "Dissect",
  "knowledge.dissection.confirm": "Confirm and start",
  "knowledge.dissection.confirm_theme": "Confirm and reveal",
  "knowledge.dissection.instructions": "Instructions (optional)",
  "knowledge.dissection.comment": "Follow-up comment (optional)",
  "knowledge.dissection.tab.general": "General",
  "knowledge.dissection.tab.themes": "Themes",
  "knowledge.dissection.selected_count": "Selected themes: {count}",
  "knowledge.dissection.themes_hint": "Select themes to dissect on the Themes tab first.",
  "knowledge.dissection.select_all": "Select all",
  "knowledge.dissection.deselect_all": "Deselect all",
  "knowledge.dissection.quota": "You can dissect {count} more themes.",
  "knowledge.dissection.quota_unlimited": "Dissection quota: unlimited.",
  "knowledge.dissection.quota_reset": "Resets at {time}.",
  "knowledge.dissection.themes_empty": "No themes available for dissection.",
  "knowledge.dissection.themes_loading": "Loading themes…",
  "knowledge.dissection.theme_author": "Author: {author}",
  "knowledge.dissection.exposure_units": "This theme contains {count} fresh questions. Revealing it will mark them as seen.",
  "knowledge.confirm_exposure": "This discloses the packet. Continue?",
  "knowledge.ai_unavailable": "AI needs a configured provider.",
  "training_history.value": "Value",
  "training_history.answer": "Answer",
  "training_history.accepted": "Accepted answers",
  "training_history.rejected": "Rejected answers",
  "training_history.commentary": "Commentary",
  "common.close": "Close",
};

const i18n = {
  t: (key: string, parameters: Record<string, unknown> = {}): string =>
    (messages[key] ?? key).replace(/\{(\w+)\}/g, (_: string, name: string): string => {
      const value = parameters[name];
      return typeof value === "string" || typeof value === "number" ? String(value) : `{${name}}`;
    }),
};

const data: KnowledgeViewData = {
  facts: {
    state: "empty", order: "title_asc", total: 0, next_cursor: null,
    items: [],
  },
  sharing: { grants: [], shared: [], links: [] },
  dissection: { packets: [], requests: [], next_cursor: null },
};

const packet: KnowledgePacketCard = {
  id: "packet-1", packet_id: "packet-1", name: "Cup 2026", year: 2026, scope: "training",
  authors: [], tournaments: [], total_theme_count: 12, fresh_theme_count: 7,
  dissected_theme_count: 0, dissected: false, best_buzz_calculated: false,
};

const themesResource = {
  version_id: "packet-1", packet_name: "Cup 2026",
  themes: [
    { theme_revision_id: "theme-1", name: "Naval history" },
    { theme_revision_id: "theme-2", name: "Ancient Rome" },
  ],
  quota: { enabled: true, remaining: 3, reset_at: "2026-11-01T00:00:00Z" },
};

const themeDetail: KnowledgeThemeDetail = {
  theme_revision_id: "theme-1", name: "Naval history", author: "Ada",
  commentary: "Focus on the age of sail",
  questions: [{
    revision_id: "q-1", value: 10, text: "What flagship fought at Trafalgar?",
    answer: "HMS Victory", accepted_answers: ["Victory"], rejected_answers: ["Victory-class"],
    commentary: "It survived the battle",
  }],
};

let controller: AbortController | undefined;
afterEach(() => { controller?.abort(); controller = undefined; });

const themeActions = (): KnowledgeActions => ({
  signal: (controller ??= new AbortController()).signal,
  loadFacts: vi.fn().mockResolvedValue(data.facts),
  loadFact: vi.fn(),
  loadTags: vi.fn().mockResolvedValue({ items: [
    { tag_id: "tag-1", name: "history", label: "History", colour: "#2e7d32" },
  ] }),
  dissect: vi.fn().mockResolvedValue({}),
  loadDissections: vi.fn().mockResolvedValue([]),
  loadPackets: vi.fn().mockResolvedValue({ items: [packet], next_cursor: null }),
  loadThemes: vi.fn().mockResolvedValue(themesResource),
  previewTheme: vi.fn(),
  loadImports: vi.fn().mockResolvedValue({ items: [] }),
});

async function openComposer(actionList: KnowledgeActions): Promise<HTMLDialogElement> {
  const root = renderKnowledge(structuredClone(data), i18n as never, actionList);
  document.body.append(root);
  const packetsTab = [...root.querySelectorAll("button")]
    .find((item) => item.textContent === "Packets")!;
  packetsTab.click();
  const dissect = await vi.waitFor(() => {
    const found = [...document.querySelectorAll("button")]
      .find((item) => item.textContent === "Dissect");
    expect(found, "Dissect button").toBeDefined();
    return found!;
  });
  dissect.click();
  const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
  return dialog;
}

const flush = async (): Promise<void> => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("dissection composer", () => {
  it("opens on General and navigates to Themes with collapsed theme rows", async () => {
    const actionList = themeActions();
    const dialog = await openComposer(actionList);
    await flush();
    const general = dialog.querySelector<HTMLElement>('[data-composer-panel="general"]')!;
    const themes = dialog.querySelector<HTMLElement>('[data-composer-panel="themes"]')!;
    expect(general.hidden).toBe(false);
    expect(themes.hidden).toBe(true);
    expect(dialog.textContent).toContain("Cup 2026");

    const themesTab = [...dialog.querySelectorAll("button")]
      .find((item) => item.textContent === "Themes")!;
    themesTab.click();
    expect(general.hidden).toBe(true);
    expect(themes.hidden).toBe(false);
    expect(actionList.loadThemes).toHaveBeenCalledWith("packet-1");
    // Collapsed rows show only the theme name with an independent checkbox.
    const row = dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')!;
    expect(row.textContent).toContain("Naval history");
    expect(row.querySelector(".knowledge-theme-body")!.hasAttribute("hidden")).toBe(true);
    expect(row.querySelector<HTMLInputElement>("input[type=checkbox]")!.checked).toBe(false);
  });

  it("keeps the dissect checkbox independent of expanding the theme", async () => {
    const actionList = { ...themeActions(),
      previewTheme: vi.fn().mockResolvedValue({ confirmation_required: false, theme: themeDetail }) };
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    const row = dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')!;
    const checkbox = row.querySelector<HTMLInputElement>("input[type=checkbox]")!;
    const toggle = row.querySelector<HTMLButtonElement>(".knowledge-theme-toggle")!;

    checkbox.click();
    expect(checkbox.checked).toBe(true);
    // Checking the box never expands the row.
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(row.querySelector<HTMLElement>(".knowledge-theme-body")!.hidden).toBe(true);

    toggle.click();
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    // Expanding never changes the selection.
    expect(checkbox.checked).toBe(true);
    toggle.click();
    expect(checkbox.checked).toBe(true);
  });

  it("selects and deselects all themes and reflects the count on General", async () => {
    const actionList = themeActions();
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    const selectAll = dialog.querySelector<HTMLButtonElement>("[data-dissect-select-all]")!;

    selectAll.click();
    const boxes = [...dialog.querySelectorAll<HTMLInputElement>(".knowledge-theme-row input[type=checkbox]")];
    expect(boxes.map((box) => box.checked)).toEqual([true, true]);
    expect(dialog.querySelector<HTMLElement>("[data-dissect-selected-count]")!.textContent)
      .toBe("Selected themes: 2");
    expect(selectAll.textContent).toBe("Deselect all");

    selectAll.click();
    expect(boxes.every((box) => !box.checked)).toBe(true);
    expect(dialog.querySelector<HTMLElement>("[data-dissect-selected-count]")!.textContent)
      .toBe("Selected themes: 0");
  });

  it("shows the remaining theme quota and its reset time", async () => {
    const actionList = themeActions();
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    const quota = dialog.querySelector<HTMLElement>(".knowledge-dissection-quota")!;
    expect(quota.hidden).toBe(false);
    expect(quota.textContent).toContain("You can dissect 3 more themes.");
    expect(quota.textContent).toContain("Resets at");
  });

  it("reveals a theme only through the explicit exposure confirmation", async () => {
    const previewTheme = vi.fn()
      .mockResolvedValueOnce({ confirmation_required: true, fresh_unit_count: 2 })
      .mockResolvedValueOnce({ confirmation_required: false, theme: themeDetail });
    const actionList = { ...themeActions(), previewTheme };
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    const row = dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')!;
    row.querySelector<HTMLButtonElement>(".knowledge-theme-toggle")!.click();
    await flush();

    // The first preview asks for confirmation and discloses no content.
    expect(previewTheme).toHaveBeenCalledWith("packet-1", "theme-1", false);
    const body = row.querySelector<HTMLElement>(".knowledge-theme-body")!;
    expect(body.textContent).toContain("This theme contains 2 fresh questions.");
    expect(body.textContent).not.toContain("Trafalgar");
    expect(body.textContent).not.toContain("HMS Victory");

    const confirm = [...body.querySelectorAll("button")]
      .find((item) => item.textContent === "Confirm and reveal")!;
    confirm.click();
    await flush();
    expect(previewTheme).toHaveBeenLastCalledWith("packet-1", "theme-1", true);
    // The confirmed detail shows the author, commentary and every question field.
    expect(body.textContent).toContain("Author: Ada");
    expect(body.textContent).toContain("Focus on the age of sail");
    expect(body.textContent).toContain("What flagship fought at Trafalgar?");
    expect(body.textContent).toContain("HMS Victory");
    expect(body.textContent).toContain("Accepted answers: Victory");
    expect(body.textContent).toContain("Rejected answers: Victory-class");
    expect(body.textContent).toContain("It survived the battle");
  });

  it("caches the revealed theme and reports preview failures", async () => {
    const previewTheme = vi.fn()
      .mockRejectedValueOnce(new Error("forbidden"))
      .mockResolvedValue({ confirmation_required: false, theme: themeDetail });
    const actionList = { ...themeActions(), previewTheme };
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    const toggle = dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')!
      .querySelector<HTMLButtonElement>(".knowledge-theme-toggle")!;
    toggle.click();
    await flush();
    expect(dialog.querySelector('[data-theme-id="theme-1"] .error-text')!.textContent)
      .toBe("Something went wrong.");

    // A failed preview can be retried and then caches without a second call.
    toggle.click();
    toggle.click();
    await flush();
    expect(previewTheme).toHaveBeenCalledTimes(2);
    toggle.click();
    await flush();
    expect(previewTheme).toHaveBeenCalledTimes(2);
  });

  it("submits selected themes, auto-assign tags and commentary verbatim", async () => {
    const dissect = vi.fn().mockResolvedValue({});
    const actionList = { ...themeActions(), dissect };
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')
      ?.querySelector<HTMLInputElement>("input[type=checkbox]")!.click();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "General")!.click();
    await flush();

    const comment = [...dialog.querySelectorAll("textarea")]
      .find((node) => node.getAttribute("aria-label") === "Follow-up comment (optional)")!;
    comment.value = "Extract naval terms";
    // The tag chip is offered from the user's pool and sent as a tag id.
    const tagBox = [...dialog.querySelectorAll<HTMLInputElement>(".knowledge-tag-option input")][0]!;
    tagBox.click();
    const start = [...dialog.querySelectorAll("button")]
      .find((item) => item.textContent === "Start dissection")!;
    start.click();
    await flush();

    expect(dissect).toHaveBeenCalledWith("packet-1", {
      instructions: undefined,
      comment: "Extract naval terms",
      theme_revision_ids: ["theme-1"],
      tag_ids: ["tag-1"],
      confirm_exposure: false,
    });
    expect(document.contains(dialog)).toBe(false);
  });

  it("requires at least one selected theme before starting", async () => {
    const dissect = vi.fn();
    const actionList = { ...themeActions(), dissect };
    const dialog = await openComposer(actionList);
    await flush();
    const start = [...dialog.querySelectorAll("button")]
      .find((item) => item.textContent === "Start dissection")!;
    expect(start.disabled).toBe(true);
    const hint = [...dialog.querySelectorAll("p")]
      .find((node) => node.textContent === "Select themes to dissect on the Themes tab first.")!;
    expect(hint.hidden).toBe(false);
    start.click();
    expect(dissect).not.toHaveBeenCalled();
  });

  it("stays closable while a started dissection is still processing", async () => {
    let release: (() => void) | undefined;
    const dissect = vi.fn().mockImplementation(() => new Promise<{}>((resolve) => {
      release = () => resolve({});
    }));
    const cancel = vi.fn();
    const actionList = { ...themeActions(), dissect, cancel };
    const dialog = await openComposer(actionList);
    await flush();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Themes")!.click();
    dialog.querySelector<HTMLElement>('[data-theme-id="theme-1"]')
      ?.querySelector<HTMLInputElement>("input[type=checkbox]")!.click();
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "General")!.click();
    [...dialog.querySelectorAll("button")]
      .find((item) => item.textContent === "Start dissection")!.click();
    await flush();

    // The durable job keeps running; the window closes without cancelling it.
    const close = dialog.querySelector<HTMLButtonElement>(".knowledge-dialog-close")!;
    close.click();
    expect(document.contains(dialog)).toBe(false);
    expect(cancel).not.toHaveBeenCalled();
    release?.();
    await flush();
  });
});
