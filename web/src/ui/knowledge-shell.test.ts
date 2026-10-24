import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { MiniAppShell } from "../app";
import { I18n } from "../i18n";
import type { MiniAppPlatform } from "../platform/telegram";
import { Router } from "../routing/router";
import { matchRoute, routeRequestPath } from "../routing/routes";
import type { KnowledgeFactDetail, KnowledgeViewData } from "./knowledge";

const shells: MiniAppShell[] = [];
afterEach(() => { shells.splice(0).forEach((shell) => shell.stop()); document.body.replaceChildren(); vi.restoreAllMocks(); });
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const empty = (): KnowledgeViewData => ({
  facts: { state: "empty", items: [], total: 0, next_cursor: null, order: "title_asc" },
  sharing: { grants: [], shared: [], links: [] }, dissection: { packets: [], requests: [], next_cursor: null },
});
function start(fetcher: typeof fetch, initData = "fixture-init-data") {
  window.history.replaceState({}, "", "/knowledge?_launch=1");
  const root = document.createElement("div"); document.body.append(root);
  const platform: MiniAppPlatform = { initData, isTelegram: true, initialize: vi.fn(), destroy: vi.fn(),
    setBackHandler: vi.fn(), setMainAction: vi.fn(), notifySuccess: vi.fn(), notifyError: vi.fn(), returnToBot: vi.fn() };
  const shell = new MiniAppShell(root, new ApiClient(initData, fetcher), new Router(), platform, false);
  shells.push(shell); shell.start();
  return { root, platform, shell };
}
function button(root: ParentNode, text: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find((node) => node.textContent === text);
  expect(found, `button ${text}`).toBeDefined();
  return found!;
}

