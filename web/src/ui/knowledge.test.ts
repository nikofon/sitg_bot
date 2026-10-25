import stylesheet from "../styles.css?raw";
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
  "knowledge.fact.origin.packet": "From packet: {name}",
  "knowledge.fact.read_only": "Read-only shared fact",
  "knowledge.fact.edit": "Edit",
  "knowledge.fact.edit_label": "Edit fact: {title}",
  "knowledge.fact.delete_label": "Delete fact: {title}",
  "knowledge.images_field": "Images",
  "knowledge.images.remove": "Remove image",
  "knowledge.images.too_large": "Image is larger than 2 MiB.",
  "knowledge.images.unsupported": "Only PNG, JPEG or WebP images are supported.",
  "knowledge.images.too_many": "A fact can have at most 8 images.",
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
  "knowledge.tag_rename": "Rename tag",
  "knowledge.tag_delete": "Delete tag",
  "knowledge.share": "Share library",
  "knowledge.sharing.title": "Sharing",
  "knowledge.sharing.empty": "No copies sent yet.",
  "knowledge.sharing.empty_links": "No active links yet.",
  "knowledge.sharing.launch_unavailable": "Sharing links are unavailable right now: the bot launch identity is not configured. Try again later.",
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
  "knowledge.packets.owner_only": "Only my uploads",
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
    const style = document.createElement("style");
    style.textContent = stylesheet;
    document.body.append(style);
    const { root, button } = render();
    const library = root.querySelector<HTMLElement>('[data-panel="library"]')!;
    const packets = root.querySelector<HTMLElement>('[data-panel="packets"]')!;
    expect(library.hidden).toBe(false);
    expect(packets.hidden).toBe(true);
    // The active tab is highlighted on the very first render, not only after a click.
    const tabs = [...root.querySelectorAll<HTMLButtonElement>(".knowledge-tab")];
    const active = tabs.find((tab) => tab.getAttribute("aria-current") === "page");
    expect(active?.textContent).toBe("Library");
    expect(active?.className).toContain("primary-button");
    expect(getComputedStyle(library).display).toBe("grid");
    expect(getComputedStyle(packets).display).toBe("none");
    expect(root.querySelector(".knowledge-tabs")?.textContent).toContain("Library");
    expect(root.querySelector(".knowledge-tabs")?.textContent).toContain("Packets");

    button("Packets")!.click();
    expect(library.hidden).toBe(true);
    expect(packets.hidden).toBe(false);
    expect(getComputedStyle(library).display).toBe("none");
    expect(getComputedStyle(packets).display).toBe("grid");

    button("Library")!.click();
    expect(library.hidden).toBe(false);
    expect(packets.hidden).toBe(true);
    expect(getComputedStyle(library).display).toBe("grid");
    expect(getComputedStyle(packets).display).toBe("none");
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

  it("shows packet provenance and attached images without version or manual-origin notes", async () => {
    const detail: KnowledgeFactDetail = {
      id: "fact-1", title: "Rosetta Stone", description: "d",
      links: [], tags: [], origin: "dissection", packet_name: "Cup 2026",
      content_version: 7, editable: false, statistics: { status: "none" },
      images: [{ id: "image-1", url: "/api/miniapp/knowledge/images/image-1", content_type: "image/png" }],
    };
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(detail) });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    const card = root.querySelector('[data-fact-id="fact-1"]')!;
    expect(card.textContent).toContain("From packet: Cup 2026");
    expect(card.querySelector(".knowledge-fact-images img")?.getAttribute("src"))
      .toBe("/api/miniapp/knowledge/images/image-1");
    // No fact version, no manual/dissection origin words, no card-level tag tools.
    expect(card.textContent).not.toContain("version");
    expect(card.textContent).not.toContain("Manual");
    expect(card.textContent).not.toContain("Dissection");
    expect(card.querySelector(".knowledge-tag-tool")).toBeNull();
    // Read-only facts never offer the pencil editor control.
    expect(card.querySelector<HTMLElement>(".knowledge-fact-edit")!.hidden).toBe(true);
  });
});

