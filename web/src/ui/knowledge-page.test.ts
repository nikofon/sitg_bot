import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { I18n } from "../i18n";
import { renderKnowledgePage } from "./knowledge-page";
import type { KnowledgeViewData, ShareLink } from "./knowledge";

const initial: KnowledgeViewData = {
  facts: { state: "empty", items: [], total: 0, next_cursor: null, order: "title_asc" },
  sharing: { grants: [], links: [] },
  dissection: { packets: [], requests: [], next_cursor: null },
};
const token = "a".repeat(43);
const link: ShareLink = {
  id: "link-1", token, url: `https://t.me/ActualBot?start=kl_${token}`,
  app_url: `/knowledge#import=${token}`, launch_available: true, active: true,
  created_at: "2026-10-01T00:00:00Z", deactivated_at: null,
};
const reply = (data: unknown, status = 200): Response => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json" },
});
const button = (name: string): HTMLButtonElement => [...document.querySelectorAll("button")]
  .find((item) => item.textContent === name)!;

function setup(overrides: Record<string, unknown> = {}) {
  const controller = new AbortController();
  let links: ShareLink[] = [];
  const fetcher = vi.fn<typeof fetch>(async (url, options) => {
    const path = String(url).split("?")[0]!;
    if (path === "/api/miniapp/session") return reply({
      csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en",
    });
    if (path in overrides) return reply(overrides[path]);
    if (path.endsWith("/entitlements")) return reply({ features: {} });
    if (path.endsWith("/facts")) return reply(initial.facts);
    if (path.endsWith("/dissections")) return reply({ items: [] });
    if (path.endsWith("/tags")) return reply({ items: [
      { tag_id: "tag-1", name: "history", colour: "#1565c0" },
    ] });
    if (path.endsWith("/sharing")) return reply({ grants: [], links });
    if (path.endsWith("/link_create")) { links = [link]; return reply(link); }
    if (path.endsWith("/link_deactivate")) {
      links = [{ ...link, active: false, deactivated_at: "2026-10-02T00:00:00Z" }];
      return reply(links[0]);
    }
    if (path.endsWith("/import")) return reply({
      owner_id: "owner", recipient_id: "recipient", imported_count: 2, skipped_count: 1,
    });
    throw new Error(`Unexpected ${options?.method ?? "GET"} ${url}`);
  });
  const root = renderKnowledgePage(structuredClone(initial),
    new ApiClient("signed-init-data", fetcher), new I18n("en"), controller.signal);
  document.body.append(root);
  return { root, fetcher, controller };
}

afterEach(() => {
  document.body.replaceChildren();
  window.history.replaceState({}, "", "/");
  vi.restoreAllMocks();
});

describe("Knowledge page direct HTTP payload contract", () => {
  it("creates and deactivates actual bot links through authenticated typed mutations", async () => {
    const { fetcher, controller } = setup();
    try {
      button("Share library").click();
      await vi.waitFor(() => expect(button("Create link")).toBeDefined());
      button("Create link").click();
      await vi.waitFor(() => expect(document.querySelector<HTMLInputElement>(
        "[aria-label='Import link']")?.value).toBe(link.url));
      vi.spyOn(window, "confirm").mockReturnValue(true);
      button("Deactivate").click();
      await vi.waitFor(() => expect(button("Deactivate")).toBeUndefined());
      const mutation = fetcher.mock.calls.find(([url]) => String(url).endsWith("/link_deactivate"))!;
      expect(JSON.parse(String(mutation[1]?.body))).toEqual({ link_id: link.id });
      expect(mutation[1]?.credentials).toBe("include");
      expect(mutation[1]?.headers).toMatchObject({ "X-CSRF-Token": "csrf-test-token",
        "X-Idempotency-Key": expect.any(String) });
    } finally { controller.abort(); }
  });

  it("preserves the bot WebApp launch fragment through authentication until confirmed import", async () => {
    window.history.replaceState({}, "", `/knowledge?_launch=1#import=${token}`);
    const { fetcher, controller } = setup();
    try {
      expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/import"))).toBe(false);
      button("Import").click();
      await vi.waitFor(() => expect(document.body.textContent).toContain("Imported 2 facts"));
      const mutation = fetcher.mock.calls.find(([url]) => String(url).endsWith("/import"))!;
      expect(JSON.parse(String(mutation[1]?.body))).toEqual({ token });
      expect(window.location.hash).toBe("");
    } finally { controller.abort(); }
  });

  it("keeps both versions of a logical packet and dissects each version", async () => {
    const items = ["version-1", "version-2"].map((id) => ({
      id, packet_id: "logical-packet", name: id, year: 2026, scope: "tournament",
      authors: [], tournaments: [], total_theme_count: 1, fresh_theme_count: 1,
      dissected_theme_count: 0, dissected: false, best_buzz_calculated: false,
    }));
    const { root, fetcher, controller } = setup({
      "/api/miniapp/training/packets": { items, next_cursor: null },
      "/api/miniapp/knowledge/dissect": {},
    });
    try {
      expect(fetcher.mock.calls.some(([url]) => String(url).includes("/training/packets"))).toBe(false);
      button("Packets").click();
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-packet-card").length).toBe(2));
      for (const [index, packet] of items.entries()) {
        root.querySelectorAll<HTMLElement>(".knowledge-packet-card")[index]!
          .querySelector<HTMLButtonElement>("button")!.click();
        document.querySelector<HTMLButtonElement>('[data-confirm="false"]')!.click();
        await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) =>
          String(url).endsWith("/dissect")).length).toBe(index + 1));
        const mutation = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/dissect"))[index]!;
        expect(JSON.parse(String(mutation[1]?.body)).version_id).toBe(packet.id);
        await vi.waitFor(() => expect(document.querySelector(".knowledge-dialog")).toBeNull());
      }
    } finally { controller.abort(); }
  });

  it("uses the real tag-pool payload rather than a free-text tag field", async () => {
    const { controller } = setup();
    try {
      await vi.waitFor(() => expect(document.querySelector<HTMLSelectElement>(
        "[aria-label='Filter by tag']")?.options.length).toBe(2));
      button("Add fact").click();
      const tag = document.querySelector<HTMLInputElement>(".knowledge-tag-option input")!;
      expect(tag.value).toBe("history");
      expect(document.querySelector(".knowledge-new-tag")?.textContent).toBe("New tag");
    } finally { controller.abort(); }
  });
});
