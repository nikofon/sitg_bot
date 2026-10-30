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
    // The card carries a trash-icon delete control with a localized accessible name.
    const cardDelete = root.querySelector<HTMLButtonElement>(".knowledge-fact-delete")!;
    expect(cardDelete.hidden).toBe(false);
    expect(cardDelete.getAttribute("aria-label")).toBe(i18n.t("knowledge.fact.delete_label", { title: "<History>" }));
    expect(cardDelete.querySelector("svg.icon-trash")).not.toBeNull();
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

  it("wires the dissection composer and pending import review to their contracts", async () => {
    const i18n = new I18n("en");
    const resource = empty();
    const packetCard = {
      id: "packet-1", name: "Cup 2026", year: 2026, scope: "training" as const,
      authors: [], tournaments: [], total_theme_count: 2, fresh_theme_count: 2,
      dissected_theme_count: 0, dissected: false, best_buzz_calculated: false,
    };
    const themeDetail = {
      theme_revision_id: "theme-1", name: "Naval history", author: "Ada", commentary: null,
      questions: [{ revision_id: "q-1", value: 10, text: "Question text", answer: "Answer",
        accepted_answers: [], rejected_answers: [], commentary: null }],
    };
    const importCard = {
      id: "import-1", type: "dissection" as const, author: "Me", status: "pending" as const,
      fact_count: 2, packet_name: "Cup 2026", created_at: "2026-10-02T12:00:00Z", request_id: "req-1",
    };
    const importDetail = {
      ...importCard, approved_count: 0,
      sections: {
        new: [{ id: "c-1", title: "New fact", description: "d", author: "Ada",
          links: [], images: [], tag_names: [], selected: true }],
        potential_duplicates: [{ id: "c-2", title: "Maybe existing", description: "d", author: "Ada",
          links: [], images: [], tag_names: [], selected: true }],
        duplicates: [{ id: "c-3", title: "Existing", description: "d", author: "Ada",
          links: [], images: [], tag_names: [], selected: false }],
      },
    };
    const dissectBodies: unknown[] = [];
    const previewBodies: unknown[] = [];
    let importsReads = 0;
    const fetcher = vi.fn<typeof fetch>(async (input, options) => {
      const path = String(input);
      if (path.endsWith("/session")) return reply({ csrf_token: "csrf-fixture-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (path.includes("/routes/resolve")) return reply({ locale: "en", authorization: { allowed: true }, resource: { kind: "knowledge", state: "ready", ...resource } });
      if (path.endsWith("/entitlements")) return reply({ features: {} });
      if (path.endsWith("/knowledge/tags")) return reply({ items: [] });
      if (path.includes("/knowledge/facts?")) return reply(resource.facts);
      if (path.includes("/training/packets?")) return reply({ items: [packetCard], next_cursor: null });
      if (path.includes("/knowledge/themes?")) return reply({
        version_id: "packet-1", packet_name: "Cup 2026",
        themes: [{ theme_revision_id: "theme-1", name: "Naval history" }],
        quota: { enabled: true, remaining: 1, reset_at: null },
      });
      if (path.endsWith("/knowledge/theme_preview")) {
        const body = JSON.parse(String(options?.body));
        previewBodies.push(body);
        return body.confirm_exposure
          ? reply({ confirmation_required: false, theme: themeDetail })
          : reply({ confirmation_required: true, fresh_unit_count: 2 });
      }
      if (path.endsWith("/knowledge/dissect")) {
        const body = JSON.parse(String(options?.body));
        dissectBodies.push(body);
        return body.confirm_exposure
          ? reply({ dissection_id: "dis-1", import_id: "import-1" })
          : reply({ confirmation_required: true });
      }
      if (path.includes("/knowledge/imports/import-1")) return reply(importDetail);
      if (path.includes("/knowledge/imports")) { importsReads += 1; return reply({ items: [importCard] }); }
      if (path.endsWith("/knowledge/import_decide")) {
        return reply({ import_id: "import-1", status: "approved", approved_count: 2, skipped_count: 1 });
      }
      if (path.includes("/knowledge/dissections")) return reply({ items: [] });
      throw new Error(`Unexpected request: ${path}`);
    });
    const { root } = start(fetcher);
    await vi.waitFor(() => expect(root.querySelector(".knowledge-library")).not.toBeNull());

    // Composer: Dissect opens the window, themes load as metadata only.
    button(root, i18n.t("knowledge.tab.packets")).click();
    const dissect = await vi.waitFor(() => button(root, i18n.t("knowledge.dissection.submit_short")));
    dissect.click();
    const dialog = await vi.waitFor(() => document.querySelector<HTMLDialogElement>(".knowledge-dialog")!);
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([path]) =>
      String(path).includes("/knowledge/themes?version_id=packet-1"))).toBe(true));

    // Theme preview goes through the explicit exposure confirmation.
    button(dialog, i18n.t("knowledge.dissection.tab.themes")).click();
    const themeRow = await vi.waitFor(() => {
      const row = dialog.querySelector<HTMLElement>("[data-theme-id='theme-1']");
      expect(row).not.toBeNull();
      return row!;
    });
    themeRow.querySelector<HTMLButtonElement>(".knowledge-theme-toggle")!.click();
    await vi.waitFor(() => expect(previewBodies).toHaveLength(1));
    expect(previewBodies[0]).toMatchObject({ version_id: "packet-1", theme_revision_id: "theme-1", confirm_exposure: false });
    button(dialog, i18n.t("knowledge.dissection.confirm_theme")).click();
    await vi.waitFor(() => expect(previewBodies).toHaveLength(2));
    expect(previewBodies[1]).toMatchObject({ confirm_exposure: true });
    expect(dialog.textContent).toContain("Question text");

    // Select the theme, submit commentary, and confirm the exposure warning.
    dialog.querySelector<HTMLInputElement>("[data-theme-id='theme-1'] input[type=checkbox]")!.click();
    button(dialog, i18n.t("knowledge.dissection.tab.general")).click();
    const comment = [...dialog.querySelectorAll("textarea")]
      .find((node) => node.getAttribute("aria-label") === i18n.t("knowledge.dissection.comment"))!;
    comment.value = "Extract naval terms";
    button(dialog, i18n.t("knowledge.dissection.submit")).click();
    await vi.waitFor(() => expect(dissectBodies).toHaveLength(1));
    expect(dissectBodies[0]).toMatchObject({
      version_id: "packet-1", theme_revision_ids: ["theme-1"], tag_ids: [],
      comment: "Extract naval terms", confirm_exposure: false,
    });
    button(dialog, i18n.t("knowledge.dissection.confirm")).click();
    await vi.waitFor(() => expect(document.contains(dialog)).toBe(false));
    expect(dissectBodies[1]).toMatchObject({ confirm_exposure: true });

    // Pending imports: the staged dissection appears and is reviewed.
    button(root, i18n.t("knowledge.tab.imports")).click();
    await vi.waitFor(() => expect(importsReads).toBeGreaterThanOrEqual(1));
    expect(root.querySelector('[data-panel="imports"]')!.textContent).toContain("Cup 2026");
    button(root, i18n.t("knowledge.imports.review")).click();
    const review = await vi.waitFor(() => document.querySelector<HTMLDialogElement>(".knowledge-dialog")!);
    await vi.waitFor(() => expect(review.textContent).toContain(i18n.t("knowledge.imports.section.duplicates")));
    const duplicate = review.querySelector<HTMLInputElement>("[data-candidate-id='c-3'] input")!;
    expect(duplicate.checked).toBe(false);
    // Exact duplicates stay deselected even after a manual reselect round-trip.
    duplicate.click();
    duplicate.click();
    button(review, i18n.t("knowledge.imports.add_selected")).click();
    await vi.waitFor(() => {
      const decide = fetcher.mock.calls.find(([path, options]) =>
        String(path).endsWith("/knowledge/import_decide") && options?.method === "POST");
      expect(decide).toBeDefined();
      expect(JSON.parse(String(decide![1]!.body))).toMatchObject({
        import_id: "import-1", decision: "approve", selected_candidate_ids: ["c-1", "c-2"],
      });
    });
    await vi.waitFor(() => expect(document.contains(review)).toBe(false));
    // Approval refreshes the library and the pending import cards.
    await vi.waitFor(() => expect(importsReads).toBeGreaterThanOrEqual(2));
    expect(fetcher.mock.calls.some(([path]) => String(path).includes("/knowledge/facts?"))).toBe(true);

    // Every knowledge mutation carries the session CSRF and idempotency headers.
    const writes = fetcher.mock.calls.filter(([path, options]) =>
      options?.method === "POST" && String(path).startsWith("/api/miniapp/knowledge/"));
    expect(writes.length).toBeGreaterThanOrEqual(3);
    for (const [, options] of writes) {
      const headers = new Headers(options?.headers);
      expect(headers.get("X-CSRF-Token")).toBe("csrf-fixture-token");
      expect(headers.get("X-Idempotency-Key")).toBeTruthy();
    }
  });
});