describe("fact card controls", () => {
  const editableDetail: KnowledgeFactDetail = {
    id: "fact-1", title: "Battle of Austerlitz", description: "d",
    links: [], tags: [], origin: "manual", content_version: 1, editable: true,
    statistics: { status: "none" },
  };

  it("aligns the edit and delete controls at the card's bottom right", async () => {
    const actionList = actions({
      loadFact: vi.fn().mockResolvedValue(editableDetail),
      save: vi.fn().mockResolvedValue(editableDetail),
      remove: vi.fn().mockResolvedValue(undefined),
    });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    const card = root.querySelector('[data-fact-id="fact-1"]')!;
    const controls = card.querySelector<HTMLElement>(".knowledge-fact-actions")!;
    expect(controls.hidden).toBe(false);
    // The aligned control row sits at the very bottom of the card, after the body.
    expect(card.lastElementChild).toBe(controls);
    const body = card.querySelector<HTMLElement>(".knowledge-fact-body")!;
    expect(body.compareDocumentPosition(controls) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const edit = controls.querySelector<HTMLButtonElement>(".knowledge-fact-edit")!;
    const del = controls.querySelector<HTMLButtonElement>(".knowledge-fact-delete")!;
    // Keyboard-accessible real buttons with meaningful localized names and equal sizing.
    expect(edit).toBeInstanceOf(HTMLButtonElement);
    expect(del).toBeInstanceOf(HTMLButtonElement);
    expect(edit.className).toContain("icon-button");
    expect(del.className).toContain("icon-button");
    expect(edit.getAttribute("aria-label")).toBe("Edit fact: Battle of Austerlitz");
    expect(del.getAttribute("aria-label")).toBe("Delete fact: Battle of Austerlitz");
    // The delete control is labelled with a drawn trash-bin icon, not text.
    expect(del.querySelector("svg.icon-trash")).not.toBeNull();
    expect(del.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  });

  it("keeps the delete control hidden for read-only or unauthorized facts", async () => {
    const detail: KnowledgeFactDetail = { ...editableDetail, editable: false };
    const withRemove = actions({ loadFact: vi.fn().mockResolvedValue(detail), remove: vi.fn() });
    const { root } = render(structuredClone(data), withRemove);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    expect(root.querySelector<HTMLElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!.hidden).toBe(true);
    expect(root.querySelector<HTMLElement>('[data-fact-id="fact-1"] .knowledge-fact-actions')!.hidden).toBe(true);
    // Editable facts without a remove action never offer the card delete control either.
    const withoutRemove = actions({ loadFact: vi.fn().mockResolvedValue(editableDetail), save: vi.fn() });
    const second = render(structuredClone(data), withoutRemove);
    second.root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    expect(second.root.querySelector<HTMLElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!.hidden).toBe(true);
    expect(second.root.querySelector<HTMLElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.hidden).toBe(false);
  });

  it("cancelling the delete confirmation keeps the fact and the controls", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(editableDetail), remove });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!.click();
    expect(confirm).toHaveBeenCalledWith("Delete this fact?");
    expect(remove).not.toHaveBeenCalled();
    expect(root.querySelector('[data-fact-id="fact-1"]')).not.toBeNull();
    expect(root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!.disabled).toBe(false);
  });

  it("deletes through the authorized remove contract and refreshes the list", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    const loadFacts = vi.fn().mockResolvedValue(facts);
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(editableDetail), remove, loadFacts });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!.click();
    await flush();
    expect(remove).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith(editableDetail);
    // One confirmation is asked; the removal refreshes the fact list.
    expect(window.confirm).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(loadFacts).toHaveBeenCalledTimes(1));
  });

  it("reports a failed deletion honestly and re-enables the controls", async () => {
    const remove = vi.fn().mockRejectedValue(new Error("denied"));
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(editableDetail), remove });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const del = root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-delete')!;
    del.click();
    await flush();
    expect(root.querySelector('[data-fact-id="fact-1"]')).not.toBeNull();
    const failure = root.querySelector<HTMLElement>('[data-fact-id="fact-1"] .knowledge-fact-delete-error')!;
    expect(failure.hidden).toBe(false);
    expect(failure.textContent).toBe("Something went wrong.");
    expect(del.disabled).toBe(false);
    expect(root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.disabled).toBe(false);
  });
});

