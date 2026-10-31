import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { I18n } from "../i18n";
import { renderKnowledgePage } from "./knowledge-page";
import type { KnowledgeImportCard, KnowledgeViewData, ShareLink } from "./knowledge";

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
  it("forwards import cursors through authenticated requests and opens older reviews", async () => {
    const newest: KnowledgeImportCard = {
      id: "newest", type: "collection", author: "Sender", status: "approved", fact_count: 0,
      packet_name: null, request_id: null, created_at: "2026-10-02T00:00:00Z",
    };
    const oldest = { ...newest, id: "oldest", status: "pending" as const };
    const cursor = "timestamp/identity+==";
    const responses: Record<string, unknown> = {
      "/api/miniapp/knowledge/imports": { items: [newest], next_cursor: cursor },
      "/api/miniapp/knowledge/imports/oldest": {
        ...oldest, sections: { new: [], potential_duplicates: [], duplicates: [] }, approved_count: 0,
      },
    };
    const { root, fetcher, controller } = setup(responses);
    try {
      button("Pending imports").click();
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(1));
      responses["/api/miniapp/knowledge/imports"] = { items: [oldest], next_cursor: null };
      root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!.click();
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-import-card")).toHaveLength(2));
      const requests = fetcher.mock.calls.filter(([url]) =>
        String(url).startsWith("/api/miniapp/knowledge/imports?"));
      expect(requests).toHaveLength(2);
      expect(new URL(String(requests[0]![0]), window.location.origin).searchParams.get("cursor"))
        .toBeNull();
      expect(new URL(String(requests[1]![0]), window.location.origin).searchParams.get("cursor"))
        .toBe(cursor);
      expect(requests[1]![1]?.credentials).toBe("include");
      expect(root.querySelector<HTMLButtonElement>(".knowledge-imports-more")!.hidden).toBe(true);
      root.querySelector<HTMLButtonElement>(
        '[data-import-id="oldest"] .knowledge-import-review')!.click();
      await vi.waitFor(() => expect(document.querySelector("[data-import-approve]")).not.toBeNull());
    } finally { controller.abort(); }
  });

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
      "/api/miniapp/knowledge/themes": {
        version_id: "version", packet_name: "Packet",
        themes: [{ theme_revision_id: "theme-1", name: "Selected theme" }],
        quota: { enabled: true, remaining: 24, reset_at: null },
      },
    });
    try {
      expect(fetcher.mock.calls.some(([url]) => String(url).includes("/training/packets"))).toBe(false);
      button("Packets").click();
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-packet-card").length).toBe(2));
      for (const [index, packet] of items.entries()) {
        root.querySelectorAll<HTMLElement>(".knowledge-packet-card")[index]!
          .querySelector<HTMLButtonElement>("button")!.click();
        await vi.waitFor(() => expect(document.querySelector<HTMLInputElement>(
          '[data-theme-id="theme-1"] input[type="checkbox"]')).not.toBeNull());
        document.querySelector<HTMLInputElement>(
          '[data-theme-id="theme-1"] input[type="checkbox"]')!.click();
        document.querySelector<HTMLButtonElement>('[data-confirm="false"]')!.click();
        await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) =>
          String(url).endsWith("/dissect")).length).toBe(index + 1));
        const mutation = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/dissect"))[index]!;
        expect(JSON.parse(String(mutation[1]?.body)).version_id).toBe(packet.id);
        await vi.waitFor(() => expect(document.querySelector(".knowledge-dialog")).toBeNull());
      }
    } finally { controller.abort(); }
  });

  it("uses the real tag-pool payload as additive multi-tag filter chips and repeats tags in the query", async () => {
    const { root, fetcher, controller } = setup({
      "/api/miniapp/knowledge/tags": { items: [
        { tag_id: "tag-1", name: "history", colour: "#1565c0" },
        { tag_id: "tag-2", name: "science", colour: "#2e7d32" },
      ] },
    });
    try {
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-tag-filter-chip").length).toBe(2));
      button("Add fact").click();
      const tag = document.querySelector<HTMLInputElement>(".knowledge-tag-option input")!;
      expect(tag.value).toBe("history");
      expect(document.querySelector(".knowledge-new-tag")?.textContent).toBe("New tag");
      document.querySelector<HTMLButtonElement>(".knowledge-dialog-close")!.click();

      const chips = [...root.querySelectorAll<HTMLButtonElement>(".knowledge-tag-filter-chip")];
      chips.find((chip) => chip.textContent === "history")!.click();
      await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => {
        const path = String(url);
        return path.includes("/knowledge/facts?") && path.includes("tags=history");
      })).toBe(true));
      chips.find((chip) => chip.textContent === "science")!.click();
      await vi.waitFor(() => {
        const query = fetcher.mock.calls.map(([url]) => String(url))
          .filter((url) => url.includes("/knowledge/facts?")).at(-1)!;
        expect(query).toContain("tags=history");
        expect(query).toContain("tags=science");
        expect(query.match(/tags=/g)).toHaveLength(2);
      });
    } finally { controller.abort(); }
  });

  it("uploads fact images as bounded base64 JSON and saves their ids", async () => {
    const image = { id: "image-1", url: "/api/miniapp/knowledge/images/image-1", content_type: "image/png" };
    const { fetcher, controller } = setup({
      "/api/miniapp/knowledge/image_upload": image,
      "/api/miniapp/knowledge/create": {
        id: "fact-1", title: "Pictured", description: "d", links: [], tags: [],
        origin: "manual", content_version: 1, editable: true, statistics: { status: "none" },
      },
    });
    try {
      button("Add fact").click();
      const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
      const form = dialog.querySelector<HTMLFormElement>("form")!;
      form.querySelector<HTMLInputElement>("input")!.value = "Pictured";
      form.querySelector("textarea")!.value = "d";
      const picker = form.querySelector<HTMLInputElement>(".knowledge-images-field input[type='file']")!;
      expect(picker.getAttribute("aria-label")).toBe("Images");
      const file = new File([new Uint8Array([1, 2, 3])], "pic.png", { type: "image/png" });
      Object.defineProperty(picker, "files", { value: [file] });
      picker.dispatchEvent(new Event("change", { bubbles: true }));
      await vi.waitFor(() => expect(dialog.querySelector(".knowledge-image img")?.getAttribute("src"))
        .toBe(image.url));
      const upload = fetcher.mock.calls.find(([url]) => String(url).endsWith("/image_upload"))!;
      expect(upload[1]?.method).toBe("POST");
      expect(JSON.parse(String(upload[1]?.body))).toEqual({ source_base64: "AQID" });

      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) =>
        String(url).endsWith("/knowledge/create"))).toBe(true));
      const create = fetcher.mock.calls.find(([url]) => String(url).endsWith("/knowledge/create"))!;
      expect(JSON.parse(String(create[1]?.body))).toMatchObject({ title: "Pictured", image_ids: ["image-1"] });
    } finally { controller.abort(); }
  });

  it("sends owner_uploads_only as a packets filter and clears it with reset", async () => {
    const items = [{
      id: "version-1", packet_id: "logical-packet", name: "Mine", year: 2026, scope: "training",
      authors: [], tournaments: [], total_theme_count: 1, fresh_theme_count: 1,
      dissected_theme_count: 0, dissected: false, best_buzz_calculated: false,
    }];
    const { root, fetcher, controller } = setup({
      "/api/miniapp/training/packets": { items, next_cursor: null },
    });
    try {
      button("Packets").click();
      await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-packet-card").length).toBe(1));
      const owner = root.querySelector<HTMLInputElement>(".knowledge-owner-filter input")!;
      expect(owner.type).toBe("checkbox");
      owner.checked = true;
      owner.dispatchEvent(new Event("change", { bubbles: true }));
      await vi.waitFor(() => {
        const query = fetcher.mock.calls.map(([url]) => String(url))
          .filter((url) => url.includes("/training/packets?")).at(-1)!;
        expect(query).toContain("owner_uploads_only=true");
      });
      button("Reset filters").click();
      await vi.waitFor(() => {
        expect(root.querySelector<HTMLInputElement>(".knowledge-owner-filter input")!.checked).toBe(false);
        const query = fetcher.mock.calls.map(([url]) => String(url))
          .filter((url) => url.includes("/training/packets?")).at(-1)!;
        expect(query).not.toContain("owner_uploads_only");
      });
    } finally { controller.abort(); }
  });
});
