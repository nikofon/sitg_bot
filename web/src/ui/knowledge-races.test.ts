import { afterEach, describe, expect, it, vi } from "vitest";
import { I18n } from "../i18n";
import { renderKnowledge, type KnowledgeActions, type KnowledgeFactsResource,
  type KnowledgeFactSummary, type KnowledgeViewData } from "./knowledge";

const fact = (id: string): KnowledgeFactSummary => ({
  id, title: id, tag_names: ["history"], origin: "manual", updated_at: "2026-10-01",
  statistics: { status: "none" },
});
const page = (title: string, cursor: string | null = null): KnowledgeFactsResource => ({
  state: "ready", items: [fact(title)], total: 1, order: "title_asc", next_cursor: cursor,
});
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const click = (name: string) => [...document.querySelectorAll("button")]
  .find((button) => button.textContent === name)!.click();
function setup(overrides: Partial<KnowledgeActions> = {}) {
  const data: KnowledgeViewData = {
    facts: page("initial", "old-cursor"), sharing: { grants: [], links: [] },
    dissection: { packets: [], requests: [] },
  };
  const actions: KnowledgeActions = {
    loadFacts: vi.fn().mockResolvedValue(page("fresh")), loadFact: vi.fn(),
    loadTags: vi.fn().mockResolvedValue({ items: [
      { tag_id: "tag", name: "history", colour: "#1565c0" },
    ] }), dissect: vi.fn().mockResolvedValue({}), ...overrides,
  };
  const root = renderKnowledge(data, new I18n("en"), actions);
  document.body.append(root);
  return { root, actions, data };
}
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