describe("fact editor", () => {
  const editableDetail: KnowledgeFactDetail = {
    id: "fact-1", title: "Battle of Austerlitz", description: "d",
    links: [], tags: [], origin: "manual", content_version: 1, editable: true,
    statistics: { status: "none" },
  };

  it("exposes no merge controls or merge action", async () => {
    const actionList = actions({
      loadFact: vi.fn().mockResolvedValue(editableDetail),
      save: vi.fn().mockResolvedValue(editableDetail),
    });
    const { root } = render(structuredClone(data), actionList);
    expect("merge" in actionList).toBe(false);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect([...dialog.querySelectorAll("button")]
      .some((item) => /merge/i.test(item.textContent ?? ""))).toBe(false);
    expect(dialog.querySelector("input[aria-label='Target fact ID']")).toBeNull();
  });

  it("moves tag rename and delete controls off the card into the editor", async () => {
    const tag = vi.fn().mockResolvedValue(undefined);
    const detail: KnowledgeFactDetail = {
      ...editableDetail, tags: [{ id: "tag-1", name: "napoleonic wars", colour: "#1565c0" }],
    };
    let resolveTags!: (pool: KnowledgeTagPool) => void;
    const save = vi.fn().mockResolvedValue(detail);
    const actionList = actions({
      loadFact: vi.fn().mockResolvedValue(detail), save, tag,
      loadTags: vi.fn().mockResolvedValueOnce(tagPool)
        .mockImplementationOnce(() => new Promise<KnowledgeTagPool>((resolve) => { resolveTags = resolve; })),
    });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    const card = root.querySelector('[data-fact-id="fact-1"]')!;
    // Tag chips render read-only on the card: no rename/delete buttons there.
    expect(card.querySelectorAll(".knowledge-fact-tags button")).toHaveLength(0);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect([...dialog.querySelectorAll("button")]
      .some((item) => item.getAttribute("aria-label") === "Rename tag: napoleonic wars")).toBe(true);
    expect([...dialog.querySelectorAll("button")]
      .some((item) => item.getAttribute("aria-label") === "Delete tag: napoleonic wars")).toBe(true);
    vi.spyOn(window, "prompt").mockReturnValue(" Naval History ");
    dialog.querySelector<HTMLButtonElement>("[aria-label='Rename tag: napoleonic wars']")!.click();
    await flush();
    expect(tag).toHaveBeenCalledWith("tag_rename", "tag-1", " Naval History ");
    dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(save).not.toHaveBeenCalled();
    resolveTags({ items: [{ tag_id: "tag-1", name: "naval history", colour: "#1565c0" }] });
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ tag_names: ["naval history"] }), detail,
    ));
  });

  it("refreshes an open editor when the initial tag pool arrives", async () => {
    let resolveTags!: (pool: KnowledgeTagPool) => void;
    const { root } = render(structuredClone(data), actions({
      loadTags: vi.fn(() => new Promise<KnowledgeTagPool>((resolve) => { resolveTags = resolve; })),
      loadFact: vi.fn().mockResolvedValue(editableDetail),
      save: vi.fn().mockResolvedValue(editableDetail),
    }));
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.querySelectorAll('.knowledge-tag-options input[type="checkbox"]')).toHaveLength(0);
    resolveTags(tagPool);
    await vi.waitFor(() => expect(dialog.querySelectorAll('.knowledge-tag-options input[type="checkbox"]'))
      .toHaveLength(tagPool.items.length));
  });

  it.each(["tag_rename", "tag_delete"] as const)("refreshes canonical options after %s", async (command) => {
    const detail: KnowledgeFactDetail = {
      ...editableDetail, tags: [{ id: "tag-1", name: "napoleonic wars", colour: "#1565c0" }],
    };
    const save = vi.fn().mockResolvedValue(detail);
    const pool: KnowledgeTagPool = { items: command === "tag_delete" ? [] : [
      { tag_id: "tag-1", name: "naval history", colour: "#1565c0" },
    ] };
    const { root } = render(structuredClone(data), actions({
      loadFact: vi.fn().mockResolvedValue(detail), save,
      tag: vi.fn().mockResolvedValue(undefined),
      loadTags: vi.fn().mockResolvedValueOnce(tagPool).mockResolvedValue(pool),
    }));
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    vi.spyOn(window, "prompt").mockReturnValue(" Naval History ");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const label = command === "tag_rename" ? "Rename tag" : "Delete tag";
    dialog.querySelector<HTMLButtonElement>(`[aria-label='${label}: napoleonic wars']`)!.click();
    await vi.waitFor(() => expect(dialog.querySelector('input[value="napoleonic wars"]')).toBeNull());
    if (command === "tag_rename") {
      expect(dialog.querySelector<HTMLInputElement>('input[value="naval history"]')!.checked).toBe(true);
    }
    dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ tag_names: command === "tag_delete" ? [] : ["naval history"] }), detail,
    ));
  });

  it("uploads, shows, removes and persists fact images through the editor", async () => {
    const existing = { id: "image-1", url: "/api/miniapp/knowledge/images/image-1", content_type: "image/png" };
    const uploaded = { id: "image-2", url: "/api/miniapp/knowledge/images/image-2", content_type: "image/jpeg" };
    const detail: KnowledgeFactDetail = { ...editableDetail, images: [existing] };
    const uploadImage = vi.fn().mockResolvedValue(uploaded);
    const save = vi.fn().mockResolvedValue(detail);
    const actionList = actions({ loadFact: vi.fn().mockResolvedValue(detail), uploadImage, save });
    const { root } = render(structuredClone(data), actionList);
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    // The existing attachment is shown with its server URL and can be removed.
    expect(dialog.querySelector(".knowledge-image img")?.getAttribute("src")).toBe(existing.url);

    const picker = dialog.querySelector<HTMLInputElement>(".knowledge-images-field input[type='file']")!;
    const pick = (file: File): void => {
      Object.defineProperty(picker, "files", { value: [file], configurable: true });
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    };
    pick(new File([new Uint8Array([1, 2, 3])], "pic.jpg", { type: "image/jpeg" }));
    await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledWith("AQID"));
    await vi.waitFor(() => expect(dialog.querySelectorAll(".knowledge-image")).toHaveLength(2));

    // Unsupported types are rejected locally without an upload request.
    pick(new File([new Uint8Array([1])], "note.txt", { type: "text/plain" }));
    await vi.waitFor(() => expect(dialog.querySelector("[role='alert']")?.textContent)
      .toBe("Only PNG, JPEG or WebP images are supported."));
    expect(uploadImage).toHaveBeenCalledTimes(1);

    // Oversized files are rejected locally too.
    pick(new File([new Uint8Array(2 * 1024 * 1024 + 1)], "big.png", { type: "image/png" }));
    await vi.waitFor(() => expect(dialog.querySelector("[role='alert']")?.textContent)
      .toBe("Image is larger than 2 MiB."));
    expect(uploadImage).toHaveBeenCalledTimes(1);

    // Removing a thumbnail drops its id from the saved set.
    dialog.querySelector<HTMLButtonElement>("[aria-label='Remove image 1']")!.click();
    expect(dialog.querySelectorAll(".knowledge-image")).toHaveLength(1);
    dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ image_ids: ["image-2"] }), detail);
  });

  it.each([true, false])("keeps the editor open after upload failure (editing=%s)", async (editing) => {
    let rejectUpload!: (error: Error) => void;
    const image = { id: "retry-image", url: "/images/retry", content_type: "image/png" };
    const uploadImage = vi.fn()
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectUpload = reject; }))
      .mockResolvedValue(image);
    const save = vi.fn().mockResolvedValue(editableDetail);
    const { root, button } = render(structuredClone(data), actions({
      loadFact: vi.fn().mockResolvedValue(editableDetail), uploadImage, save,
    }));
    if (editing) {
      root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
      await flush();
      root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    } else button("Add fact")!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    const picker = dialog.querySelector<HTMLInputElement>('input[type="file"]')!;
    const pick = (): void => {
      Object.defineProperty(picker, "files", { configurable: true,
        value: [new File(["png"], "image.png", { type: "image/png" })] });
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const submit = (): void => {
      dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    };
    pick();
    await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledOnce());
    submit();
    rejectUpload(new Error("Upload failed"));
    await vi.waitFor(() => expect(dialog.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false));
    expect(save).not.toHaveBeenCalled();
    expect(dialog.isConnected).toBe(true);
    expect(dialog.querySelector('[role="alert"]')!.textContent).toBe("Something went wrong.");
    submit();
    await flush();
    expect(save).not.toHaveBeenCalled();
    pick();
    await vi.waitFor(() => expect(dialog.querySelectorAll(".knowledge-image")).toHaveLength(1));
    submit();
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ image_ids: ["retry-image"] }), editing ? editableDetail : undefined,
    ));
  });

  it("waits for an in-flight image upload before saving the complete attachment set", async () => {
    const uploaded = { id: "image-new", url: "/api/miniapp/knowledge/images/image-new", content_type: "image/png" };
    let resolveUpload!: (image: typeof uploaded) => void;
    const uploadImage = vi.fn(() => new Promise<typeof uploaded>((resolve) => { resolveUpload = resolve; }));
    const save = vi.fn().mockResolvedValue(editableDetail);
    const { root } = render(structuredClone(data), actions({
      loadFact: vi.fn().mockResolvedValue(editableDetail), uploadImage, save,
    }));
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    const picker = dialog.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(picker, "files", { value: [new File(["png"], "image.png", { type: "image/png" })] });
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(uploadImage).toHaveBeenCalledOnce());
    dialog.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(save).not.toHaveBeenCalled();
    resolveUpload(uploaded);
    await vi.waitFor(() => expect(save).toHaveBeenCalledWith(
      expect.objectContaining({ image_ids: ["image-new"] }), editableDetail,
    ));
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
    const { root } = render(structuredClone(data), actionList);
    await flush();
    root.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-header')!.click();
    await flush();
    const pencil = document.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!;
    // Keyboard-accessible control: a real button with a meaningful localized label.
    expect(pencil).toBeInstanceOf(HTMLButtonElement);
    expect(pencil.getAttribute("aria-label")).toBe("Edit fact: Battle of Austerlitz");
    expect(pencil.tabIndex).toBe(0);
    pencil.click();
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
    document.querySelector<HTMLButtonElement>('[data-fact-id="fact-1"] .knowledge-fact-edit')!.click();
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

  it("filters the fact list additively by several tags at once", async () => {
    const reloaded: KnowledgeFactsResource = { ...facts, items: [facts.items[0]!], total: 1, next_cursor: null };
    const loadFacts = vi.fn().mockResolvedValue(reloaded);
    const actionList = actions({ loadFacts });
    const { root } = render(structuredClone(data), actionList);
    await flush();
    const chips = [...root.querySelectorAll<HTMLButtonElement>(".knowledge-tag-filter-chip")];
    expect(chips.map((chip) => chip.textContent)).toEqual(["history", "napoleonic wars"]);

    chips.find((chip) => chip.textContent === "napoleonic wars")!.click();
    await flush();
    expect(loadFacts).toHaveBeenLastCalledWith({ search: "", order: "title_asc", tags: ["napoleonic wars"] });
    expect(chips.find((chip) => chip.textContent === "napoleonic wars")!.getAttribute("aria-pressed"))
      .toBe("true");

    // A second tag joins the filter instead of replacing the first one.
    chips.find((chip) => chip.textContent === "history")!.click();
    await flush();
    expect(loadFacts).toHaveBeenLastCalledWith({ search: "", order: "title_asc", tags: ["napoleonic wars", "history"] });

    // Deselecting one chip leaves the other selected.
    chips.find((chip) => chip.textContent === "napoleonic wars")!.click();
    await flush();
    expect(loadFacts).toHaveBeenLastCalledWith({ search: "", order: "title_asc", tags: ["history"] });
  });
});