describe("Knowledge Mini App shell with redesigned dialogs", () => {
  it("resolves the exact bot launch path including the cache transition query", () => {
    const route = matchRoute({ pathname: "/knowledge", search: "?_launch=1" });
    expect(route.id).toBe("knowledge"); expect(route.titleKey).toBe("knowledge.title");
    expect(routeRequestPath(route)).toBe("/knowledge?_launch=1");
  });

  it.each(["en", "ru"] as const)("authenticates and creates/edits/deletes with versioned writes (%s)", async (locale) => {
    const i18n = new I18n(locale);
    let detail: KnowledgeFactDetail = { id: "fact", title: "<History>", description: "Description", tags: [],
      links: [], origin: "manual", content_version: 1, editable: true, statistics: { status: "none" } };
    const resource = empty();
    const fetcher = vi.fn<typeof fetch>(async (input, options) => {
      const path = String(input);
      if (path.endsWith("/session")) return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale });
      if (path.includes("/routes/resolve")) return reply({ locale, authorization: { allowed: true }, resource: { kind: "knowledge", state: "ready", ...resource } });
      if (path.endsWith("/entitlements")) return reply({ features: {} });
      if (path.endsWith("/tags")) return reply({ items: [] });
      if (path.includes("/facts?")) return reply(resource.facts);
      if (path.endsWith("/facts/fact")) return reply(detail);
      if (path.endsWith("/create") || path.endsWith("/update")) {
        const body = JSON.parse(String(options?.body)); detail = { ...detail, ...body, content_version: detail.content_version + 1 };
        resource.facts = { ...resource.facts, state: "ready", total: 1, items: [{ ...detail, tag_names: [], updated_at: "2026-01-01" }] };
        return reply(detail);
      }
      if (path.endsWith("/delete")) { resource.facts = empty().facts; return reply({}); }
      throw new Error(`Unexpected request: ${path}`);
    });
    const { root } = start(fetcher);
    await vi.waitFor(() => expect(root.querySelector(".knowledge-empty")?.textContent).toBe(i18n.t("knowledge.empty")));
    expect(root.querySelector("h1")?.textContent).toBe(i18n.t("knowledge.title"));
    // No cross-navigation link to Training at the top of the page.
    expect(root.querySelector('a[href="/training"]')).toBeNull();
    // The active section is highlighted immediately on first render in both locales.
    const firstActive = [...root.querySelectorAll<HTMLButtonElement>(".knowledge-tab")]
      .find((tab) => tab.getAttribute("aria-current") === "page");
    expect(firstActive?.textContent).toBe(i18n.t("knowledge.tab.library"));
    expect(firstActive?.className).toContain("primary-button");
    button(root, i18n.t("knowledge.create")).click();
    const form = document.querySelector<HTMLFormElement>(".knowledge-dialog form")!;
    form.querySelector("input")!.value = "<History>"; form.querySelector("textarea")!.value = "Description";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector(".knowledge-fact-title")?.textContent).toBe("<History>"));
    expect(root.querySelector("history")).toBeNull();
    root.querySelector<HTMLButtonElement>(".knowledge-fact-header")!.click();
    await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>(".knowledge-fact-edit")!.hidden).toBe(false));
    root.querySelector<HTMLButtonElement>(".knowledge-fact-edit")!.click();
    const edit = document.querySelector<HTMLFormElement>(".knowledge-dialog form")!;
    edit.querySelector("input")!.value = "Updated";
    edit.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector(".knowledge-fact-title")?.textContent).toBe("Updated"));
    const update = fetcher.mock.calls.find(([path]) => String(path).endsWith("/update"))!;
    expect(JSON.parse(String(update[1]?.body))).toMatchObject({ fact_id: "fact", expected_version: 2, title: "Updated" });
    const search = root.querySelector<HTMLInputElement>('.knowledge-controls input[type="search"]')!;
    search.value = "History"; search.dispatchEvent(new Event("input")); search.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) => String(path).includes("search=History"))).toBe(true));
    // Reopen the current detail/editor, then delete through the explicit editor action.
    root.querySelector<HTMLButtonElement>(".knowledge-fact-header")!.click();
    await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>(".knowledge-fact-edit")!.hidden).toBe(false));
    root.querySelector<HTMLButtonElement>(".knowledge-fact-edit")!.click();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    button(document, i18n.t("knowledge.delete")).click();
    await vi.waitFor(() => expect(root.querySelector(".knowledge-empty")).not.toBeNull());
    const writes = fetcher.mock.calls.filter(([path, options]) => options?.method === "POST" && path !== "/api/miniapp/session");
    expect(writes).toHaveLength(3);
    for (const [, options] of writes) {
      const headers = new Headers(options?.headers);
      expect(headers.get("X-CSRF-Token")).toBe("csrf-fixture-token");
      expect(headers.get("X-Idempotency-Key")).toBeTruthy(); expect(options?.credentials).toBe("include");
    }
  });

  it("shows loading and a recoverable read error without discarding the window", async () => {
    let resolve!: (value: Response) => void;
    const initial = new Promise<Response>((done) => { resolve = done; });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path.endsWith("/session")) return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (path.includes("/routes/resolve")) return initial;
      if (path.endsWith("/entitlements")) return reply({ features: {} });
      if (path.endsWith("/tags")) return reply({ items: [] });
      return reply({ error: { code: "internal_error" } }, 500);
    });
    const { root } = start(fetcher);
    expect(root.textContent).toContain(new I18n("ru").t("common.loading"));
    resolve(reply({ locale: "en", authorization: { allowed: true }, resource: { kind: "knowledge", ...empty() } }));
    await vi.waitFor(() => expect(root.querySelector(".knowledge-empty")).not.toBeNull());
    button(root, new I18n("en").t("knowledge.refresh")).click();
    await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe(new I18n("en").t("knowledge.error")));
    expect(root.querySelector(".knowledge-library")).not.toBeNull();
  });

  it("does not turn a missing Telegram session into public library access", async () => {
    const fetcher = vi.fn<typeof fetch>();
    const { root } = start(fetcher, "");
    await vi.waitFor(() => expect(root.textContent).toContain(new I18n("ru").t("error.authentication_required")));
    expect(fetcher).not.toHaveBeenCalled(); expect(root.querySelector(".knowledge-library")).toBeNull();
  });
});
