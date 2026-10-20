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
  sharing: { grants: [], shared: [] }, dissection: { packets: [{ id: "packet", name: "Packet" }], requests: [], next_cursor: "packet-page" },
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
function button(root: HTMLElement, text: string): HTMLButtonElement {
  const found = [...root.querySelectorAll("button")].find((node) => node.textContent === text);
  expect(found, `button ${text}`).toBeDefined();
  return found!;
}

describe("Knowledge Mini App window", () => {
  it("resolves the exact bot launch path including the cache transition query", () => {
    const route = matchRoute({ pathname: "/knowledge", search: "?_launch=1" });
    expect(route.id).toBe("knowledge"); expect(route.titleKey).toBe("knowledge.title");
    expect(routeRequestPath(route)).toBe("/knowledge?_launch=1");
  });

  it.each(["en", "ru"] as const)("opens the existing localized library, creates/edits/deletes and loads filters (%s)", async (locale) => {
    const i18n = new I18n(locale);
    let detail: KnowledgeFactDetail = { id: "fact", title: "<History>", description: "Description", tags: [{ id: "tag", name: "Past" }],
      links: [], origin: "manual", content_version: 1, editable: true, statistics: { status: "none" } };
    const resource = empty();
    const fetcher = vi.fn<typeof fetch>(async (input, options) => {
      const path = String(input);
      if (path === "/api/miniapp/session") return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale });
      if (path.includes("/routes/resolve")) return reply({ locale, authorization: { allowed: true }, resource: { kind: "knowledge", state: "ready", ...resource } });
      if (path.endsWith("/entitlements")) return reply({ features: {} });
      if (path.includes("/facts?")) return reply(resource.facts);
      if (path.endsWith("/facts/fact")) return reply(detail);
      if (path.endsWith("/create") || path.endsWith("/update")) {
        const body = JSON.parse(String(options?.body)); detail = { ...detail, ...body, content_version: detail.content_version + 1 };
        resource.facts = { ...resource.facts, state: "ready", total: 1, items: [{ ...detail, tag_names: ["Past"], updated_at: "2026-01-01" }] };
        return reply(detail);
      }
      if (path.endsWith("/delete")) { resource.facts = empty().facts; return reply({}); }
      throw new Error(`Unexpected request: ${path}`);
    });
    const { root } = start(fetcher);
    await vi.waitFor(() => expect(root.querySelector(".knowledge-empty")?.textContent).toBe(i18n.t("knowledge.empty")));
    expect(root.querySelector("h1")?.textContent).toBe(i18n.t("knowledge.title"));
    expect(root.textContent).toContain(i18n.t("knowledge.sharing.permanent"));
    expect(root.querySelector('a[href="/training"]')).not.toBeNull();
    const form = root.querySelector<HTMLFormElement>(".knowledge-library > form")!;
    form.querySelector("input")!.value = "<History>"; form.querySelector("textarea")!.value = "Description";
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector(".knowledge-fact-open")?.textContent).toBe("<History>"));
    expect(root.querySelector("history")).toBeNull();
    root.querySelector<HTMLButtonElement>(".knowledge-fact-open")!.click();
    await vi.waitFor(() => expect(root.querySelector(".knowledge-details form")).not.toBeNull());
    const edit = root.querySelector<HTMLFormElement>(".knowledge-details form")!;
    edit.querySelector("input")!.value = "Updated"; edit.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector(".knowledge-fact-open")?.textContent).toBe("Updated"));
    const update = fetcher.mock.calls.find(([path]) => String(path).endsWith("/update"))!;
    expect(JSON.parse(String(update[1]?.body))).toMatchObject({ fact_id: "fact", expected_version: 2, title: "Updated" });
    const search = root.querySelector<HTMLInputElement>('input[type="search"]')!;
    search.value = "History"; search.dispatchEvent(new Event("input")); search.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(search.disabled).toBe(false));
    expect(fetcher.mock.calls.some(([path]) => String(path).includes("search=History"))).toBe(true);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    button(root, i18n.t("knowledge.delete")).click();
    await vi.waitFor(() => expect(root.querySelector(".knowledge-empty")).not.toBeNull());
    const writes = fetcher.mock.calls.filter(([path, options]) => options?.method === "POST" && path !== "/api/miniapp/session");
    expect(writes).toHaveLength(3);
    for (const [, options] of writes) {
      const headers = new Headers(options?.headers);
      expect(headers.get("X-CSRF-Token")).toBe("csrf-fixture-token");
      expect(headers.get("X-Idempotency-Key")).toBeTruthy(); expect(options?.credentials).toBe("include");
    }
  });

  it("keeps existing permanent-copy sharing and dissection confirmation API contracts", async () => {
    const i18n = new I18n("en");
    const fetcher = vi.fn<typeof fetch>(async (input, options) => {
      const path = String(input);
      if (path.endsWith("/session")) return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (path.includes("/routes/resolve")) return reply({ locale: "en", authorization: { allowed: true }, resource: { kind: "knowledge", ...empty() } });
      if (path.endsWith("/entitlements")) return reply({ features: {} });
      if (path.endsWith("/grant")) return reply({});
      if (path.endsWith("/sharing")) return reply({ grants: [{ id: "grant", recipient_name: "Recipient", copied_count: 2 }], shared: [] });
      if (path.endsWith("/dissect")) return reply({ confirmation_required: !JSON.parse(String(options?.body)).confirm_exposure });
      if (path.includes("/training/packets?cursor=")) return reply({ items: [{ id: "packet-2", name: "Second packet" }], next_cursor: null });
      throw new Error(`Unexpected request: ${path}`);
    });
    const { root } = start(fetcher);
    await vi.waitFor(() => expect(root.querySelector(".knowledge-sharing")).not.toBeNull());
    const recipient = root.querySelector<HTMLInputElement>(`input[aria-label="${i18n.t("knowledge.sharing.recipient")}"]`)!;
    recipient.value = "Recipient"; button(root, i18n.t("knowledge.sharing.grant")).click();
    await vi.waitFor(() => expect(root.querySelector(".knowledge-sharing ul")?.textContent).toContain("Recipient"));
    const grant = fetcher.mock.calls.find(([path]) => String(path).endsWith("/grant"))!;
    expect(JSON.parse(String(grant[1]?.body))).toEqual({ recipient: "Recipient" });
    button(root, i18n.t("knowledge.more_packets")).click();
    await vi.waitFor(() => expect(root.querySelectorAll(".knowledge-dissection option")).toHaveLength(2));
    root.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${i18n.t("knowledge.dissection.instructions")}"]`)!.value = "Before 1500";
    root.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${i18n.t("knowledge.dissection.comment")}"]`)!.value = "More detail";
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    button(root, i18n.t("knowledge.dissection.submit")).click();
    await vi.waitFor(() => expect(fetcher.mock.calls.filter(([path]) => String(path).endsWith("/dissect"))).toHaveLength(2));
    expect(confirm).toHaveBeenCalledWith(i18n.t("knowledge.confirm_exposure"));
    const bodies = fetcher.mock.calls.filter(([path]) => String(path).endsWith("/dissect")).map(([, options]) => JSON.parse(String(options?.body)));
    expect(bodies).toEqual([false, true].map((confirm_exposure) => ({ version_id: "packet", instructions: "Before 1500", comment: "More detail", confirm_exposure })));
  });

  it("shows loading, then a recoverable library read error without discarding the window", async () => {
    let resolve!: (value: Response) => void;
    const initial = new Promise<Response>((done) => { resolve = done; });
    const fetcher = vi.fn<typeof fetch>(async (input) => {
      const path = String(input);
      if (path.endsWith("/session")) return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (path.includes("/routes/resolve")) return initial;
      if (path.endsWith("/entitlements")) return reply({ features: {} });
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
