import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeFactDetail,
  KnowledgeFactsResource,
  KnowledgeImportResult,
  KnowledgePacketCard,
  KnowledgeSharing,
  KnowledgeTagPool,
  KnowledgeTagPoolEntry,
  KnowledgeViewData,
  ShareLink,
} from "./knowledge";
import { renderKnowledge, type KnowledgeActions } from "./knowledge";

const messages: Record<string, string> = {
  "knowledge.title": "Knowledge library",
  "knowledge.tab.library": "Library",
  "knowledge.tab.packets": "Packets",
  "knowledge.search": "Search facts",
  "knowledge.order.title_asc": "Title A–Z",
  "knowledge.order.title_desc": "Title Z–A",
  "knowledge.order.updated_desc": "Recently updated",
  "knowledge.total": "{total} facts",
  "knowledge.empty": "No facts yet.",
  "knowledge.loading": "Loading…",
  "knowledge.error": "Something went wrong.",
  "knowledge.quota": "Quota exhausted.",
  "knowledge.load_more": "Load more",
  "knowledge.refresh": "Refresh",
  "knowledge.fact.tags": "Tags: {tags}",
  "knowledge.fact.links": "No source links.",
  "knowledge.fact.origin.manual": "Manual",
  "knowledge.fact.origin.dissection": "Dissection",
  "knowledge.fact.read_only": "Read-only shared fact",
  "knowledge.fact.version": "version {version}",
  "knowledge.fact.edit": "Edit",
  "knowledge.statistic.none": "No statistics",
  "knowledge.statistic.pending": "Pending",
  "knowledge.statistic.fresh": "Current",
  "knowledge.statistic.stale": "Stale",
  "knowledge.statistic.unavailable": "Unavailable",
  "knowledge.statistic.denied": "Denied",
  "knowledge.statistic.failed": "Failed",
  "knowledge.statistic.pending_calibration": "Pending calibration",
  "knowledge.statistic.mentions": "Mentions: {count}",
  "knowledge.statistic.mentions_partial": "Mentions: at least {count}",
  "knowledge.statistic.common": "Common knowledge: {value}%",
  "knowledge.statistic.bingo": "Bingo",
  "knowledge.create": "Add fact",
  "knowledge.save": "Save",
  "knowledge.delete": "Delete fact",
  "knowledge.title_field": "Title",
  "knowledge.description": "Description",
  "knowledge.links_field": "Sources (URL | title, one per line)",
  "knowledge.confirm_delete": "Delete this fact?",
  "knowledge.tag_filter": "Filter by tag",
  "knowledge.tag_filter_all": "All tags",
  "knowledge.merge": "Merge into another fact",
  "knowledge.merge_target": "Target fact ID",
  "knowledge.tag_rename": "Rename tag",
  "knowledge.tag_delete": "Delete tag",
  "knowledge.share": "Share library",
  "knowledge.sharing.title": "Sharing",
  "knowledge.sharing.empty": "No copies sent yet.",
  "knowledge.sharing.empty_links": "No active links yet.",
  "knowledge.sharing.permanent": "Permanent, independent copies",
  "knowledge.sharing.received": "Received copies",
  "knowledge.sharing.count": "{count} facts copied",
  "knowledge.sharing.recipient": "Recipient name",
  "knowledge.sharing.grant": "Send copies of my collection",
  "knowledge.sharing.revoke": "Revoke",
  "knowledge.sharing.revoked": "Revoked",
  "knowledge.sharing.link_hint": "Anyone with an active link can import.",
  "knowledge.sharing.create_link": "Create link",
  "knowledge.sharing.links": "Active links",
  "knowledge.sharing.links_inactive": "Deactivated links",
  "knowledge.sharing.link_url": "Import link",
  "knowledge.sharing.copy": "Copy",
  "knowledge.sharing.copied": "Copied",
  "knowledge.sharing.deactivate": "Deactivate",
  "knowledge.sharing.deactivated": "Deactivated",
  "knowledge.sharing.confirm_deactivate": "Deactivate this link?",
  "knowledge.sharing.direct": "Send copies directly",
  "knowledge.tags.new": "New tag",
  "knowledge.tags.create": "Create tag",
  "knowledge.tags.name": "Tag name",
  "knowledge.tags.colour": "Colour",
  "knowledge.tags.selected": "Tags",
  "knowledge.tags.field_empty": "No tags yet — create one.",
  "knowledge.import.title": "Import shared library",
  "knowledge.import.body": "Import a copy of the shared facts?",
  "knowledge.import.confirm": "Import",
  "knowledge.import.dismiss": "Not now",
  "knowledge.import.done": "Imported {imported} facts, skipped {skipped} already-known facts.",
  "knowledge.packets.search": "Search by packet name",
  "knowledge.packets.reset": "Reset filters",
  "knowledge.packets.empty": "No packets available for reading yet.",
  "knowledge.packets.no_matches": "No packets match these filters.",
  "knowledge.packets.loading": "Loading packets…",
  "knowledge.packets.fresh": "Fresh themes: {fresh} / {total}",
  "knowledge.packets.fresh_label": "Fresh themes",
  "knowledge.packets.dissected": "Dissected themes: {count}",
  "knowledge.packets.dissected_label": "Dissected themes",
  "knowledge.packets.dissected_unknown": "unknown",
  "knowledge.packets.dissected_complete": "Dissected",
  "knowledge.packets.scope.training": "Training",
  "knowledge.packets.scope.tournament": "Tournament",
  "knowledge.packets.jobs": "Dissection jobs",
  "knowledge.dissection.title": "Packet dissection",
  "knowledge.dissection.packet": "Packet",
  "knowledge.dissection.instructions": "Instructions (optional)",
  "knowledge.dissection.submit": "Start dissection",
  "knowledge.dissection.submit_short": "Dissect",
  "knowledge.dissection.confirm": "Confirm and start",
  "knowledge.dissection.empty": "No dissections yet.",
  "knowledge.dissection.comment": "Follow-up comment (optional)",
  "knowledge.dissection.status.pending": "Pending",
  "knowledge.dissection.status.succeeded": "Succeeded",
  "knowledge.dissection.status.partially_succeeded": "Partial",
  "knowledge.dissection.status.failed": "Failed",
  "knowledge.dissection.partial": "Some candidates failed.",
  "knowledge.dissection.base": "Base",
  "knowledge.cancel": "Cancel job",
  "knowledge.confirm_exposure": "This discloses the packet. Continue?",
  "common.close": "Close",
  "common.loading": "Loading…",
  "lobby.packet_sort": "Sort packets",
  "lobby.packet_sort_default": "Default order",
  "lobby.packet_sort_year_asc": "Year · oldest first",
  "lobby.packet_sort_year_desc": "Year · newest first",
  "lobby.packet_authors": "Authors",
  "lobby.packet_tournaments": "Tournaments",
  "lobby.packet_from": "From year",
  "lobby.packet_to": "To year",
  "lobby.packet_year": "Packet year",
  "lobby.packet_publication_year": "Publication year",
  "filters.name_asc": "Name · A–Z",
  "filters.name_desc": "Name · Z–A",
};