describe("sharing links", () => {
  const link = (id: string, active = true, overrides: Partial<ShareLink> = {}): ShareLink => ({
    id, token: `token-${id}`,
    url: `https://t.me/sitg_bot?start=kl_token-${id}`,
    app_url: `/knowledge#import=token-${id}`,
    launch_available: true,
    created_at: "2026-10-01T00:00:00Z", deactivated_at: active ? null : "2026-10-02T00:00:00Z", active,
    ...overrides,
  });

  it("opens a Share library dialog, creates a link and shows the actual bot share URL unchanged", async () => {
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
    expect(url.value).toBe("https://t.me/sitg_bot?start=kl_token-link-1");

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
    expect(writeText).toHaveBeenCalledWith("https://t.me/sitg_bot?start=kl_token-link-1");
    expect(button("Copied", dialog)).toBeDefined();
  });

  it("shows a localized unavailable message instead of a copyable link without a launch identity", async () => {
    const unavailable: KnowledgeSharing = {
      ...sharing,
      links: [link("link-1", true, { url: null, launch_available: false, app_url: "/knowledge#import=token-link-1" })],
    };
    const actionList = actions({ loadSharing: vi.fn().mockResolvedValue(unavailable) });
    const { button } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Sharing links are unavailable right now");
    expect(dialog.querySelector("[aria-label='Import link']")).toBeNull();
    expect([...dialog.querySelectorAll("button")].some((item) => item.textContent === "Copy")).toBe(false);
  });

  it("deactivated links never offer a copyable URL", async () => {
    const withLinks: KnowledgeSharing = { ...sharing, links: [link("link-1", false)] };
    const actionList = actions({ loadSharing: vi.fn().mockResolvedValue(withLinks) });
    const { button } = render(structuredClone(data), actionList);
    button("Share library")!.click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Deactivated");
    expect(dialog.querySelector("[aria-label='Import link']")).toBeNull();
    expect([...dialog.querySelectorAll("button")].some((item) => item.textContent === "Copy")).toBe(false);
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

  it("refreshes an empty recipient library and tag pool after importing", async () => {
    const view = structuredClone(data);
    view.facts = { ...view.facts, items: [], total: 0, next_cursor: null };
    view.import_token = "token-1";
    const actionList = actions({
      importLibrary: vi.fn().mockResolvedValue({ imported_count: 1, skipped_count: 0 }),
      loadTags: vi.fn().mockResolvedValueOnce({ items: [] }).mockResolvedValue(tagPool),
    });
    const { root, button } = render(view, actionList);
    await flush();
    expect(root.textContent).toContain("0 facts");
    button("Import")!.click();
    await vi.waitFor(() => {
      expect(root.querySelector(".knowledge-total")?.textContent).toBe(`${facts.total} facts`);
      expect(root.querySelector(".knowledge-list")?.textContent).toContain(facts.items[0]!.title);
      expect(root.querySelectorAll(".knowledge-tag-filter-chip").length).toBe(tagPool.items.length);
      expect(document.querySelector(".knowledge-dialog")?.textContent).toContain("Imported 1 facts");
    });
    expect(actionList.loadFacts).toHaveBeenCalledOnce();
    expect(actionList.loadTags).toHaveBeenCalledTimes(2);
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

  it("labels packet cards with the real tournament names, not a generic word", async () => {
    const controller = new AbortController();
    try {
      const fromTournament = {
        ...packet("packet-1", "Cup 2026"), scope: "tournament" as const,
        tournaments: [{ id: "tournament-1", name: "Autumn Cup" }, { id: "tournament-2", name: "Spring Cup" }],
      };
      const loadPackets = vi.fn().mockResolvedValue({ items: [fromTournament], next_cursor: null });
      const { root } = renderPackets(actions({ loadPackets }), controller);
      await vi.advanceTimersByTimeAsync(0);
      const badges = [...root.querySelectorAll(".knowledge-scope-badge")]
        .map((badge) => badge.textContent);
      expect(badges).toEqual(["Autumn Cup", "Spring Cup"]);
      // A training-scope upload without tournaments keeps its meaningful scope label.
      const training = { ...packet("packet-2", "Drills"), tournaments: [] };
      loadPackets.mockResolvedValue({ items: [training], next_cursor: null });
      await vi.advanceTimersByTimeAsync(30_000);
      expect(root.querySelectorAll(".knowledge-scope-badge")[0]?.textContent).toBe("Training");
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
