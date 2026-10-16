import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeFactDetail,
  KnowledgeFactsResource,
  KnowledgeViewData,
} from "./knowledge";
import { renderKnowledge } from "./knowledge";

const messages: Record<string, string> = {
  "knowledge.title": "Knowledge library",
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
  "knowledge.fact.tags": "Tags: {tags}",
  "knowledge.fact.links": "No source links.",
  "knowledge.fact.origin.manual": "Manual",
  "knowledge.fact.origin.dissection": "Dissection",
  "knowledge.fact.read_only": "Read-only shared fact",
  "knowledge.fact.version": "version {version}",
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
  "knowledge.sharing.title": "Sharing",
  "knowledge.sharing.empty": "Nobody shares a collection with you.",
  "knowledge.sharing.recipient": "Recipient name",
  "knowledge.sharing.grant": "Share my collection",
  "knowledge.sharing.revoke": "Revoke",
  "knowledge.sharing.revoked": "Revoked",
  "knowledge.sharing.permanent": "Permanent, independent copies",
  "knowledge.sharing.count": "{count} facts copied",
  "knowledge.sharing.received": "Received copies",
  "knowledge.dissection.title": "Packet dissection",
  "knowledge.dissection.packet": "Packet",
  "knowledge.dissection.instructions": "Instructions (optional)",
  "knowledge.dissection.submit": "Start dissection",
  "knowledge.dissection.empty": "No dissections yet.",
  "knowledge.dissection.status.pending": "Pending",
  "knowledge.dissection.status.succeeded": "Succeeded",
  "knowledge.dissection.status.partially_succeeded": "Partial",
  "knowledge.dissection.status.failed": "Failed",
  "knowledge.dissection.partial": "Some candidates failed.",
  "knowledge.dissection.base": "Base",
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

const data: KnowledgeViewData = {
  facts,
  sharing: {
    grants: [
      { id: "grant-1", recipient_name: "Ada", copied_count: 3 },
      { id: "grant-2", recipient_name: "Bob", copied_count: 2 },
    ],
  },
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

function render() {
  const actions = {
    loadFacts: vi.fn(),
    loadFact: vi.fn(),
    grant: vi.fn().mockResolvedValue(undefined),
    revoke: vi.fn().mockResolvedValue(undefined),
    dissect: vi.fn().mockResolvedValue(undefined),
  };
  const root = renderKnowledge(data, i18n as never, actions);
  document.body.append(root);
  const button = (text: string) =>
    [...root.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const field = <T extends HTMLElement>(text: string): T =>
    root.querySelector<T>(`[aria-label='${text}']`)!;
  return { root, actions, button, field };
}

const flush = async (): Promise<void> => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
};

describe("integrated knowledge editing", () => {
  it("saves all manual fields and refreshes the fact page", async () => {
    const detail: KnowledgeFactDetail = {
      id: "new-fact", title: "Title", description: "Description", links: [], tags: [],
      origin: "manual", content_version: 1, editable: true, statistics: { status: "pending" },
    };
    const actions = {
      loadFacts: vi.fn().mockResolvedValue(facts), loadFact: vi.fn(),
      grant: vi.fn(), revoke: vi.fn(), dissect: vi.fn(), save: vi.fn().mockResolvedValue(detail),
    };
    const root = renderKnowledge(structuredClone(data), i18n as never, actions);
    document.body.append(root);
    const form = root.querySelector("form")!;
    const inputs = form.querySelectorAll("input");
    const areas = form.querySelectorAll("textarea");
    inputs[0]!.value = "Title";
    inputs[1]!.value = "history, art";
    areas[0]!.value = "Description";
    areas[1]!.value = "https://example.org/source | Reference title";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flush();
    expect(actions.save).toHaveBeenCalledWith({ title: "Title", description: "Description",
      tag_names: ["history", "art"],
      links: [{ url: "https://example.org/source", title: "Reference title", position: 0 }],
    }, undefined);
    expect(actions.loadFacts).toHaveBeenCalled();
  });

  it("hides editing controls for non-owned references", async () => {
    const actions = { loadFacts: vi.fn(), loadFact: vi.fn().mockResolvedValue({
      id: "fact-1", title: "Shared", description: "Read-only", links: [], tags: [],
      origin: "manual", content_version: 1, editable: false, statistics: { status: "none" },
    }), grant: vi.fn(), revoke: vi.fn(), dissect: vi.fn(), save: vi.fn() };
    const root = renderKnowledge(structuredClone(data), i18n as never, actions);
    document.body.append(root);
    root.querySelector<HTMLButtonElement>(".knowledge-fact-open")!.click();
    await flush();
    expect(root.querySelector(".knowledge-details form")).toBeNull();
    expect(root.querySelector(".knowledge-readonly")?.textContent).toContain("Read-only");
  });
});

afterEach(() => { document.body.replaceChildren(); });

describe("knowledge library view", () => {
  it("renders facts, tag names and explicit statistic states", () => {
    const { root } = render();
    expect(root.textContent).toContain("2 facts");
    expect(root.textContent).toContain("Battle of Austerlitz");
    expect(root.textContent).toContain("Tags: napoleonic wars");
    expect(root.textContent).toContain("Stale");
    expect(root.textContent).toContain("Mentions: at least 5");
    expect(root.textContent).toContain("Common knowledge: 30%");
    expect(root.textContent).toContain("Bingo");
    expect(root.textContent).toContain("Pending calibration");
    expect(root.textContent).toContain("Denied");
    expect(root.textContent).toContain("Permanent, independent copies");
    expect(root.textContent).toContain("3 facts copied");
  });

  it("loads the next page with the cursor and appends its items", async () => {
    const { actions, button } = render();
    const next: KnowledgeFactsResource = {
      ...facts,
      items: [facts.items[1]!],
      next_cursor: null,
    };
    actions.loadFacts.mockResolvedValueOnce(next);
    button("Load more").click();
    await flush();
    expect(actions.loadFacts).toHaveBeenCalledWith({
      cursor: "cursor-2",
      search: "",
      order: "title_asc",
    });
    expect(document.body.textContent).toContain("Rosetta Stone");
  });

  it("loads fact details lazily when a card is opened", async () => {
    const { actions, button } = render();
    const detail: KnowledgeFactDetail = {
      id: "fact-1",
      title: "Battle of Austerlitz",
      description: "1805 battle",
      links: [{ url: "https://example.test/a", title: "Source" }],
      tags: [],
      origin: "dissection",
      content_version: 2,
      editable: false,
      statistics: { status: "pending" },
    };
    actions.loadFact.mockResolvedValueOnce(detail);
    button("Battle of Austerlitz").click();
    expect(document.body.textContent).toContain("Loading…");
    await flush();
    expect(actions.loadFact).toHaveBeenCalledWith("fact-1");
    expect(document.body.textContent).toContain("1805 battle");
    expect(document.body.textContent).toContain("Read-only shared fact");
    expect(document.querySelector("a[href='https://example.test/a']")).not.toBeNull();
  });

  it("shows loading failures without breaking the list", async () => {
    const { actions, button } = render();
    actions.loadFact.mockRejectedValueOnce(new Error("gone"));
    button("Rosetta Stone").click();
    await flush();
    expect(document.body.textContent).toContain("Something went wrong.");
  });

  it("sends permanent copies without offering revocation", async () => {
    const { actions, button, field } = render();
    field<HTMLInputElement>("Recipient name").value = "Carol";
    button("Share my collection").click();
    await flush();
    expect(actions.grant).toHaveBeenCalledWith("Carol");
    expect([...document.querySelectorAll("button")].some((item) => item.textContent === "Revoke")).toBe(false);
    expect(document.body.textContent).not.toContain("Something went wrong.");
  });

  it("starts dissection with packet and instructions", async () => {
    const { actions, button, field } = render();
    field<HTMLTextAreaElement>("Instructions (optional)").value = "Focus on naval history";
    button("Start dissection").click();
    await flush();
    expect(actions.dissect).toHaveBeenCalledWith("packet-1", "Focus on naval history");
    expect(document.body.textContent).toContain("Cup 2026");
    expect(document.body.textContent).toContain("Partial");
    expect(document.body.textContent).toContain("Some candidates failed.");
  });

  it("reloads the list with search and order filters", async () => {
    const { actions, field } = render();
    const reloaded: KnowledgeFactsResource = {
      ...facts,
      items: [facts.items[0]!],
      total: 1,
      next_cursor: null,
    };
    actions.loadFacts.mockResolvedValueOnce(reloaded);
    const search = field<HTMLInputElement>("Search facts");
    search.value = "austerlitz";
    search.dispatchEvent(new Event("input"));
    search.dispatchEvent(new Event("change"));
    await flush();
    expect(actions.loadFacts).toHaveBeenCalledWith({ search: "austerlitz", order: "title_asc" });
    expect(document.body.textContent).toContain("1 facts");
  });
});