const i18n = {
  t: (key: string, parameters: Record<string, unknown> = {}): string =>
    (messages[key] ?? key).replace(/\{(\w+)\}/g, (_: string, name: string): string => {
      const value = parameters[name];
      return typeof value === "string" || typeof value === "number" ? String(value) : `{${name}}`;
    }),
};

const facts: KnowledgeFactsResource = {
  state: "ready",
  order: "title_asc",
  total: 2,
  next_cursor: "cursor-2",
  items: [
    {
      id: "fact-1",
      title: "Battle of Austerlitz",
      tag_names: ["napoleonic wars"],
      origin: "manual",
      updated_at: "2026-10-01",
      statistics: {
        status: "stale",
        mentions: { count: 5, complete: false },
        common_knowledge: 30,
        bingo: { status: "pending_calibration", value: null },
      },
    },
    {
      id: "fact-2",
      title: "Rosetta Stone",
      tag_names: [],
      origin: "dissection",
      updated_at: "2026-10-02",
      statistics: { status: "denied" },
    },
  ],
};

const tagPool: KnowledgeTagPool = {
  items: [
    { tag_id: "tag-1", name: "napoleonic wars", colour: "#1565c0" },
    { tag_id: "tag-2", name: "history", colour: "#2e7d32" },
  ],
};

