import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { I18n } from "../i18n";
import { renderAdminAi } from "./admin-ai";

const SETTINGS_PATH = "/api/miniapp/admin/ai/settings";
const PROVIDER_PATH = "/api/miniapp/admin/ai/provider";
const CHECK_PATH = "/api/miniapp/admin/ai/provider/check";
const PLAYERS_PATH = "/api/miniapp/admin/ai/players";
const TIER_PATH = "/api/miniapp/admin/ai/tier";
const RESET_PATH = "/api/miniapp/admin/ai/reset";

const settings = {
  version: 5,
  dissection_enabled: false,
  best_buzz_enabled: true,
  free: { dissection: 0, best_buzz: null },
  premium: { dissection: 24, best_buzz: null },
  reset_period: "day",
  reset_at: "2026-05-04T00:00:00Z",
  quota_generation: 3,
};
const provider = {
  provider: "openai",
  model: "gpt-test",
  configured: true,
  state: "responding",
  checked_at: "2026-05-03T12:00:00Z",
  verified_at: "2026-05-03T12:00:00Z",
  error: null,
  check_available_at: null,
  check_cost_notice: "may incur a small provider charge",
};

const reply = (data: unknown, status = 200): Response => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json" },
});

interface Call { path: string; method: string; body: Record<string, unknown> | null; url: string }

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settled) => { resolve = settled; });
  return { promise, resolve };
}

