import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api/client";
import type { AdminCard } from "./api/types";
import { MiniAppShell } from "./app";
import { TelegramPlatform } from "./platform/telegram";
import { Router } from "./routing/router";
import { I18n } from "./i18n";
import { renderAdminManagement } from "./ui/admin-management";

const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const packet = (id: string, name: string): AdminCard => ({
  id, packet_id: "one-logical-packet", name, version_number: id === "v1" ? 1 : 2,
  themes: 1, questions: 5, authors: [{ id: "author", display_name: "Ada" }],
  tournaments: [{ id: "cup", name: "Hidden Cup" }],
});

describe("admin packet loading", () => {
  let shell: MiniAppShell | undefined;
  afterEach(() => {
    shell?.stop();
    shell = undefined;
    document.body.replaceChildren();
    sessionStorage.clear();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function setup() {
    window.history.replaceState({}, "", "/admin/management?section=packets");
    const page = vi.fn<(url: URL, options?: RequestInit) => Promise<Response>>()
      .mockImplementation(async () => response({ items: [packet("v1", "First")], next_cursor: null }));
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (input, options) => {
      const url = new URL(String(input), window.location.origin);
      if (url.pathname.endsWith("/session")) return response({
        csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en",
      });
      if (url.pathname.endsWith("/routes/resolve")) {
        const section = new URL(url.searchParams.get("path")!, window.location.origin)
          .searchParams.get("section") ?? "tournaments";
        return response({ locale: "en", authorization: { allowed: true }, resource: {
          kind: "admin_management", state: "ready", section, items: [],
          packets_deferred: section === "packets",
        } });
      }
      return page(url, options);
    });
    const root = document.createElement("div");
    document.body.append(root);
    const router = new Router();
    shell = new MiniAppShell(root, new ApiClient("signed-init", fetcher), router, new TelegramPlatform(), false);
    shell.start();
    return { root, page, fetcher, router };
  }

  it("loads version pages progressively and retains mounted cards and open details", async () => {
    const { root, page, fetcher } = setup();
    let resolveNext!: (value: Response) => void;
    page.mockResolvedValueOnce(response({ items: [packet("v1", "First")], next_cursor: "v1" }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNext = resolve; }));
    await vi.waitFor(() => expect(root.querySelector('[data-resource-id="v1"]')).not.toBeNull());
    expect(String(fetcher.mock.calls[1]![0])).toContain("include_items=false");
    const first = root.querySelector<HTMLElement>('[data-resource-id="v1"]')!;
    expect(first.textContent).not.toContain("Hidden Cup");
    const details = [...first.querySelectorAll("details")]
      .find((node) => node.querySelector("summary")?.textContent?.startsWith("Tournaments"))!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    expect(details.textContent).toContain("Hidden Cup");
    const button = first.querySelector<HTMLButtonElement>("button")!;
    button.focus();
    await vi.waitFor(() => expect(page).toHaveBeenCalledTimes(2));
    expect(page.mock.calls[1]![0].searchParams.get("cursor")).toBe("v1");
    expect(page.mock.calls[1]![0].searchParams.get("limit")).toBe("25");
    resolveNext(response({ items: [packet("v2", "Second")], next_cursor: null }));
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(2));
    expect(root.querySelector('[data-resource-id="v1"]')).toBe(first);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(button);
    expect(root.querySelector('[aria-busy="true"]')).toBeNull();
  });

  it("preserves nested metadata search with a debounce", async () => {
    const { root, page } = setup();
    page.mockResolvedValueOnce(response({ items: [packet("v1", "First"),
      { ...packet("v2", "Second"), tournaments: [] }], next_cursor: null }));
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(2));
    const search = root.querySelector<HTMLInputElement>('input[type="search"]')!;
    search.value = "Hidden Cup";
    search.dispatchEvent(new Event("input"));
    expect(root.querySelectorAll("article")).toHaveLength(2);
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(1));
    expect(root.querySelector("article")!.dataset.resourceId).toBe("v1");
    expect(page).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sessionStorage.getItem("sitg:miniapp:filters:admin_management_packets")!))
      .toMatchObject({ search: "Hidden Cup" });
  });

  it("aborts in-flight pages on navigation and ignores late results", async () => {
    const { root, page, router } = setup();
    let resolveOld!: (value: Response) => void;
    page.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    await vi.waitFor(() => expect(page).toHaveBeenCalledOnce());
    router.navigate("/admin/management?section=authors");
    await vi.waitFor(() => expect(root.querySelector('[aria-current="page"]')?.textContent).toBe("Authors"));
    expect(page.mock.calls[0]![1]!.signal!.aborted).toBe(true);
    resolveOld(response({ items: [packet("late", "Late")], next_cursor: null }));
    await Promise.resolve();
    expect(root.querySelector('[data-resource-id="late"]')).toBeNull();
  });

  it("retries transient page failures", async () => {
    const { root, page } = setup();
    page.mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(response({ items: [packet("v1", "First")], next_cursor: null }));
    await vi.waitFor(() => expect(page).toHaveBeenCalledTimes(1));
    expect(root.querySelectorAll("article")).toHaveLength(0);
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(1), { timeout: 2000 });
    expect(page).toHaveBeenCalledTimes(2);
  });

  it("stops loading after an authorization failure", async () => {
    const { root, page } = setup();
    page.mockResolvedValueOnce(response({ error: { code: "forbidden" } }, 403));
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')).not.toBeNull());
    expect(page).toHaveBeenCalledTimes(1);
    expect(page.mock.calls[0]![1]!.signal!.aborted).toBe(true);
    expect(root.querySelector("article")).toBeNull();
  });

  it("uses the displayed version ID for administrative downloads", async () => {
    const { root, page, fetcher } = setup();
    page.mockImplementation(async (_url, options) => options?.method === "POST"
      ? response({ queued: true })
      : response({ items: [packet("v1", "First"), packet("v2", "Second")], next_cursor: null }));
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(2));
    const card = root.querySelector('[data-resource-id="v2"]')!;
    [...card.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent === "Download")!.click();
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) =>
      String(url).endsWith("/packets/v2/download"))).toBe(true));
    const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/packets/v2/download"))!;
    expect(JSON.parse(String(call[1]!.body))).toEqual({ confirm: false });
  });

  it("renders packet relations on demand and reuses cards across unchanged updates", () => {
    const item = { id: "cup", name: "Cup", packets: Array.from({ length: 200 }, (_, i) => ({
      id: `v${i}`, name: `Nested packet ${i}`,
    })) };
    const view = renderAdminManagement({ kind: "admin_management", state: "ready",
      section: "tournaments", items: [item] }, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    const card = view.querySelector("article")!;
    expect(card.textContent).not.toContain("Nested packet");
    const details = [...card.querySelectorAll("details")].find((node) =>
        node.querySelector("summary")?.textContent?.includes("(200)"))!;
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    expect(card.textContent).toContain("Nested packet 199");
    view.update([structuredClone(item)], true);
    expect(view.querySelector("article")).toBe(card);
    expect(details.open).toBe(true);
  });
});