describe("Knowledge query response ownership", () => {
  it("keeps simultaneous polling jobs distinguishable with partial explanations", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    try {
      const requests = [
        { id: "job-1", packet_id: "version-a", status: "partially_succeeded", base: true,
          applied_count: 2, partial: true },
        { id: "job-2", packet_id: "version-b", status: "running", base: false,
          applied_count: 0, partial: false },
      ];
      const { root } = setup({ signal: controller.signal,
        loadDissections: vi.fn().mockResolvedValue(requests) });
      click("Packets");
      await vi.advanceTimersByTimeAsync(3000);
      const jobs = root.querySelectorAll(".knowledge-job");
      expect(jobs).toHaveLength(2);
      expect(jobs[0]!.textContent).toContain("version-a");
      expect(jobs[0]!.textContent).toContain("Some candidates failed");
      expect(jobs[1]!.textContent).toContain("version-b");
      expect(jobs[1]!.textContent).not.toContain("Some candidates failed");
    } finally { controller.abort(); vi.useRealTimers(); }
  });

  it("ignores a stale pagination response after a search reload", async () => {
    const old = deferred<KnowledgeFactsResource>();
    const loadFacts = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce(page("new", "new-cursor"));
    const { root, data } = setup({ loadFacts });
    click("Load more");
    const search = root.querySelector<HTMLInputElement>("[type=search]")!;
    search.value = "new";
    search.dispatchEvent(new Event("input")); search.dispatchEvent(new Event("change"));
    await flush();
    old.resolve(page("obsolete", "obsolete-cursor"));
    await flush();
    expect(root.querySelector(".knowledge-list")!.textContent).toContain("new");
    expect(root.querySelector(".knowledge-list")!.textContent).not.toContain("obsolete");
    expect(data.facts.next_cursor).toBe("new-cursor");
  });

  it("does not drop a tag change during a reload or accept the older result", async () => {
    const first = deferred<KnowledgeFactsResource>();
    const second = deferred<KnowledgeFactsResource>();
    const loadFacts = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { root } = setup({ loadFacts });
    await flush();
    click("Refresh");
    const tagChip = [...root.querySelectorAll<HTMLButtonElement>(".knowledge-tag-filter-chip")]
      .find((chip) => chip.textContent === "history")!;
    tagChip.click();
    expect(loadFacts).toHaveBeenLastCalledWith({ search: "", order: "title_asc", tags: ["history"] });
    second.resolve(page("tagged")); await flush();
    first.resolve(page("unfiltered")); await flush();
    expect(root.querySelector(".knowledge-list")!.textContent).toContain("tagged");
    expect(root.querySelector(".knowledge-list")!.textContent).not.toContain("unfiltered");
  });

  it.each(["search", "author", "tags", "order", "changed", "refresh"])(
    "invalidates pagination synchronously during a %s reload",
    async (trigger) => {
      const pending = deferred<KnowledgeFactsResource>();
      const loadFacts = vi.fn().mockReturnValueOnce(pending.promise)
        .mockResolvedValueOnce(page("next"));
      const { root, data } = setup({ loadFacts });
      await flush();
      const more = root.querySelector<HTMLButtonElement>(".knowledge-load-more")!;
      if (trigger === "search" || trigger === "author") {
        const input = root.querySelectorAll<HTMLInputElement>("input[type=search]")
          [trigger === "search" ? 0 : 1]!;
        input.value = "filtered";
        input.dispatchEvent(new Event("input"));
        input.dispatchEvent(new Event("change"));
      } else if (trigger === "tags") {
        root.querySelector<HTMLButtonElement>(".knowledge-tag-filter-chip")!.click();
      } else if (trigger === "order") {
        const select = root.querySelector<HTMLSelectElement>(".knowledge-controls select")!;
        select.value = "title_desc";
        select.dispatchEvent(new Event("change"));
      } else if (trigger === "changed") {
        root.dispatchEvent(new Event("knowledge:changed"));
      } else click("Refresh");
      expect(data.facts.next_cursor).toBeNull();
      expect(more.hidden).toBe(true);
      expect(more.disabled).toBe(true);
      more.click();
      expect(loadFacts).toHaveBeenCalledTimes(1);
      pending.resolve(page("filtered", "filtered-cursor"));
      await flush();
      expect(more.hidden).toBe(false);
      expect(more.disabled).toBe(false);
      more.click();
      await flush();
      expect(loadFacts.mock.calls[1]![0].cursor).toBe("filtered-cursor");
      expect(data.facts.items.map((item) => item.id)).toEqual(["filtered", "next"]);
    },
  );

  it("does not let stale pagination re-enable pagination during a reload", async () => {
    const old = deferred<KnowledgeFactsResource>();
    const current = deferred<KnowledgeFactsResource>();
    const loadFacts = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise);
    const { root } = setup({ loadFacts });
    click("Load more");
    click("Refresh");
    old.resolve(page("stale", "stale-cursor"));
    await flush();
    const more = root.querySelector<HTMLButtonElement>(".knowledge-load-more")!;
    expect(more.disabled).toBe(true);
    more.click();
    expect(loadFacts).toHaveBeenCalledTimes(2);
    current.resolve(page("current", "current-cursor"));
    await flush();
    expect(more.disabled).toBe(false);
    expect(root.querySelector(".knowledge-list")!.textContent).not.toContain("stale");
  });

  it("keeps pagination invalid after a failed reload and permits a fresh retry", async () => {
    const { root, data, actions } = setup({ loadFacts: vi.fn()
      .mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(page("retried", "retry")) });
    click("Refresh");
    await flush();
    expect(data.facts.next_cursor).toBeNull();
    root.querySelector<HTMLButtonElement>(".knowledge-load-more")!.click();
    expect(actions.loadFacts).toHaveBeenCalledTimes(1);
    click("Refresh");
    await flush();
    expect(data.facts.next_cursor).toBe("retry");
    expect(root.querySelector<HTMLButtonElement>(".knowledge-load-more")!.disabled).toBe(false);
  });

  it("retains appended facts in the backing collection", async () => {
    const { data } = setup({ loadFacts: vi.fn().mockResolvedValue(page("second")) });
    click("Load more"); await flush();
    expect(data.facts.items.map((item) => item.id)).toEqual(["initial", "second"]);
  });

  it("only loads a fact once across rapid expand/collapse clicks", async () => {
    const read = deferred<Awaited<ReturnType<KnowledgeActions["loadFact"]>>>();
    const loadFact = vi.fn().mockReturnValue(read.promise);
    const { root } = setup({ loadFact });
    const header = root.querySelector<HTMLButtonElement>(".knowledge-fact-header")!;
    header.click(); header.click(); header.click();
    expect(loadFact).toHaveBeenCalledTimes(1);
    read.resolve({ id: "initial", title: "initial", description: "detail", links: [], tags: [],
      origin: "manual", content_version: 1, editable: false, statistics: { status: "none" } });
    await flush();
    expect(root.querySelector(".knowledge-fact-body")!.textContent).toContain("detail");
  });
});