function setup(handlers: Record<string, (call: Call) => Response | Promise<Response>> = {}) {
  const controller = new AbortController();
  const calls: Call[] = [];
  const fetcher = vi.fn<typeof fetch>(async (url, options) => {
    const path = new URL(String(url), "https://app.local").pathname;
    const call: Call = {
      path, method: options?.method ?? "GET",
      body: options?.body ? JSON.parse(String(options.body)) : null, url: String(url),
    };
    calls.push(call);
    if (path === "/api/miniapp/session") return reply({
      csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en",
    });
    const handler = handlers[`${call.method} ${path}`];
    if (handler) return handler(call);
    throw new Error(`Unexpected ${call.method} ${url}`);
  });
  const api = new ApiClient("signed", fetcher);
  const actions = { reload: vi.fn(), confirm: vi.fn(() => true), message: vi.fn() };
  const navigate = vi.fn();
  const root = renderAdminAi(api, new I18n("en"), controller.signal, navigate, actions);
  const view = { controller, calls, fetcher, actions, navigate, root };
  const save = (): void => root.querySelector<HTMLFormElement>("form")!.requestSubmit();
  const button = (text: string): HTMLButtonElement =>
    [...root.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const quota = (label: string): { number: HTMLInputElement; unlimited: HTMLInputElement } => {
    const row = [...root.querySelectorAll<HTMLDivElement>(".admin-ai-quota")]
      .find((item) => item.textContent!.includes(label))!;
    return {
      number: row.querySelector<HTMLInputElement>("input[type=number]")!,
      unlimited: row.querySelector<HTMLInputElement>("input[type=checkbox]")!,
    };
  };
  return { ...view, save, button, quota };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("admin AI functionality tab", () => {
  it("renders server settings once on open, distinguishing zero from Unlimited, without writes", async () => {
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    // Zero forbids usage; null renders as the explicit Unlimited switch.
    expect(view.quota("Dissection · Free").number.value).toBe("0");
    expect(view.quota("Dissection · Free").unlimited.checked).toBe(false);
    expect(view.quota("Best buzz · Free").unlimited.checked).toBe(true);
    expect(view.quota("Dissection · Premium").number.value).toBe("24");
    expect(view.root.querySelector<HTMLSelectElement>("select")!.value).toBe("day");
    expect(view.root.textContent).toContain("openai · gpt-test");
    expect(view.root.textContent).toContain("Responding");
    expect(view.root.textContent).toContain("Quota reset period");
    // Disabled policies stay disabled until an administrator saves.
    expect([...view.root.querySelectorAll<HTMLInputElement>("input[type=checkbox]")].some((input) => input.checked)).toBe(true);
    const posts = view.calls.filter((call) => call.method === "POST" && call.path !== "/api/miniapp/session");
    expect(posts).toEqual([]);
    // GET-only provider observation: exactly one provider read, no check call.
    expect(view.calls.filter((call) => call.path === PROVIDER_PATH)).toHaveLength(1);
  });

  it("saves the full editable replacement with expected_version and applies the returned settings", async () => {
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`POST ${SETTINGS_PATH}`]: (call) => reply({ ...settings, ...call.body as object, version: 6, quota_generation: 3 }),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    const dissection = [...view.root.querySelectorAll<HTMLInputElement>("input[type=checkbox]")]
      .find((input) => input.getAttribute("aria-label") === "Dissection enabled")!;
    dissection.checked = true;
    view.quota("Dissection · Free").number.value = "10";
    const premiumBuzz = view.quota("Best buzz · Premium");
    premiumBuzz.unlimited.checked = false;
    premiumBuzz.unlimited.dispatchEvent(new Event("change"));
    premiumBuzz.number.value = "7";
    view.root.querySelector<HTMLSelectElement>("select")!.value = "week";
    view.save();
    await vi.waitFor(() => expect(view.actions.message).toHaveBeenCalled());
    const post = view.calls.find((call) => call.method === "POST" && call.path === SETTINGS_PATH)!;
    expect(post.body).toEqual({
      dissection_enabled: true,
      best_buzz_enabled: true,
      free: { dissection: 10, best_buzz: null },
      premium: { dissection: 24, best_buzz: 7 },
      reset_period: "week",
      expected_version: 5,
    });
    expect(view.root.textContent).toContain("Quota generation: 3");
    expect(view.actions.message).toHaveBeenCalledWith("Management", ["AI settings saved."]);
  });

  it("shows a reload prompt on a stale save instead of overwriting the form", async () => {
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`POST ${SETTINGS_PATH}`]: () => reply({ error: { code: "stale_write" } }, 409),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    view.quota("Dissection · Premium").number.value = "30";
    view.save();
    await vi.waitFor(() => expect(document.querySelector("dialog")?.textContent).toContain("Settings changed"));
    expect(document.querySelector("dialog")!.textContent).toContain("unsaved edits will be lost");
    // The unsaved draft is preserved, not blindly overwritten by the stale server data.
    expect(view.quota("Dissection · Premium").number.value).toBe("30");
    document.querySelector<HTMLButtonElement>("dialog .primary-button")!.click();
    expect(view.actions.reload).toHaveBeenCalledOnce();
  });

  it("requires cost confirmation before an explicit provider check and applies its result", async () => {
    const unknown = { ...provider, state: "unknown", verified_at: null, checked_at: null };
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(unknown),
      [`POST ${CHECK_PATH}`]: () => reply({
        ...provider, state: "responding",
        check_available_at: new Date(Date.now() + 60_000).toISOString(),
      }),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("openai · gpt-test"));
    expect(view.root.textContent).toContain("Not verified yet");
    view.actions.confirm.mockReturnValueOnce(false);
    view.button("Check provider").click();
    expect(view.calls.filter((call) => call.path === CHECK_PATH)).toHaveLength(0);
    view.button("Check provider").click();
    await vi.waitFor(() => expect(view.root.textContent).toContain("Responding"));
    const post = view.calls.find((call) => call.path === CHECK_PATH)!;
    expect(post.body).toEqual({ confirm_cost: true });
    expect(view.actions.confirm).toHaveBeenCalledWith(
      expect.stringContaining("may incur a small provider charge"));
    // The cooldown returned by the check itself holds the button until it elapses.
    await vi.waitFor(() => expect(view.button("Check provider").disabled).toBe(true));
  });

  it("reports a failed explicit check generically without reinterpreting provider health", async () => {
    const unknown = { ...provider, state: "unknown", verified_at: null, checked_at: null };
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(unknown),
      [`POST ${CHECK_PATH}`]: () => reply({ error: { code: "capability_unavailable" } }, 400),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("openai · gpt-test"));
    expect(view.root.textContent).toContain("Not verified yet");
    view.button("Check provider").click();
    await vi.waitFor(() => expect(view.actions.message).toHaveBeenCalled());
    expect(view.actions.message).toHaveBeenCalledWith(
      "Management", ["This feature is not supported by the server yet."]);
    // The failed tiny check never turns into a provider/credential verdict.
    expect(view.root.textContent).toContain("Not verified yet");
    expect(view.button("Check provider").disabled).toBe(false);
  });

  it("keeps the check button idle while checking or inside the cooldown, without raw upstream detail", async () => {
    const cooldownUntil = new Date(Date.now() + 60_000).toISOString();
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply({
        ...provider, state: "error", error: "timeout", check_available_at: cooldownUntil,
      }),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Last check failed"));
    expect(view.button("Check provider").disabled).toBe(true);
    expect(view.root.textContent).toContain("Next check available after");
    // The raw upstream error kind ("timeout") is never surfaced.
    expect(view.root.textContent).not.toContain("timeout");
  });

  it("resets all AI quotas only after explicit confirmation and reflects the new generation", async () => {
    const pending = deferred<Response>();
    let released = false;
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`POST ${RESET_PATH}`]: () => {
        if (released) throw new Error("duplicate reset dispatch");
        released = true;
        return pending.promise;
      },
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    view.actions.confirm.mockReturnValueOnce(false);
    view.button("Reset all AI quotas").click();
    expect(view.calls.filter((call) => call.path === RESET_PATH)).toHaveLength(0);
    expect(view.actions.confirm).toHaveBeenCalledWith(
      expect.stringContaining("dissection, Best buzz, common knowledge"));
    view.button("Reset all AI quotas").click();
    // Duplicate dispatch is prevented while the reset is outstanding.
    view.button("Reset all AI quotas").click();
    await vi.waitFor(() => expect(view.calls.filter((call) => call.path === RESET_PATH)).toHaveLength(1));
    pending.resolve(reply({ ...settings, version: 6, quota_generation: 4 }));
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 4"));
    expect(view.actions.message).toHaveBeenCalledWith("Management", ["Quotas reset. Generation: 4"]);
    const post = view.calls.find((call) => call.path === RESET_PATH)!;
    expect(post.body).toEqual({ confirm: true, expected_version: 5 });
  });

  it("searches players only past the minimum length, debounced and bounded", async () => {
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`GET ${PLAYERS_PATH}`]: () => reply({
        items: [{ id: "11111111-1111-1111-1111-111111111111", name: "Alpha", tier: "free" }],
        limit: 20,
      }),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 300));
    const search = view.root.querySelector<HTMLInputElement>("input[type=search]")!;
    search.value = "a";
    search.dispatchEvent(new Event("input"));
    await settle();
    expect(view.calls.filter((call) => call.path === PLAYERS_PATH)).toHaveLength(0);
    expect(view.root.textContent).toContain("Enter at least 2 characters.");
    search.value = "al";
    search.dispatchEvent(new Event("input"));
    await settle();
    expect(view.calls.filter((call) => call.path === PLAYERS_PATH)).toHaveLength(1);
    expect(view.calls.find((call) => call.path === PLAYERS_PATH)!.url).toContain("search=al&limit=20");
    expect(view.root.textContent).toContain("Alpha");
    expect(view.root.textContent).toContain("11111111-1111-1111-1111-111111111111");
  });

  it("applies a manual tier change to the matching player only", async () => {
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`GET ${PLAYERS_PATH}`]: () => reply({
        items: [
          { id: "11111111-1111-1111-1111-111111111111", name: "Alpha", tier: "free" },
          { id: "22222222-2222-2222-2222-222222222222", name: "Beta", tier: "no_ai" },
        ],
        limit: 20,
      }),
      [`POST ${TIER_PATH}`]: (call) => reply({ id: (call.body as { player_id: string }).player_id, tier: "premium" }),
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    const search = view.root.querySelector<HTMLInputElement>("input[type=search]")!;
    search.value = "al";
    search.dispatchEvent(new Event("input"));
    await vi.waitFor(() => expect(view.root.textContent).toContain("Alpha"));
    const rows = [...view.root.querySelectorAll<HTMLLIElement>("[data-player-id]")];
    const select = rows[1]!.querySelector<HTMLSelectElement>("select")!;
    select.value = "premium";
    rows[1]!.querySelector<HTMLButtonElement>("button")!.click();
    await vi.waitFor(() => expect(view.calls.some((call) => call.path === TIER_PATH)));
    const post = view.calls.find((call) => call.path === TIER_PATH)!;
    expect(post.body).toEqual({ player_id: "22222222-2222-2222-2222-222222222222", tier: "premium" });
    await vi.waitFor(() => expect(rows[1]!.querySelector<HTMLSelectElement>("select")!.value).toBe("premium"));
    expect(rows[0]!.querySelector<HTMLSelectElement>("select")!.value).toBe("free");
  });

  it("never lands a late tier response on a player replaced by a newer search", async () => {
    const tierReply = deferred<Response>();
    let searched = 0;
    const view = setup({
      [`GET ${SETTINGS_PATH}`]: () => reply(settings),
      [`GET ${PROVIDER_PATH}`]: () => reply(provider),
      [`GET ${PLAYERS_PATH}`]: () => {
        ++searched;
        return searched === 1 ? reply({
          items: [{ id: "11111111-1111-1111-1111-111111111111", name: "Alpha", tier: "free" }],
          limit: 20,
        }) : reply({
          items: [{ id: "22222222-2222-2222-2222-222222222222", name: "Beta", tier: "no_ai" }],
          limit: 20,
        });
      },
      [`POST ${TIER_PATH}`]: () => tierReply.promise,
    });
    await vi.waitFor(() => expect(view.root.textContent).toContain("Quota generation: 3"));
    const search = view.root.querySelector<HTMLInputElement>("input[type=search]")!;
    search.value = "al";
    search.dispatchEvent(new Event("input"));
    await vi.waitFor(() => expect(view.root.textContent).toContain("Alpha"));
    const alphaRow = view.root.querySelector<HTMLLIElement>("[data-player-id]")!;
    alphaRow.querySelector<HTMLSelectElement>("select")!.value = "premium";
    alphaRow.querySelector<HTMLButtonElement>("button")!.click();
    // A new search replaces the row while the tier request is outstanding.
    search.value = "beta";
    search.dispatchEvent(new Event("input"));
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(view.root.textContent).toContain("Beta");
    expect(view.root.textContent).not.toContain("Alpha");
    tierReply.resolve(reply({ id: "11111111-1111-1111-1111-111111111111", tier: "premium" }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    // The stale response is dropped: the visible player keeps its server tier.
    expect(view.root.querySelector<HTMLLIElement>("[data-player-id]")!
      .querySelector<HTMLSelectElement>("select")!.value).toBe("no_ai");
    expect(view.actions.message).not.toHaveBeenCalled();
  });
});