const sharing: KnowledgeSharing = {
  grants: [
    { id: "grant-1", recipient_name: "Ada", copied_count: 3 },
  ],
  shared: [{ owner_id: "owner-1", owner_name: "Grace", copied_count: 2 }],
  links: [],
};

const data: KnowledgeViewData = {
  facts,
  sharing,
  dissection: {
    packets: [{ id: "packet-1", name: "Cup 2026" }],
    requests: [
      {
        id: "request-1",
        packet_id: "packet-1",
        status: "partially_succeeded",
        base: true,
        applied_count: 3,
        partial: true,
      },
    ],
  },
};

const packet = (id: string, name: string): KnowledgePacketCard => ({
  id, packet_id: id, name, year: 2026, scope: "training",
  authors: [{ id: "author-1", name: "Author" }],
  tournaments: [], total_theme_count: 12, fresh_theme_count: 7,
  dissected_theme_count: 4, dissected: true, best_buzz_calculated: false,
});

function actions(overrides: Partial<KnowledgeActions> = {}): KnowledgeActions {
  return {
    loadFacts: vi.fn().mockResolvedValue(facts),
    loadFact: vi.fn().mockResolvedValue({
      id: "fact-1", title: "Battle of Austerlitz", description: "detail",
      links: [], tags: [], origin: "manual", content_version: 1,
      editable: false, statistics: { status: "none" },
    }),
    loadTags: vi.fn().mockResolvedValue(tagPool),
    createTag: vi.fn(),
    loadSharing: vi.fn().mockResolvedValue(sharing),
    grant: vi.fn().mockResolvedValue(undefined),
    createLink: vi.fn(),
    deactivateLink: vi.fn().mockResolvedValue(undefined),
    importLibrary: vi.fn(),
    consumeImportToken: vi.fn(),
    resolveShareUrl: (url: string) => `https://mini.example.test${url}`,
    dissect: vi.fn().mockResolvedValue({}),
    loadDissections: vi.fn().mockResolvedValue(data.dissection.requests),
    cancel: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function render(view: KnowledgeViewData = structuredClone(data), actionList: KnowledgeActions = actions()) {
  const root = renderKnowledge(view, i18n as never, actionList);
  document.body.append(root);
  const button = (text: string, scope: ParentNode = document) =>
    [...scope.querySelectorAll("button")].find((item) => item.textContent === text);
  const field = <T extends HTMLElement>(text: string, scope: ParentNode = document): T =>
    scope.querySelector<T>(`[aria-label='${text}']`)!;
  return { root, actions: actionList, button, field };
}

const flush = async (): Promise<void> => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("knowledge tabs", () => {
  it("starts on the Library tab and switches to Packets and back", () => {
    const { root, button } = render();
    const library = root.querySelector<HTMLElement>('[data-panel="library"]')!;
    const packets = root.querySelector<HTMLElement>('[data-panel="packets"]')!;
    expect(library.hidden).toBe(false);
    expect(packets.hidden).toBe(true);
    expect(root.querySelector(".knowledge-tabs")?.textContent).toContain("Library");
    expect(root.querySelector(".knowledge-tabs")?.textContent).toContain("Packets");

    button("Packets")!.click();
    expect(library.hidden).toBe(true);
    expect(packets.hidden).toBe(false);

    button("Library")!.click();
    expect(library.hidden).toBe(false);
    expect(packets.hidden).toBe(true);
  });

  it("preserves packet filters and loaded cards across tab switches", async () => {
    vi.useFakeTimers();
    try {
      const loadPackets = vi.fn().mockResolvedValue({
        items: [packet("packet-1", "Cup 2026"), packet("packet-2", "School 2025")],
        next_cursor: null,
      });
      const controller = new AbortController();
      const { root, button, field } = render(structuredClone(data),
        actions({ loadPackets, signal: controller.signal }));
      button("Packets")!.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(loadPackets).toHaveBeenCalledTimes(1);
      expect(loadPackets.mock.calls[0]![0].get("limit")).toBe("25");
      expect(root.querySelector('[data-panel="packets"]')!.textContent).toContain("Cup 2026");

      field<HTMLInputElement>("Search by packet name").value = "cup";
      field<HTMLInputElement>("Search by packet name").dispatchEvent(new Event("input", { bubbles: true }));
      await vi.advanceTimersByTimeAsync(250);
      expect(loadPackets).toHaveBeenCalledTimes(2);
      expect(loadPackets.mock.calls[1]![0].get("name")).toBe("cup");

      button("Library")!.click();
      button("Packets")!.click();
      expect(field<HTMLInputElement>("Search by packet name").value).toBe("cup");
      await vi.advanceTimersByTimeAsync(0);
      // The card cache survives the tab switch without a refetch.
      expect(root.querySelector('[data-panel="packets"]')!.textContent).toContain("Cup 2026");
      expect(loadPackets).toHaveBeenCalledTimes(3);
      controller.abort();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not fetch packets while the Packets tab stays inactive", () => {
    const loadPackets = vi.fn();
    const controller = new AbortController();
    try {
      render(structuredClone(data), actions({ loadPackets, signal: controller.signal }));
      expect(loadPackets).not.toHaveBeenCalled();
    } finally {
      controller.abort();
    }
  });
});

describe("collapsed fact cards", () => {
  it("shows only fact names until a card is expanded", async () => {
    const detail: KnowledgeFactDetail = {
      id: "fact-1", title: "Battle of Austerlitz", description: "1805 battle",
      links: [{ url: "https://example.test/a", title: "Source" }],
      tags: [{ id: "tag-1", name: "napoleonic wars", colour: "#1565c0" }],
      origin: "dissection", content_version: 2, editable: false,
      statistics: { status: "pending" },
    };
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(detail) });
    const { root } = render(structuredClone(data), actionList);
    const list = root.querySelector(".knowledge-list")!;
    expect(list.textContent).toContain("Battle of Austerlitz");
    expect(list.textContent).toContain("Rosetta Stone");
    expect(list.textContent).not.toContain("Stale");
    expect(list.textContent).not.toContain("Tags:");

    const header = root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!;
    header.click();
    expect(header.getAttribute("aria-expanded")).toBe("true");
    await flush();
    expect(actionList.loadFact).toHaveBeenCalledWith("fact-1");
    const card = root.querySelector('[data-fact-id="fact-1"]')!;
    expect(card.textContent).toContain("1805 battle");
    expect(card.textContent).toContain("Pending");
    expect(card.textContent).toContain("Read-only shared fact");
    expect(card.querySelector("a[href='https://example.test/a']")).not.toBeNull();

    header.click();
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(card.querySelector<HTMLElement>(".knowledge-fact-body")!.hidden).toBe(true);
  });

  it("keeps collapsed cards unexpanded after a list reload", async () => {
    const { root, field } = render();
    const header = document.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!;
    header.click();
    await flush();
    const search = field<HTMLInputElement>("Search facts");
    search.value = "austerlitz";
    search.dispatchEvent(new Event("input"));
    search.dispatchEvent(new Event("change"));
    await flush();
    const fresh = root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!;
    expect(fresh.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("tags with colours", () => {
  it("loads the tag pool once and draws coloured chips and a combined card outline", async () => {
    const detail: KnowledgeFactDetail = {
      id: "fact-1", title: "Battle of Austerlitz", description: "d",
      links: [], tags: [
        { id: "tag-1", name: "napoleonic wars", colour: "#1565c0" },
        { id: "tag-2", name: "history", colour: "#2e7d32" },
      ],
      origin: "manual", content_version: 1, editable: false, statistics: { status: "none" },
    };
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(detail) });
    const view: KnowledgeViewData = structuredClone(data);
    view.facts.items[0]!.tag_names = ["napoleonic wars", "history"];
    const { root, actions: mocked } = render(view, actionList);
    await flush();
    expect(mocked.loadTags).toHaveBeenCalledTimes(1);
    const card = root.querySelector<HTMLElement>('[data-fact-id="fact-1"]')!;
    expect(card.classList.contains("knowledge-fact-tagged")).toBe(true);
    expect(card.style.getPropertyValue("--knowledge-tag-border"))
      .toContain("linear-gradient(120deg, #1565c0, #2e7d32)");

    card.querySelector<HTMLButtonElement>(".knowledge-fact-header")!.click();
    await flush();
    const chips = card.querySelectorAll<HTMLElement>(".knowledge-tag-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0]!.style.color).toContain("21, 101, 192");
    expect(chips[1]!.style.color).toContain("46, 125, 50");
  });

  it("offers pool tags as checkboxes, not free text, when editing a fact", async () => {
    const detail: KnowledgeFactDetail = {
      id: "fact-1", title: "Battle of Austerlitz", description: "d",
      links: [], tags: [{ id: "tag-1", name: "napoleonic wars", colour: "#1565c0" }],
      origin: "manual", content_version: 1, editable: true, statistics: { status: "none" },
    };
    const actionList = actions({
      loadFact: vi.fn().mockResolvedValue(detail),
      save: vi.fn().mockResolvedValue(detail),
    });
    const { button } = render(structuredClone(data), actionList);
    await flush();
    document.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    button("Edit")!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    const checkboxes = [...dialog.querySelectorAll<HTMLInputElement>(".knowledge-tag-option input")];
    expect(checkboxes.map((box) => box.value)).toEqual(["history", "napoleonic wars"]);
    expect(checkboxes.find((box) => box.value === "napoleonic wars")!.checked).toBe(true);

    checkboxes.find((box) => box.value === "history")!.click();
    const form = dialog.querySelector("form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(actionList.save).toHaveBeenCalledWith(expect.objectContaining({
      tag_names: ["napoleonic wars", "history"],
    }), detail);
  });

  it("creates a tag with name and colour through the New tag dialog", async () => {
    const created: KnowledgeTagPoolEntry = { tag_id: "tag-3", name: "art", colour: "#ef6c00" };
    const detail: KnowledgeFactDetail = {
      id: "fact-1", title: "t", description: "d", links: [], tags: [],
      origin: "manual", content_version: 1, editable: true, statistics: { status: "none" },
    };
    const actionList = actions({
      createTag: vi.fn().mockResolvedValue(created),
      loadFact: vi.fn().mockResolvedValue(detail),
      save: vi.fn().mockResolvedValue(detail),
    });
    const { root, button } = render(structuredClone(data), actionList);
    await flush();
    expect(root.querySelector(".knowledge-tag-select")).toBeNull(); // New tag button lives beside the selector in fact editing.
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    button("Edit")!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("New tag", dialog)!.click();
    const tagDialog = [...document.querySelectorAll<HTMLDialogElement>(".knowledge-dialog")]
      .find((item) => item.querySelector("[aria-label='Tag name']"))!;
    const name = tagDialog.querySelector<HTMLInputElement>("[aria-label='Tag name']")!;
    const colour = tagDialog.querySelector<HTMLInputElement>("[aria-label='Colour']")!;
    name.value = "art";
    colour.value = "#ef6c00";
    tagDialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(actionList.createTag).toHaveBeenCalledWith("art", "#ef6c00");
    const options = [...dialog.querySelectorAll<HTMLInputElement>(".knowledge-tag-option input")];
    const art = options.find((box) => box.value === "art");
    expect(art).toBeDefined();
    expect(art!.checked).toBe(true);
  });

  it("filters the fact list by a tag chosen from the pool", async () => {
    const reloaded: KnowledgeFactsResource = { ...facts, items: [facts.items[0]!], total: 1, next_cursor: null };
    const actionList = actions({ loadFacts: vi.fn().mockResolvedValue(reloaded) });
    const { field } = render(structuredClone(data), actionList);
    await flush();
    const select = field<HTMLSelectElement>("Filter by tag");
    expect([...select.options].map((option) => option.value)).toEqual(
      ["", "history", "napoleonic wars"],
    );
    select.value = "napoleonic wars";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(actionList.loadFacts).toHaveBeenCalledWith({
      search: "", order: "title_asc", tag: "napoleonic wars",
    });
  });
});

describe("sharing links", () => {
  const link = (id: string, active = true): ShareLink => ({
    id, token: `token-${id}`, url: `/knowledge#import=token-${id}`,
    created_at: "2026-10-01T00:00:00Z", deactivated_at: active ? null : "2026-10-02T00:00:00Z", active,
  });

  it("opens a Share library dialog, creates a link and shows the resolved URL", async () => {
    const withLinks: KnowledgeSharing = { ...sharing, links: [link("link-1")] };
    const actionList = actions({
      loadSharing: vi.fn().mockResolvedValue(withLinks),
      createLink: vi.fn().mockResolvedValue(link("link-2")),
    });
    const { button } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Anyone with an active link can import.");
    expect(actionList.loadSharing).toHaveBeenCalledTimes(1);
    const url = dialog.querySelector<HTMLInputElement>("[aria-label='Import link']")!;
    expect(url.value).toBe("https://mini.example.test/knowledge#import=token-link-1");

    button("Create link", dialog)!.click();
    await flush();
    expect(actionList.createLink).toHaveBeenCalled();
    expect(actionList.loadSharing).toHaveBeenCalledTimes(2);
  });

  it("deactivates an active link after confirmation and lists it as deactivated", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const withLinks: KnowledgeSharing = { ...sharing, links: [link("link-1")] };
    const afterDeactivate: KnowledgeSharing = { ...sharing, links: [link("link-1", false)] };
    const actionList = actions({
      loadSharing: vi.fn()
        .mockResolvedValueOnce(withLinks)
        .mockResolvedValueOnce(afterDeactivate),
    });
    const { button } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("Deactivate", dialog)!.click();
    await flush();
    expect(actionList.deactivateLink).toHaveBeenCalledWith("link-1");
    expect(dialog.textContent).toContain("Deactivated links");
    expect([...dialog.querySelectorAll("button")].some((item) => item.textContent === "Deactivate")).toBe(false);
  });

  it("copies the import URL to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const withLinks: KnowledgeSharing = { ...sharing, links: [link("link-1")] };
    const actionList = actions({ loadSharing: vi.fn().mockResolvedValue(withLinks) });
    const { button } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("Copy", dialog)!.click();
    await flush();
    expect(writeText).toHaveBeenCalledWith("https://mini.example.test/knowledge#import=token-link-1");
    expect(button("Copied", dialog)).toBeDefined();
  });

  it("keeps direct permanent-copy grants inside the sharing dialog", async () => {
    const actionList = actions();
    const { button, field } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Send copies directly");
    expect(dialog.textContent).toContain("Ada");
    expect(dialog.textContent).toContain("Grace");
    field<HTMLInputElement>("Recipient name", dialog).value = "Carol";
    button("Send copies of my collection", dialog)!.click();
    await flush();
    expect(actionList.grant).toHaveBeenCalledWith("Carol");
  });
});

describe("importing a shared library", () => {
  it("asks for confirmation when an import token is present and reports the result", async () => {
    const result: KnowledgeImportResult = {
      owner_id: "owner-1", recipient_id: "me", imported_count: 5, skipped_count: 2,
    };
    const actionList = actions({ importLibrary: vi.fn().mockResolvedValue(result) });
    const view: KnowledgeViewData = { ...structuredClone(data), import_token: "token-1" };
    render(view, actionList);
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Import shared library");
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Import")!.click();
    await flush();
    expect(actionList.importLibrary).toHaveBeenCalledWith("token-1");
    expect(actionList.consumeImportToken).toHaveBeenCalled();
    expect(dialog.textContent).toContain("Imported 5 facts, skipped 2 already-known facts.");
  });

  it("dismisses the import offer without importing", () => {
    const actionList = actions();
    const view: KnowledgeViewData = { ...structuredClone(data), import_token: "token-1" };
    render(view, actionList);
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Not now")!.click();
    expect(actionList.importLibrary).not.toHaveBeenCalled();
    expect(actionList.consumeImportToken).toHaveBeenCalled();
    expect(document.querySelector(".knowledge-dialog")).toBeNull();
  });
});

describe("packets tab", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  function renderPackets(actionList: KnowledgeActions, controller: AbortController) {
    const rendered = render(structuredClone(data), { ...actionList, signal: controller.signal });
    const packetsButton = [...rendered.root.querySelectorAll("button")]
      .find((item) => item.textContent === "Packets")!;
    packetsButton.click();
    return rendered;
  }

  it("loads readable packets in batches and shows dissection progress", async () => {
    const controller = new AbortController();
    try {
      const loadPackets = vi.fn()
        .mockResolvedValueOnce({ items: [packet("packet-1", "Cup 2026")], next_cursor: "cursor-2" })
        .mockResolvedValueOnce({ items: [packet("packet-2", "School 2025")], next_cursor: null });
      const { root } = renderPackets(actions({ loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(0);
      const panel = root.querySelector('[data-panel="packets"]')!;
      expect(panel.textContent).toContain("Cup 2026");
      expect(panel.textContent).toContain("Dissected themes: 4");
      expect(panel.textContent).toContain("Fresh themes");
      expect(panel.textContent).toContain("7 / 12");
      expect(panel.textContent).toContain("Dissected");
      await vi.advanceTimersByTimeAsync(250);
      expect(loadPackets).toHaveBeenCalledTimes(2);
      expect(loadPackets.mock.calls[1]![0].get("cursor")).toBe("cursor-2");
      expect(panel.textContent).toContain("School 2025");
      expect(panel.textContent).not.toContain("No packets available");
    } finally {
      controller.abort();
    }
  });

  it("shows the empty state and keeps unknown dissection coverage distinct from zero", async () => {
    const controller = new AbortController();
    try {
      const unknown = { ...packet("packet-1", "Only packet"), dissected_theme_count: null, dissected: false };
      const loadPackets = vi.fn().mockResolvedValue({ items: [unknown], next_cursor: null });
      const { root } = renderPackets(actions({ loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(0);
      const panel = root.querySelector('[data-panel="packets"]')!;
      expect(panel.textContent).toContain("unknown");
      expect(panel.textContent).not.toContain("Dissected themes: 0");
    } finally {
      controller.abort();
    }
  });

  it("sends filters and order to the packet catalogue", async () => {
    const controller = new AbortController();
    try {
      const loadPackets = vi.fn().mockResolvedValue({ items: [], next_cursor: null });
      const { root, field } = renderPackets(actions({ loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(0);
      field<HTMLInputElement>("Search by packet name").value = "cup";
      field<HTMLInputElement>("Search by packet name").dispatchEvent(new Event("input", { bubbles: true }));
      field<HTMLInputElement>("Authors").value = "ann";
      field<HTMLInputElement>("Authors").dispatchEvent(new Event("input", { bubbles: true }));
      const sort = field<HTMLSelectElement>("Sort packets");
      sort.value = "year_desc";
      sort.dispatchEvent(new Event("change", { bubbles: true }));
      await vi.advanceTimersByTimeAsync(250);
      const query = loadPackets.mock.calls.at(-1)![0] as URLSearchParams;
      expect(query.get("name")).toBe("cup");
      expect(query.get("author")).toBe("ann");
      expect(query.get("order")).toBe("year_desc");
      expect(root.querySelector('[data-panel="packets"]')!.textContent)
        .toContain("No packets match these filters.");
    } finally {
      controller.abort();
    }
  });

  it("runs the two-step exposure confirmation from the Dissect dialog", async () => {
    const controller = new AbortController();
    try {
      const dissect = vi.fn()
        .mockResolvedValueOnce({ confirmation_required: true })
        .mockResolvedValueOnce({});
      const loadPackets = vi.fn()
        .mockResolvedValue({ items: [packet("packet-1", "Cup 2026")], next_cursor: null });
      const { field, button } = renderPackets(actions({ dissect, loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(0);
      button("Dissect")!.click();
      const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
      expect(dialog.textContent).toContain("Cup 2026");
      field<HTMLTextAreaElement>("Instructions (optional)", dialog).value = "Focus on naval history";
      button("Start dissection", dialog)!.click();
      await flush();
      expect(dissect).toHaveBeenCalledWith("packet-1", "Focus on naval history", undefined, false);
      expect(dialog.textContent).toContain("This discloses the packet.");
      button("Confirm and start", dialog)!.click();
      await flush();
      expect(dissect).toHaveBeenLastCalledWith("packet-1", "Focus on naval history", undefined, true);
      expect(document.contains(dialog)).toBe(false);
    } finally {
      controller.abort();
    }
  });

  it("lists dissection jobs with statuses without extra per-card requests", async () => {
    const controller = new AbortController();
    try {
      const loadPackets = vi.fn().mockResolvedValue({ items: [packet("packet-1", "Cup 2026")], next_cursor: null });
      const { root } = renderPackets(actions({ loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(300);
      const jobs = root.querySelector(".knowledge-jobs")!;
      expect(jobs.textContent).toContain("Cup 2026");
      expect(jobs.textContent).toContain("Partial");
      expect(jobs.textContent).toContain("Some candidates failed.");
      expect(loadPackets).toHaveBeenCalledTimes(1);
    } finally {
      controller.abort();
    }
  });
});
