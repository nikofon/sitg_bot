import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeImportCard,
  KnowledgeImportDetail,
  KnowledgeViewData,
} from "./knowledge";
import { renderKnowledge, type KnowledgeActions } from "./knowledge";

/**
 * Mock-backed browser-behavior tests for the Pending imports tab and its
 * review dialog. They exercise user actions against stubbed API actions
 * only; they are not real LLM/DB/browser deployment acceptance.
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
  "knowledge.share": "Share library",
  "knowledge.fact.author": "Author: {author}",
  "knowledge.fact.author_field": "Author (optional)",
  "knowledge.imports.empty": "No imports yet.",
  "knowledge.imports.loading": "Loading imports…",
  "knowledge.imports.type.dissection": "Packet dissection",
  "knowledge.imports.type.collection": "Shared library import",
  "knowledge.imports.status.working": "Working",
  "knowledge.imports.status.pending": "Pending review",
  "knowledge.imports.status.approved": "Approved",
  "knowledge.imports.status.rejected": "Rejected",
  "knowledge.imports.status.failed": "Failed",
  "knowledge.imports.author": "Author: {author}",
  "knowledge.imports.fact_count": "{count} facts",
  "knowledge.imports.source_packet": "Source packet: {name}",
  "knowledge.imports.created": "Created: {date}",
  "knowledge.imports.review": "Review",
  "knowledge.imports.review_title": "Review import",
  "knowledge.imports.section.new": "New facts",
  "knowledge.imports.section.potential_duplicates": "Potential duplicates",
  "knowledge.imports.section.duplicates": "Duplicates",
  "knowledge.imports.select_all": "Select all",
  "knowledge.imports.deselect_all": "Deselect all",
  "knowledge.imports.add_selected": "Add selected facts",
  "knowledge.imports.reject": "Reject import",
  "knowledge.imports.reject_confirm": "Reject this import?",
  "knowledge.imports.approved_note": "This import was approved; its facts are in your library.",
  "knowledge.imports.rejected_note": "This import was rejected.",
  "knowledge.import.staged": "The shared facts were sent for review.",
  "common.close": "Close",
};

const i18n = {
  t: (key: string, parameters: Record<string, unknown> = {}): string =>
    (messages[key] ?? key).replace(/\{(\w+)\}/g, (_: string, name: string): string => {
      const value = parameters[name];
      return typeof value === "string" || typeof value === "number" ? String(value) : `{${name}}`;
    }),
  locale: "en" as const,
};

const data: KnowledgeViewData = {
  facts: {
    state: "ready", order: "title_asc", total: 1, next_cursor: null,
    items: [{ id: "fact-1", title: "Existing fact", tag_names: [],
      origin: "manual", statistics: { status: "none" }, updated_at: "2026-10-01" }],
  },
  sharing: { grants: [], shared: [], links: [] },
  dissection: { packets: [], requests: [], next_cursor: null },
};

const importCard = (overrides: Partial<KnowledgeImportCard>): KnowledgeImportCard => ({
  id: "import-1", type: "collection", author: "Grace", status: "pending",
  fact_count: 3, packet_name: null, created_at: "2026-10-02T12:00:00Z",
  request_id: null, ...overrides,
});

const candidate = (id: string, title: string, selected: boolean) => ({
  id, title, description: `Description of ${title}`, author: "Ada",
  links: [{ url: "https://example.test/a", title: "Source" }],
  images: [], tag_names: ["history"], selected,
});

const importDetail: KnowledgeImportDetail = {
  id: "import-1", type: "collection", author: "Grace", status: "pending",
  fact_count: 3, packet_name: null, created_at: "2026-10-02T12:00:00Z",
  request_id: null, approved_count: 0,
  sections: {
    new: [candidate("c-new", "Brand-new fact", true)],
    potential_duplicates: [candidate("c-pot", "Potential duplicate", true)],
    duplicates: [candidate("c-dup", "Exact duplicate", false)],
  },
};

const decision = {
  import_id: "import-1", status: "approved", approved_count: 2, skipped_count: 1,
};

let controller: AbortController | undefined;

const importActions = (overrides: Partial<KnowledgeActions> = {}): KnowledgeActions => ({
  signal: (controller ??= new AbortController()).signal,
  loadFacts: vi.fn().mockResolvedValue(data.facts),
  loadFact: vi.fn(),
  loadTags: vi.fn().mockResolvedValue({ items: [] }),
  dissect: vi.fn(),
  loadImports: vi.fn().mockResolvedValue({ items: [importCard({})], next_cursor: null }),
  loadImportDetail: vi.fn().mockResolvedValue(importDetail),
  decideImport: vi.fn().mockResolvedValue(decision),
  ...overrides,
});

function render(actionList: KnowledgeActions) {
  const root = renderKnowledge(structuredClone(data), i18n as never, actionList);
  document.body.append(root);
  const button = (text: string, scope: ParentNode = document): HTMLButtonElement =>
    [...scope.querySelectorAll("button")].find((item) => item.textContent === text)!;
  return { root, actions: actionList, button };
}

const openImports = (actionList: KnowledgeActions) => {
  const rendered = render(actionList);
  rendered.button("Pending imports").click();
  return rendered;
};

const flush = async (): Promise<void> => {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
};

afterEach(() => {
  controller?.abort();
  controller = undefined;
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("pending imports tab", () => {
  it("loads import cards only when the tab becomes active", async () => {
    const actionList = importActions();
    const { root, button } = render(actionList);
    expect(actionList.loadImports).not.toHaveBeenCalled();
    button("Pending imports").click();
    await flush();
    expect(actionList.loadImports).toHaveBeenCalledOnce();
    const panel = root.querySelector<HTMLElement>('[data-panel="imports"]')!;
    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain("Shared library import");
    expect(panel.textContent).toContain("Author: Grace");
    expect(panel.textContent).toContain("Pending review");
    expect(panel.textContent).toContain("3 facts");
  });

  it("loads older pending imports beyond 100 and opens their review", async () => {
    const first = Array.from({ length: 100 }, (_, index) =>
      importCard({ id: `newer-${index}`, status: "approved" }));
    const loadImports = vi.fn()
      .mockResolvedValueOnce({ items: first, next_cursor: "older/page+1" })
      .mockResolvedValueOnce({ items: [importCard({ id: "old-pending" })], next_cursor: null });
    const actionList = importActions({
      loadImports,
      loadImportDetail: vi.fn().mockResolvedValue({ ...importDetail, id: "old-pending" }),
    });
    const { root } = openImports(actionList);
    await flush();
    expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(100);
    expect(loadImports).toHaveBeenCalledWith(null);
    const more = root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!;
    expect(more.hidden).toBe(false);
    more.click();
    more.click();
    await flush();
    expect(loadImports).toHaveBeenCalledTimes(2);
    expect(loadImports).toHaveBeenLastCalledWith("older/page+1");
    expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(101);
    expect(more.hidden).toBe(true);
    root.querySelector<HTMLButtonElement>(
      '[data-import-id="old-pending"] .knowledge-import-review')!.click();
    await flush();
    expect(actionList.loadImportDetail).toHaveBeenCalledWith("old-pending");
    expect(document.querySelector("[data-import-approve]")).not.toBeNull();
  });

  it("preserves loaded pages and the cursor when loading more fails", async () => {
    const loadImports = vi.fn()
      .mockResolvedValueOnce({ items: [importCard({})], next_cursor: "next" })
      .mockRejectedValueOnce(new Error("net"))
      .mockResolvedValueOnce({ items: [importCard({ id: "older" })], next_cursor: null });
    const { root } = openImports(importActions({ loadImports }));
    await flush();
    const more = root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!;
    more.click();
    await flush();
    expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(1);
    expect(root.querySelector<HTMLElement>('[data-panel="imports"] > [role="alert"]')!.hidden)
      .toBe(false);
    expect(more.disabled).toBe(false);
    more.click();
    await flush();
    expect(loadImports).toHaveBeenLastCalledWith("next");
    expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(2);
    expect(root.querySelector<HTMLElement>('[data-panel="imports"] > [role="alert"]')!.hidden)
      .toBe(true);
  });

  it("refreshes from the first page and ignores an older in-flight page", async () => {
    let resolvePage!: (page: { items: KnowledgeImportCard[]; next_cursor: null }) => void;
    const loadImports = vi.fn()
      .mockResolvedValueOnce({ items: [importCard({})], next_cursor: "next" })
      .mockImplementationOnce(() => new Promise((resolve) => { resolvePage = resolve; }))
      .mockResolvedValueOnce({ items: [importCard({ id: "fresh" })], next_cursor: null });
    const { root, button } = openImports(importActions({ loadImports }));
    await flush();
    root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!.click();
    button("Refresh", root.querySelector('[data-panel="imports"]')!).click();
    await flush();
    resolvePage({ items: [importCard({ id: "stale" })], next_cursor: null });
    await flush();
    expect(loadImports).toHaveBeenLastCalledWith(null);
    expect([...root.querySelectorAll(".knowledge-import-card")]
      .map((card) => card.getAttribute("data-import-id"))).toEqual(["fresh"]);
    expect(root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!.hidden).toBe(true);
  });

  it("shows own dissection source packets but never collection provenance", async () => {
    const actionList = importActions({
      loadImports: vi.fn().mockResolvedValue({ items: [
        importCard({ type: "dissection", packet_name: "Cup 2026", status: "working" }),
        importCard({ id: "import-2", packet_name: null }),
      ], next_cursor: null }),
    });
    const { root } = openImports(actionList);
    await flush();
    const cards = [...root.querySelectorAll<HTMLElement>(".knowledge-import-card")];
    // The user's own authorized dissection names its source packet.
    expect(cards[0]!.textContent).toContain("Source packet: Cup 2026");
    expect(cards[0]!.textContent).toContain("Working");
    // A collection import never leaks the sender's private packet provenance.
    expect(cards[1]!.textContent).not.toContain("Source packet");
    expect(cards[0]!.querySelector(".knowledge-import-review")).not.toBeNull();
    expect(cards[1]!.querySelector(".knowledge-import-review")).not.toBeNull();
  });

  it("shows the empty state and reports loading failures honestly", async () => {
    const actionList = importActions({
      loadImports: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    });
    const { root } = openImports(actionList);
    await flush();
    expect(root.querySelector('[data-panel="imports"]')!.textContent)
      .toContain("No imports yet.");

    document.body.replaceChildren();
    controller?.abort();
    controller = undefined;
    const failing = importActions({ loadImports: vi.fn().mockRejectedValue(new Error("net")) });
    const { root: failingRoot } = openImports(failing);
    await flush();
    expect(failingRoot.querySelector('[data-panel="imports"]')!.textContent)
      .toContain("Something went wrong.");
  });
});

describe("import review dialog", () => {
  it.each(["working", "failed", "approved", "rejected"] as const)(
    "opens %s imports read-only and permits decisions only after pending",
    async (status) => {
      const actionList = importActions({
        loadImports: vi.fn().mockResolvedValue({
          items: [importCard({ status })], next_cursor: null,
        }),
        loadImportDetail: vi.fn().mockResolvedValue({ ...importDetail, status }),
      });
      const { button } = openImports(actionList);
      await flush();
      button("Review").click();
      await flush();
      const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
      expect(dialog.querySelector(`[data-import-status="${status}"]`)).not.toBeNull();
      expect(dialog.querySelector("input[type=checkbox]")).toBeNull();
      expect(dialog.querySelector("[data-import-approve]")).toBeNull();
      expect(dialog.querySelector(".knowledge-import-reject")).toBeNull();
      expect(actionList.decideImport).not.toHaveBeenCalled();
      dialog.querySelector<HTMLButtonElement>(".knowledge-dialog-close")!.click();
      vi.mocked(actionList.loadImportDetail!).mockResolvedValue(importDetail);
      button("Review").click();
      await flush();
      document.querySelector<HTMLButtonElement>("[data-import-approve]")!.click();
      await flush();
      expect(actionList.decideImport).toHaveBeenCalledWith(
        "import-1", "approve", ["c-new", "c-pot"],
      );
    },
  );

  it("lists the three sections with default selection only for new and potential facts", async () => {
    const actionList = importActions();
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(actionList.loadImportDetail).toHaveBeenCalledWith("import-1");
    for (const key of ["new", "potential_duplicates", "duplicates"] as const) {
      expect(dialog.querySelector(`[data-import-section="${key}"]`)).not.toBeNull();
    }
    expect(dialog.textContent).toContain("New facts (1)");
    expect(dialog.textContent).toContain("Potential duplicates (1)");
    expect(dialog.textContent).toContain("Duplicates (1)");

    const checkbox = (id: string): HTMLInputElement =>
      dialog.querySelector<HTMLInputElement>(`[data-candidate-id="${id}"] input[type=checkbox]`)!;
    // Defaults come from the server classification: new/potential selected,
    // exact duplicates deselected.
    expect(checkbox("c-new").checked).toBe(true);
    expect(checkbox("c-pot").checked).toBe(true);
    expect(checkbox("c-dup").checked).toBe(false);
    // Add-selected controls exist at both the top and the bottom.
    expect(dialog.querySelectorAll("[data-import-approve]").length).toBe(2);
  });

  it("expands a candidate without changing its checkbox and vice versa", async () => {
    const actionList = importActions();
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    const row = dialog.querySelector<HTMLElement>('[data-candidate-id="c-new"]')!;
    const checkbox = row.querySelector<HTMLInputElement>("input[type=checkbox]")!;
    const toggle = row.querySelector<HTMLButtonElement>(".knowledge-theme-toggle")!;
    expect(row.textContent).toContain("Brand-new fact");
    expect(row.querySelector<HTMLElement>(".knowledge-candidate-body")!.hidden).toBe(true);

    toggle.click();
    const body = row.querySelector<HTMLElement>(".knowledge-candidate-body")!;
    expect(body.hidden).toBe(false);
    expect(body.textContent).toContain("Description of Brand-new fact");
    expect(body.textContent).toContain("Author: Ada");
    expect(body.querySelector("a[href='https://example.test/a']")).not.toBeNull();
    expect(checkbox.checked).toBe(true);

    checkbox.click();
    expect(checkbox.checked).toBe(false);
    expect(body.hidden).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("selects and deselects all candidates within one section", async () => {
    const detail: KnowledgeImportDetail = {
      ...importDetail,
      sections: {
        ...importDetail.sections,
        potential_duplicates: [
          candidate("c-pot", "Potential duplicate", true),
          candidate("c-pot-2", "Another potential", false),
        ],
      },
    };
    const actionList = importActions({
      loadImportDetail: vi.fn().mockResolvedValue(detail),
    });
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    const section = dialog.querySelector<HTMLElement>('[data-import-section="potential_duplicates"]')!;
    const toggleAll = section.querySelector<HTMLButtonElement>("[data-import-section-toggle]")!;

    toggleAll.click();
    const boxes = [...section.querySelectorAll<HTMLInputElement>("input[type=checkbox]")];
    expect(boxes.every((box) => box.checked)).toBe(true);
    expect(toggleAll.textContent).toBe("Deselect all");

    toggleAll.click();
    expect(boxes.some((box) => box.checked)).toBe(false);
    expect(toggleAll.textContent).toBe("Select all");
  });

  it("adds the selected facts through either the top or the bottom button", async () => {
    for (const which of [0, 1] as const) {
      document.body.replaceChildren();
      controller = undefined;
      const actionList = importActions();
      const { button } = openImports(actionList);
      await flush();
      button("Review").click();
      await flush();
      const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
      // Deselect one potential duplicate so the decision list is explicit.
      dialog.querySelector<HTMLInputElement>('[data-candidate-id="c-pot"] input')!.click();
      const approve = [...dialog.querySelectorAll<HTMLButtonElement>("[data-import-approve]")][which]!;
      approve.click();
      await flush();

      expect(actionList.decideImport).toHaveBeenCalledWith("import-1", "approve", ["c-new"]);
      expect(actionList.loadFacts).toHaveBeenCalled();
      // Approval refreshes both the library and the pending import cards.
      expect(actionList.loadImports).toHaveBeenCalledTimes(2);
      expect(document.contains(dialog)).toBe(false);
    }
  });

  it("rejects an import after confirmation and refreshes nothing into the library", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const actionList = importActions({
      decideImport: vi.fn().mockResolvedValue({ ...decision, status: "rejected", approved_count: 0 }),
    });
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("Reject import", dialog).click();
    await flush();
    expect(actionList.decideImport).toHaveBeenCalledWith("import-1", "reject", undefined);
    expect(document.contains(dialog)).toBe(false);

    confirmSpy.mockReturnValue(false);
    button("Review").click();
    await flush();
    const reopened = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("Reject import", reopened).click();
    await flush();
    // Declining the confirmation never sends the terminal decision.
    expect(actionList.decideImport).toHaveBeenCalledTimes(1);
    expect(document.contains(reopened)).toBe(true);
    expect(confirmSpy).toHaveBeenCalledTimes(2);
  });

  it("keeps the review open and honest on a stale or conflicting decision", async () => {
    const actionList = importActions({
      decideImport: vi.fn().mockRejectedValue(new Error("stale_write")),
    });
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    button("Add selected facts", dialog).click();
    await flush();
    expect(document.contains(dialog)).toBe(true);
    expect(dialog.querySelector(".error-text")!.textContent).toBe("Something went wrong.");
    const approve = [...dialog.querySelectorAll<HTMLButtonElement>("[data-import-approve]")][0]!;
    expect(approve.disabled).toBe(false);
  });

  it("shows approved imports read-only with no decision controls", async () => {
    const actionList = importActions({
      loadImportDetail: vi.fn().mockResolvedValue({ ...importDetail, status: "approved", approved_count: 2 }),
    });
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("This import was approved");
    expect(dialog.querySelectorAll("[data-import-approve]").length).toBe(0);
    expect([...dialog.querySelectorAll("button")].some((item) =>
      item.textContent === "Reject import")).toBe(false);
    // Terminal review rows show facts without selection checkboxes.
    expect(dialog.querySelectorAll(".knowledge-candidate-row input[type=checkbox]").length).toBe(0);
  });

  it("reports a failed import detail load without inventing success", async () => {
    const actionList = importActions({
      loadImportDetail: vi.fn().mockRejectedValue(new Error("not_found")),
    });
    const { button } = openImports(actionList);
    await flush();
    button("Review").click();
    await flush();
    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Something went wrong.");
    expect(dialog.querySelectorAll(".knowledge-candidate-row").length).toBe(0);
  });
});
