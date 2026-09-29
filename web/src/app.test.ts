import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api/client";
import { MiniAppShell } from "./app";
import type { MiniAppPlatform } from "./platform/telegram";
import { Router } from "./routing/router";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

class FakePlatform implements MiniAppPlatform {
  readonly initData = "signed-init-data";
  readonly isTelegram = true;
  initialize = vi.fn();
  destroy = vi.fn();
  setBackHandler = vi.fn();
  setMainAction = vi.fn();
  notifySuccess = vi.fn();
  notifyError = vi.fn();
  returnToBot = vi.fn();
}

describe("MiniAppShell", () => {
  let shell: MiniAppShell | undefined;

  afterEach(() => {
    shell?.stop();
    shell = undefined;
    document.body.replaceChildren();
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  const lobby = {
    kind: "lobby", state: "ready", id: "lobby-id", version: 7,
    tournament_id: "cup-id", tournament_name: "Autumn Cup", invitation_code: "invite-code",
    status: "assembling", max_players: 4, expires_at: "2099-01-01", searching: false,
    hybrid_matchmaking_available: false, settings: { theme_count: 3, ready_delay: 1 },
    selected_packets: [{ packet_id: "selected", name: "Selected packet", year: 2020, published_at: "2024-12-31T23:00:00Z", lead_author: "Anna", authors: ["Anna", "Boris"], fresh_play_unit_count: 2, total_play_unit_count: 5, playable_for_all: true }],
    packet_suggestions: [{ packet_id: "discoverable", name: "Discoverable packet", year: 2022, published_at: "2025-01-01T00:00:00Z", lead_author: "Carol", authors: ["Carol", "Dmitry"], fresh_play_unit_count: 4, total_play_unit_count: 6, playable_for_all: false }],
    members: [{ display_name: "Alice <b>", role: "player", ready: true }],
    viewer: { display_name: "Alice <b>", role: "player", ready: true },
    validation_violations: [{ code: "insufficient_fresh_content" }], available_actions: ["settings_update", "packet_select", "packet_remove", "unready", "invite"],
    mutable_parameters: ["theme_count"], poll_after_seconds: 5, last_event_sequence: 1,
    setting_descriptors: [
      { name: "theme_count", value: 3, value_type: "integer", description_key: "setting.theme_count.description", options: [] },
      { name: "ready_delay", value: 1, value_type: "number", description_key: "setting.ready_delay.description", options: [] },
    ],
  };

  function lobbyShell(section: string, overrides: Record<string, unknown> = {}) {
    window.history.replaceState({}, "", `/lobbies/ref?section=${section}`);
    const resource = structuredClone(lobby);
    Object.assign(resource, structuredClone(overrides));
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (String(url).endsWith("/packet-select")) {
        const { packet_id } = JSON.parse(String(options?.body));
        resource.selected_packets.push(resource.packet_suggestions.find((packet) => packet.packet_id === packet_id)!);
        resource.version += 1;
        return response({ selected: true });
      }
      if (String(url).endsWith("/packet-remove")) {
        const { packet_id } = JSON.parse(String(options?.body));
        resource.selected_packets = resource.selected_packets.filter((packet) => packet.packet_id !== packet_id);
        resource.version += 1;
        return response({ removed: true });
      }
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);
    shell.start();
    return { root, fetcher };
  }

  function libraryShell(command: "view" | "download", confirm: boolean) {
    window.history.replaceState({}, "", command === "view" ? "/library/version" : "/library");
    vi.spyOn(window, "confirm").mockReturnValue(confirm);
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (String(url).endsWith(`/${command}`)) {
        const body = JSON.parse(String(options?.body));
        if (!body.confirm) return response({ confirmation_required: true, fresh_unit_count: 1 });
        return response(command === "download" ? { confirmation_required: false, queued: true } : {
          confirmation_required: false, name: "Packet", pages: [{ title: "Theme", author: "Writer", questions: [{
            value: 10, text: "Protected question", answer: "Protected answer", accepted_answers: [], commentary: "", author: "", form: "", source: "",
          }] }],
        });
      }
      return response({ locale: "en", authorization: { allowed: true }, resource: {
        kind: "library", state: "ready", items: [{ packet_id: "packet", version_id: "version", name: "Packet", year: 2020, published_at: "2026-01-01", lead_author: "Writer", authors: [], fresh_play_unit_count: 2, total_play_unit_count: 4, tournaments: [] }],
      } });
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);
    shell.start();
    return { root, fetcher };
  }

  it("submits an author link request from the searchable window", async () => {
    window.history.replaceState({}, "", "/authors/link");
    const authorship = { packet_count: 1, theme_count: 2, question_count: 10,
      packet_names: ["Packet"], theme_names: ["Theme"], tournament_names: [], years: [2026] };
    let requests = [{
      request_id: "existing", player_id: "player-1", player_nickname: "Alice", status: "pending",
      author: { author_id: "a9", display_name: "Grace Hopper", authorship },
      request_note: "Old request", decision_note: null, decided_by_id: null,
      created_at: "2026-09-01T00:00:00Z", decided_at: null, cancelled_at: null,
    }];
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (String(url).includes("/routes/resolve")) {
        return response({ locale: "en", authorization: { allowed: true },
          resource: { kind: "author_links", state: "ready", items: requests } });
      }
      if (String(url).includes("/api/miniapp/authors?query=")) {
        return response({ items: [
          { author_id: "a1", display_name: "Ada Lovelace", authorship },
        ], next_cursor: null });
      }
      if (String(url).endsWith("/api/miniapp/authors/link")) {
        const body = JSON.parse(String(options?.body));
        requests = [{
          request_id: "new", player_id: "player-1", player_nickname: "Alice", status: "pending",
          author: { author_id: body.author_id, display_name: "Ada Lovelace", authorship },
          request_note: body.note, decision_note: null, decided_by_id: null,
          created_at: "2026-09-02T00:00:00Z", decided_at: null, cancelled_at: null,
        }, ...requests];
        return response({ request_id: "new" });
      }
      return response({});
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);
    shell.start();
    await vi.waitFor(() => expect(root.textContent).toContain("Grace Hopper"));
    const select = root.querySelector<HTMLSelectElement>("select")!;
    select.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(select.textContent).toContain("Ada Lovelace"));
    select.value = "a1";
    root.querySelector<HTMLInputElement>('[name="note"]')!.value = "I am this author";
    root.querySelector<HTMLFormElement>("form")!.requestSubmit();
    const submitted = await vi.waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => String(url) === "/api/miniapp/authors/link");
      expect(call).toBeDefined();
      return call!;
    });
    expect(JSON.parse(String(submitted[1]?.body))).toEqual({ author_id: "a1", note: "I am this author" });
    await vi.waitFor(() => expect(root.querySelectorAll("[data-request-id]")).toHaveLength(2));
    expect(root.querySelector('[data-request-id="new"] h2')?.textContent).toBe("Ada Lovelace");
  });

  it("requires confirmation before exposing a fresh packet", async () => {
    const { root, fetcher } = libraryShell("view", true);
    await vi.waitFor(() => expect(root.textContent).toContain("Protected answer"));
    const calls = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/view"));
    expect(calls.map(([, options]) => JSON.parse(String(options?.body)))).toEqual([{ confirm: false }, { confirm: true }]);
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("prevent you from playing"));
  });

  it("returns to library without revealing content when confirmation is declined", async () => {
    const { root, fetcher } = libraryShell("view", false);
    await vi.waitFor(() => expect(root.querySelector("article button")?.textContent).toBe("View"));
    expect(root.textContent).not.toContain("Protected answer");
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/view"))).toHaveLength(1);
  });

  it("confirms a fresh download and reports that Telegram delivery is queued", async () => {
    const { root, fetcher } = libraryShell("download", true);
    await vi.waitFor(() => expect(root.querySelectorAll("article button")).toHaveLength(2));
    root.querySelectorAll<HTMLButtonElement>("article button")[1]!.click();
    await vi.waitFor(() => expect(document.body.textContent).toContain("queued for delivery in Telegram"));
    const calls = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/download"));
    expect(calls.map(([, options]) => JSON.parse(String(options?.body)))).toEqual([{ confirm: false }, { confirm: true }]);
    expect(root.textContent).not.toContain("Protected answer");
  });

  it("opens the packet picker and submits a discoverable packet with the lobby version", async () => {
    const { root, fetcher } = lobbyShell("packets");
    await vi.waitFor(() => expect(root.textContent).toContain("Discoverable packet"));
    expect(root.querySelector('input[name="username"]')).toBeNull();
    const button = root.querySelector<HTMLButtonElement>('[data-packet-id="discoverable"] button');
    button?.click();
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/packet-select"))).toBe(true));
    const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/packet-select"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ expected_version: 7, packet_id: "discoverable" });
    await vi.waitFor(() => expect(root.querySelector('[data-packet-id="discoverable"] button')?.textContent).toBe("Remove"));
    expect(root.querySelectorAll('[data-packet-id="discoverable"]')).toHaveLength(1);
    root.querySelector<HTMLButtonElement>('[data-packet-id="discoverable"] button')?.click();
    await vi.waitFor(() => expect(root.querySelector('[data-packet-id="discoverable"] button')?.textContent).toBe("Add packet"));
    const remove = fetcher.mock.calls.find(([url]) => String(url).endsWith("/packet-remove"));
    expect(JSON.parse(String(remove?.[1]?.body))).toEqual({ expected_version: 8, packet_id: "discoverable" });
  });

  it("shows all settings and edits only granted fields with typed controls", async () => {
    const { root, fetcher } = lobbyShell("settings");
    await vi.waitFor(() => expect(root.querySelector('input[name="setting:theme_count"]')).not.toBeNull());
    const input = root.querySelector<HTMLInputElement>('input[name="setting:theme_count"]')!;
    expect(input.type).toBe("number");
    expect(root.querySelector('input[name="setting:ready_delay"]')).toBeNull();
    expect(root.textContent).toContain("Ready delay");
    input.value = "4";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/settings"))).toBe(true));
    const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/settings"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ expected_version: 7, changes: { theme_count: 4 } });
  });

  it("lists editable settings before fixed ones and previews message pacing options", async () => {
    const { root } = lobbyShell("settings", { mutable_parameters: ["ready_delay"] });
    await vi.waitFor(() => expect(root.querySelector('input[name="setting:ready_delay"]')).not.toBeNull());
    const form = root.querySelector("form.settings-form")!;
    expect(form.textContent).toContain("Settings you can change");
    expect(root.querySelector('input[name="setting:theme_count"]')).toBeNull();
    const fixed = root.querySelector(".resource-card")!;
    expect(fixed.textContent).toContain("Fixed settings");
    expect(fixed.textContent).toContain("Theme count");
    expect(fixed.querySelector("input, select, button")).toBeNull();
    expect(form.compareDocumentPosition(fixed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const demos = root.querySelectorAll(".setting-demo");
    expect(demos).toHaveLength(1);
    expect(demos[0]!.textContent).toContain("Message flow preview");
    const input = root.querySelector<HTMLInputElement>('input[name="setting:ready_delay"]')!;
    input.value = "25";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.waitFor(() => expect(root.querySelector(".setting-demo-status")?.textContent).toContain("shortened for the preview"));
  });

  it("groups settings into categories and submits the maximum-themes sentinel", async () => {
    const { root, fetcher } = lobbyShell("settings", {
      settings: { theme_count: 3, ready_delay: 1, minimum_players: 4 },
      mutable_parameters: ["theme_count", "ready_delay", "minimum_players"],
      setting_descriptors: [
        { name: "theme_count", value: 3, value_type: "integer", description_key: "ruleset.si.theme_count.description", options: [] },
        { name: "ready_delay", value: 1, value_type: "number", description_key: "ruleset.si.ready_delay.description", options: [] },
        { name: "minimum_players", value: 4, value_type: "integer", description_key: "ruleset.si.minimum_players.description", options: [] },
      ],
    });
    await vi.waitFor(() => expect(root.querySelector('input[name="setting:theme_count"]')).not.toBeNull());
    const headings = Array.from(root.querySelectorAll(".descriptor-category"), (node) => node.textContent);
    expect(headings).toEqual(["Number of players", "Theme count", "Message timings"]);
    const maximum = root.querySelector<HTMLInputElement>('input[name="setting:theme_count:max"]')!;
    const count = root.querySelector<HTMLInputElement>('input[name="setting:theme_count"]')!;
    expect(maximum.checked).toBe(false);
    expect(root.textContent).toContain("Use all themes from the selected packets");
    maximum.checked = true;
    maximum.dispatchEvent(new Event("change", { bubbles: true }));
    expect(count.disabled).toBe(true);
    count.form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/settings"))).toBe(true));
    const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/settings"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ expected_version: 7, changes: { theme_count: "max" } });
  });

  it("shows the maximum-themes sentinel as a fixed value without edit controls", async () => {
    const { root } = lobbyShell("settings", {
      settings: { theme_count: "max", ready_delay: 1 },
      mutable_parameters: [],
      setting_descriptors: [
        { name: "theme_count", value: "max", value_type: "integer", description_key: "ruleset.si.theme_count.description", options: [] },
        { name: "ready_delay", value: 1, value_type: "number", description_key: "ruleset.si.ready_delay.description", options: [] },
      ],
    });
    await vi.waitFor(() => expect(root.textContent).toContain("Fixed settings"));
    const fixed = root.querySelector(".resource-card")!;
    expect(fixed.textContent).toContain("All themes from the selected packets");
    expect(root.querySelector('input[name="setting:theme_count"]')).toBeNull();
  });

  it("saves player-editable minimum and maximum together", async () => {
    const { root, fetcher } = lobbyShell("settings", {
      settings: { minimum_players: 4, maximum_players: 4 },
      mutable_parameters: ["minimum_players", "maximum_players"],
      setting_descriptors: ["minimum_players", "maximum_players"].map((name) => ({
        name, value: 4, value_type: "integer", options: [],
        description_key: `ruleset.si.${name}.description`,
      })),
    });
    await vi.waitFor(() => expect(root.querySelector("[name='setting:minimum_players']")).not.toBeNull());
    const minimum = root.querySelector<HTMLInputElement>("[name='setting:minimum_players']")!;
    const maximum = root.querySelector<HTMLInputElement>("[name='setting:maximum_players']")!;
    expect(minimum.min).toBe("1");
    expect(maximum.max).toBe("12");
    minimum.value = "1";
    maximum.value = "6";
    minimum.form!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/settings"))).toBe(true));
    const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/settings"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      expected_version: 7, changes: { minimum_players: 1, maximum_players: 6 },
    });
  });

  it("shows lobby membership without an invite-player form", async () => {
    const { root } = lobbyShell("overview");
    await vi.waitFor(() => expect(root.textContent).toContain("Alice <b>"));
    expect(root.querySelector("li b")).toBeNull();
    expect(root.textContent).toContain("Selected packet");
    expect(root.querySelector('input[name="username"]')).toBeNull();
    expect(root.textContent).not.toContain("Invite player");
    expect(root.textContent).not.toContain("Discoverable packet");
    expect(root.querySelector("form")).toBeNull();
    expect(root.querySelector(".lobby-packet-card button")).toBeNull();
    expect(root.textContent).toContain("Ready delay");
    expect(root.querySelector('[role="alert"]')?.textContent).toContain("Not enough themes");
    expect(root.querySelector(".lobby-overview")?.lastElementChild?.getAttribute("role")).toBe("alert");
  });

  it("shows selected cards first with metadata, shared freshness, and independent playability", async () => {
    const { root } = lobbyShell("packets");
    await vi.waitFor(() => expect(root.querySelectorAll(".lobby-packet-card")).toHaveLength(2));
    const cards = root.querySelectorAll(".lobby-packet-card");
    expect(cards[0]?.textContent).toContain("Selected packet");
    expect(cards[0]?.textContent).toContain("2020");
    expect(cards[0]?.textContent).toContain("Anna, Boris");
    expect(cards[0]?.textContent).toContain("2 / 5");
    expect(cards[0]?.textContent).toContain("Playable for all: Yes");
    expect(cards[1]?.textContent).toContain("4 / 6");
    expect(cards[1]?.textContent).toContain("Playable for all: No");
  });

  it("combines title, author, and inclusive year ranges and preserves filters after mutation", async () => {
    const { root } = lobbyShell("packets");
    await vi.waitFor(() => expect(root.querySelector('input[name="name"]')).not.toBeNull());
    const filter = (name: string, value: string): void => {
      const input = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    const ids = (): Array<string | null> => Array.from(root.querySelectorAll(".lobby-packet-card"), (card) => card.getAttribute("data-packet-id"));
    filter("name", "DISCOVERABLE");
    filter("author", "dmitry");
    filter("year_from", "2022");
    filter("year_to", "2022");
    filter("publication_from", "2025");
    filter("publication_to", "2025");
    expect(ids()).toEqual(["discoverable"]);
    for (const [name, value, restore] of [
      ["author", "Anna", "Carol"], ["year_from", "2023", "2022"],
      ["year_to", "2021", "2022"], ["publication_from", "2026", "2025"],
      ["publication_to", "2024", "2025"],
    ] as const) {
      filter(name, value);
      expect(ids()).toEqual([]);
      expect(root.textContent).toContain("No packets match");
      filter(name, restore);
      expect(ids()).toEqual(["discoverable"]);
    }
    root.querySelector<HTMLButtonElement>('[data-packet-id="discoverable"] button')?.click();
    await vi.waitFor(() => expect(root.querySelector('[data-packet-id="discoverable"] button')?.textContent).toBe("Remove"));
    expect(ids()).toEqual(["discoverable"]);
    expect(root.querySelector<HTMLInputElement>('input[name="name"]')?.value).toBe("DISCOVERABLE");
    Array.from(root.querySelectorAll("button")).find((button) => button.textContent === "Reset filters")?.click();
    expect(ids()).toEqual(["selected", "discoverable"]);
  });

  it("authenticates, re-authorizes a route, localizes it, and renders untrusted text safely", async () => {
    window.history.replaceState({}, "", "/history");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "en",
          authorization: { allowed: true },
          resource: {
            state: "ready",
            items: [{ id: "one", label: "<b>Result</b>", description: "Player & score" }],
          },
        }),
      );
    const root = document.createElement("div");
    document.body.append(root);
    const platform = new FakePlatform();
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), platform, false);

    shell.start();

    await vi.waitFor(() => expect(root.querySelector("h1")?.textContent).toBe("History"));
    expect(root.querySelector("h2")?.textContent).toBe("<b>Result</b>");
    expect(root.querySelector("h2 b")).toBeNull();
    expect(root.querySelector(".shell-nav")).toBeNull();
    expect(fetcher.mock.calls[1]?.[0]).toBe("/api/miniapp/routes/resolve?path=%2Fhistory");
    expect(platform.initialize).toHaveBeenCalledOnce();
  });

  it("renders a canonical access error without retrying a forbidden action", async () => {
    window.history.replaceState({}, "", "/manager/appeals");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "ru" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "ru",
          authorization: { allowed: false, reason_code: "forbidden" },
          resource: { state: "empty" },
        }),
      );
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);

    shell.start();

    await vi.waitFor(() => expect(root.querySelector("[role=alert]")).not.toBeNull());
    expect(root.querySelector("[role=alert]")?.textContent).toContain("больше нет доступа");
    expect(root.querySelector("[role=alert] button")).toBeNull();
  });

  it("renders permission-derived tournament actions and opens localized info in-app", async () => {
    window.history.replaceState({}, "", "/tournaments?role=player");
    const tournament = {
      id: "00000000-0000-0000-0000-000000000001",
      name: "Autumn Open",
      slug: "autumn-open",
      status: "active",
      phase: "ongoing",
      visibility: "public",
      starts_at: "2026-09-10T12:00:00Z",
      planned_ends_at: null,
      actual_ends_at: null,
      language: "en",
      payment_type: "free",
      pricing_plans: [],
      registration_open: true,
      registration_starts_at: null,
      registration_ends_at: null,
      authors: ["Author One"],
      type_key: "ladder",
      type_version: 1,
      ruleset_key: "si",
      ruleset_version: 1,
      membership_status: null,
      managed: false,
      policy_version: 2,
      available_actions: ["info", "register"],
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "en",
          authorization: { allowed: true },
          resource: {
            kind: "tournaments",
            state: "ready",
            role: "player",
            navigation_version: 4,
            total: 1,
            items: [tournament],
          },
        }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "en",
          authorization: { allowed: true },
          resource: {
            kind: "tournament_profile",
            state: "ready",
            type_key: "ladder",
            tournament: {
              id: "00000000-0000-0000-0000-000000000001",
              name: "Autumn Open",
              slug: "autumn-open",
            },
            general: {
              name: "Autumn Open",
              slug: "autumn-open",
              description: "",
              status: "active",
              moderation_status: "normal",
              visibility: "public",
              language: "en",
              payment_type: "free",
              type: { key: "ladder", name: "Ladder", version: 1 },
              ruleset: { key: "si", name: "Своя игра", version: 1 },
              starts_at: null,
              planned_ends_at: null,
              actual_starts_at: null,
              actual_ends_at: null,
              finalized_at: null,
              settings_version: 1,
              registration: { open: true, starts_at: null, ends_at: null },
              managers: [],
              authors: ["Author One"],
              registration_count: 0,
              participant_count: 0,
            },
            registrations: [],
            participants: null,
            games: { kind: "ladder", items: [] },
            leaders: { kind: "ladder", items: [] },
          },
        }),
      );
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);

    shell.start();

    await vi.waitFor(() => expect(root.querySelector("h2")?.textContent).toBe("Autumn Open"));
    expect(Array.from(root.querySelectorAll(".tournament-actions button"), (item) => item.textContent)).toEqual([
      "Info",
      "Register",
    ]);
    (root.querySelector(".tournament-actions button") as HTMLButtonElement).click();
    await vi.waitFor(() => expect(root.querySelector(".tournament-profile .tournament-name")?.textContent).toBe("Autumn Open"));
    expect(window.location.pathname).toBe("/tournaments/00000000-0000-0000-0000-000000000001");
    expect(fetcher.mock.calls[2]?.[0]).toContain("/api/miniapp/routes/resolve");
  });

  it("selects a managed tournament with the authoritative navigation version", async () => {
    window.history.replaceState({}, "", "/tournaments?role=manager&relationship=managed");
    const platform = new FakePlatform();
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "ru" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "ru",
          authorization: { allowed: true },
          resource: {
            kind: "tournaments",
            state: "ready",
            role: "manager",
            navigation_version: 7,
            total: 1,
            items: [{
              id: "00000000-0000-0000-0000-000000000002",
              name: "Кубок",
              slug: "cup",
              status: "active",
              phase: "ongoing",
              visibility: "private",
              language: "ru",
              payment_type: "free",
              pricing_plans: [],
              registration_open: false,
              authors: [],
              type_key: "classic",
              type_version: 1,
              ruleset_key: "si",
              ruleset_version: 1,
              managed: true,
              policy_version: 1,
              available_actions: ["info", "select_manager"],
            }],
          },
        }),
      )
      .mockResolvedValueOnce(response({ ok: true }));
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), platform, false);

    shell.start();
    await vi.waitFor(() => expect(root.querySelector("h2")?.textContent).toBe("Кубок"));
    const select = Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-actions button"))
      .find((button) => button.textContent === "Выбрать");
    select?.click();

    await vi.waitFor(() => expect(platform.returnToBot).toHaveBeenCalledOnce());
    const request = fetcher.mock.calls[2];
    expect(request?.[0]).toContain("/select");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ mode: "manager", expected_version: 7 });
  });

  it("renders manager settings from capabilities and locks competition after finalization", async () => {
    window.history.replaceState({}, "", "/manager/tournaments/opaque-reference/settings");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "en",
          authorization: { allowed: true },
          resource: {
            kind: "manager_settings",
            state: "ready",
            settings_version: 3,
            finalized_at: "2026-09-03T12:00:00Z",
            registration_enabled: false,
            ignore_late_registrations: true,
            available_actions: ["update_metadata", "update_policy"],
            type_options: ["classic", "ladder"],
            ruleset_options: ["si"],
            policies: { observing: "forbidden" },
            default_parameters: { theme_count: 5 },
            player_mutable_parameters: ["theme_count"],
            author_names: ["Author One"],
            authors: [{ id: "00000000-0000-0000-0000-000000000004", display_name: "Author One" }],
            setting_descriptors: [
              { name: "theme_count", value_type: "integer", description_key: "ruleset.si.theme_count.description", value: 5, options: [] },
              { name: "question_values", value_type: "array", description_key: "ruleset.si.question_values.description", value: [10, 20], options: [] },
            ],
            policy_descriptors: [
              { name: "observing", value_type: "enum", description_key: "policy.observing.description", value: "forbidden", options: ["unlimited", "burnt-only", "forbidden"] },
              { name: "packets_per_lobby", value_type: "enum", description_key: "policy.packets_per_lobby.description", value: "one", options: ["one", "any"] },
              { name: "packets_discoverable_by_default", value_type: "boolean", description_key: "policy.packets_discoverable_by_default.description", value: true, options: [] },
              { name: "packets_playable_by_default", value_type: "boolean", description_key: "policy.packets_playable_by_default.description", value: false, options: [] },
              { name: "packets_readable_by_default", value_type: "boolean", description_key: "policy.packets_readable_by_default.description", value: false, options: [] },
              { name: "member_uploads", value_type: "boolean", description_key: "policy.member_uploads.description", value: false, options: [] },
            ],
            registration_requirements: [],
            packet_assignment_count: 2,
            membership_count: 10,
            manager_count: 2,
            classic: { players: [], schemes: [{ id: "groups-9-4", kind: "groups", size: 9, round_count: 4 }],
              stages: [{ kind: "first", stage_type: "groups", scheme_key: "groups-9-4", started_at: "2026-09-01", completed_at: null,
                seeds: [], place_points: ["4", "3", "2", "1"], score_multiplier: "0.02", rounds: [], standings: [] }] },
            tournament: {
              id: "00000000-0000-0000-0000-000000000003",
              name: "Managed Cup",
              slug: "managed-cup",
              status: "active",
              phase: "ongoing",
              visibility: "private",
              language: "en",
              payment_type: "one-time",
              pricing_plans: [{
                name: "Standard",
                prices: [
                  { amount: 100, currency: "RUB" },
                  { amount: 2, currency: "USD" },
                ],
              }],
              registration_open: true,
              authors: ["Author One"],
              type_key: "classic",
              type_version: 1,
              ruleset_key: "si",
              ruleset_version: 1,
              managed: true,
              policy_version: 2,
              settings_version: 3,
              available_actions: ["info", "select_manager"],
            },
          },
        }),
      );
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);

    shell.start();

    await vi.waitFor(() => expect(root.querySelector("form.settings-form")).not.toBeNull());
    expect(root.querySelector<HTMLSelectElement>("[name=type_key]")?.disabled).toBe(true);
    expect(root.querySelector<HTMLFieldSetElement>(".classic-settings fieldset")?.disabled).toBe(true);
    expect(root.querySelectorAll(".classic-settings fieldset")).toHaveLength(2);
    expect(root.textContent).toContain("Tournament type and ruleset were locked");
    const registration = root.querySelector<HTMLInputElement>("[name=registration_open]")!;
    expect(root.querySelector("[name=registration_available]")).toBeNull();
    expect(registration.checked).toBe(false);
    registration.checked = true;
    root.querySelector<HTMLInputElement>("[name=registration_starts_at]")!.value = "2026-09-12T12:00";
    root.querySelector<HTMLInputElement>("[name=registration_ends_at]")!.value = "2026-09-12T13:00";
    const playoffSettings = root.querySelectorAll(".classic-settings fieldset")[1]!;
    expect(playoffSettings.textContent).not.toContain("Place points");
    expect(playoffSettings.querySelectorAll("input")).toHaveLength(0);
    expect(playoffSettings.querySelector("button")!.classList.contains("primary-button")).toBe(true);
    expect(root.querySelector<HTMLInputElement>("[name=ignore_late_registrations]")?.checked).toBe(true);
    expect(root.querySelectorAll("textarea")).toHaveLength(1);
    expect(root.querySelector<HTMLTextAreaElement>("[name=description]")?.value).toBe("");
    expect(root.querySelector("[name='setting:theme_count']")).not.toBeNull();
    expect(root.querySelector("[name='setting:question_values']")?.getAttribute("type")).toBe("text");
    expect(root.querySelector("[name=author_ids]")?.getAttribute("value")).toBe("00000000-0000-0000-0000-000000000004");
    expect(root.textContent).toContain("Add pricing plan");
    expect(root.querySelectorAll("[data-pricing-plan]")).toHaveLength(1);
    expect(root.querySelectorAll(".pricing-price-row")).toHaveLength(2);
    const addPrice = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Add price");
    addPrice?.click();
    expect(root.querySelectorAll(".pricing-price-row")).toHaveLength(3);
    const settingPanels = root.querySelectorAll<HTMLElement>(".descriptor-editor:first-of-type .descriptor-panel");
    expect(Array.from(settingPanels).filter((panel) => !panel.hidden)).toHaveLength(1);
    expect(root.querySelector(".setting-demo")).toBeNull();
    expect(root.textContent).not.toContain("Management overview");
    expect(root.querySelector(".danger-button")).toBeNull();
    expect(fetcher.mock.calls[1]?.[0]).toContain("opaque-reference");
    for (const [name, defaultValue] of [
      ["packets_discoverable_by_default", true],
      ["packets_playable_by_default", false],
      ["packets_readable_by_default", false],
      ["member_uploads", false],
    ] as const) {
      const control = root.querySelector<HTMLInputElement>(`[name='policy:${name}']`);
      expect(control?.type).toBe("checkbox");
      expect(control?.checked).toBe(defaultValue);
      control!.checked = !defaultValue;
    }
    expect(root.textContent).toContain("Packets discoverable by default");
    expect(root.textContent).toContain("Packets playable by default");
    expect(root.textContent).toContain("Packets readable by default");
    expect(root.textContent).toContain("Community packet uploads");
    const packetsPerLobby = root.querySelector<HTMLSelectElement>("[name='policy:packets_per_lobby']")!;
    expect(packetsPerLobby.value).toBe("one");
    expect(Array.from(packetsPerLobby.options).map((option) => option.textContent)).toEqual([
      "Only one", "Any amount",
    ]);
    packetsPerLobby.value = "any";
    packetsPerLobby.dispatchEvent(new Event("change", { bubbles: true }));
    const addRequirement = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Add requirement")!;
    addRequirement.click();
    addRequirement.click();
    root.querySelectorAll<HTMLElement>("[data-requirement]")[1]!.querySelector("button")!.click();
    root.querySelector<HTMLSelectElement>('[name="requirement_kind"]')!.value = "has-not-played-tournament";
    root.querySelector<HTMLInputElement>('[name="requirement_target"]')!.value = "00000000-0000-0000-0000-000000000008";
    root.querySelector<HTMLInputElement>('[name="requirement_message"]')!.value = "New players only";
    fetcher.mockRejectedValueOnce(new Error("Offline"));
    root.querySelector<HTMLFormElement>("form.settings-form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    const saved = JSON.parse(String(fetcher.mock.calls[2]?.[1]?.body));
    expect(saved.registration_open).toBe(true);
    expect(saved.registration_requirements).toEqual([{
      kind: "has-not-played-tournament",
      target_id: "00000000-0000-0000-0000-000000000008",
      failure_message: "New players only",
    }]);
    expect(saved.registration_open_override).toBeUndefined();
    expect(saved.registration_starts_at).toBe(new Date("2026-09-12T12:00").toISOString());
    expect(saved.registration_ends_at).toBe(new Date("2026-09-12T13:00").toISOString());
    expect(saved.policies).toMatchObject({
      packets_discoverable_by_default: false,
      packets_playable_by_default: true,
      packets_readable_by_default: true,
      member_uploads: true,
      packets_per_lobby: "any",
    });
    const managementButton = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Tournament management")!;
    expect(managementButton.type).toBe("button");
    managementButton.click();
    expect(window.location.pathname).toBe("/manager/tournaments/opaque-reference/management");
  });

  it.each(["classic", "ladder"])("renders %s tournament management sections and packet access", async (typeKey) => {
    window.history.replaceState({}, "", "/manager/tournaments/opaque-reference/management");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" }),
      )
      .mockResolvedValueOnce(
        response({
          locale: "en",
          authorization: { allowed: true },
          resource: {
            kind: "manager_management",
            state: "ready",
            sections: ["general", "registrations", "packet_accessibility", "packet_management"],
            settings_version: 4,
            finalized_at: null,
            registration_scheduled_open: false,
            registration_open: false,
            registration_open_override: null,
            registration_count: 1,
            approved_count: 0,
            participant_count: 1,
            packet_count: 1,
            available_actions: ["finalize", "registration_decide", "packet_access", "packet_management"],
            registrations: [{
              player_id: "player-1",
              display_name: "Ada",
              real_name: "Ada Lovelace",
              status: "registered",
              registered_at: "2026-09-04T12:00:00Z",
              available_actions: ["approve", "reject"],
            }],
            packets: [{
              assignment_id: "assignment-1",
              packet_id: "packet-1",
              name: "Final packet",
              version: 2,
              player_access: [{
                player_id: "player-2",
                display_name: "Grace",
                playable: true,
                discoverable: false,
                readable: false,
              }],
            }],
            tournament: {
              id: "tournament-1",
              name: "Managed Cup",
              slug: "managed-cup",
              status: "active",
              phase: "ongoing",
              visibility: "private",
              language: "en",
              payment_type: "free",
              pricing_plans: [],
              registration_open: false,
              authors: [],
              type_key: typeKey,
              type_version: 1,
              ruleset_key: "si",
              ruleset_version: 1,
              managed: true,
              policy_version: 1,
              available_actions: [],
            },
          },
        }),
      );
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);

    shell.start();

    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    expect(root.querySelectorAll(".management-section-nav button")).toHaveLength(4);
    expect(root.querySelectorAll(".management-section")).toHaveLength(1);
    expect(root.querySelector("[data-section=general]")).not.toBeNull();
    expect(root.textContent).not.toContain("Ada Lovelace");
    const settingsButton = Array.from(root.querySelectorAll<HTMLButtonElement>("[data-section=general] button"))
      .find((button) => button.textContent === "Tournament settings")!;
    expect(settingsButton.type).toBe("button");
    const navigate = vi.spyOn(Router.prototype, "navigate").mockImplementation(() => {});
    settingsButton.click();
    expect(navigate).toHaveBeenCalledWith("/manager/tournaments/opaque-reference/settings");
    navigate.mockRestore();

    const registrationsButton = Array.from(root.querySelectorAll<HTMLButtonElement>(".management-section-nav button"))
      .find((button) => button.textContent === "Registrations");
    registrationsButton?.click();
    expect(root.querySelectorAll(".management-section")).toHaveLength(1);
    expect(root.querySelector("[data-section=registrations]")).not.toBeNull();
    expect(root.textContent).toContain("Ada Lovelace");

    const accessibilityButton = Array.from(root.querySelectorAll<HTMLButtonElement>(".management-section-nav button"))
      .find((button) => button.textContent === "Packet accessibility");
    accessibilityButton?.click();
    expect(root.querySelectorAll(".management-section")).toHaveLength(1);
    expect(root.querySelector("[data-section=packet_accessibility]")).not.toBeNull();
    expect(root.textContent).toContain("Final packet · v2");
    expect(root.textContent).toContain("Set for all");
    expect(root.querySelectorAll(".packet-access-table input[type=checkbox]")).toHaveLength(typeKey === "classic" ? 2 : 6);

    const managementButton = Array.from(root.querySelectorAll<HTMLButtonElement>(".management-section-nav button"))
      .find((button) => button.textContent === "Packet management");
    managementButton?.click();
    expect(root.querySelector(".lobby-packet-card h3")?.textContent).toBe("Final packet");
    expect(Array.from(root.querySelectorAll(".lobby-packet-card button")).map((button) => button.textContent))
      .toEqual(["Modify", "Release", "Delete"]);
    expect(root.querySelector(".lobby-packet-card")?.textContent).toContain("packet-1");
    const addExisting = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Add existing packet")!;
    addExisting.click();
    const dialog = document.querySelector<HTMLDialogElement>("dialog")!;
    const packetInput = dialog.querySelector("input")!;
    packetInput.value = "existing-packet";
    fetcher.mockResolvedValueOnce(response({ packet_id: "existing-packet", packet_version_id: "version-1",
      name: "Shared packet", year: 2026, lead_author: "Author", authors: ["Author"], theme_count: 8, question_count: 40 }));
    dialog.querySelector<HTMLButtonElement>("button[type=submit]")!.click();
    await vi.waitFor(() => expect(dialog.textContent).toContain("Shared packet"));
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain("/existing-packets/preview");
    expect(dialog.textContent).toContain("Confirm adding packet");
    expect(packetInput.disabled).toBe(true);
    const callsBeforeCancel = fetcher.mock.calls.length;
    dialog.querySelector<HTMLButtonElement>("button[type=button]")!.click();
    expect(document.querySelector("dialog")).toBeNull();
    expect(fetcher.mock.calls.length).toBe(callsBeforeCancel);

    addExisting.click();
    const confirmDialog = document.querySelector<HTMLDialogElement>("dialog")!;
    confirmDialog.querySelector("input")!.value = "existing-packet";
    fetcher.mockResolvedValueOnce(response({ packet_id: "existing-packet", packet_version_id: "version-1",
      name: "Shared packet", year: null, lead_author: "", authors: [], theme_count: 8, question_count: 40 }));
    confirmDialog.querySelector<HTMLButtonElement>("button[type=submit]")!.click();
    await vi.waitFor(() => expect(confirmDialog.textContent).toContain("Shared packet"));
    fetcher.mockResolvedValueOnce(response({ error: { code: "stale_write" } }, 409));
    confirmDialog.querySelector<HTMLButtonElement>("button[type=submit]")!.click();
    await vi.waitFor(() => expect(confirmDialog.querySelector("input")!.disabled).toBe(false));
    expect(fetcher.mock.calls.at(-1)?.[0]).toContain("/existing-packets/add");
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({
      packet_id: "existing-packet", expected_version_id: "version-1",
    });
    confirmDialog.querySelector<HTMLButtonElement>("button[type=button]")!.click();
    const question = { value: 10, text: "Question", answer: "Answer", accepted_answers: [],
      commentary: "", source: "", form: "", author: "Ada" };
    fetcher.mockResolvedValueOnce(response({
      assignment_id: "assignment-1", version: 2, errors: [], warnings: [],
      field_author_ids: { lead_author: "ada", "themes.0.author": "ada", "themes.0.questions.0.author": "ada",
        "themes.1.author": "ada", "themes.1.questions.0.author": "ada" },
      associated_authors: [{ author_id: "ada", display_name: "Ada" }],
      packet: { name: "Final packet", language: "en", year: 2026, lead_author: "Ada",
        themes: [{ name: "First", author: "Ada", questions: [question] },
          { name: "Second", author: "Ada", questions: [question] }] },
      editor: { packet_fields: ["name", "year", "language", "lead_author"],
        theme_fields: ["name", "author"], question_fields: ["value", "text", "answer", "accepted_answers", "author"] },
    }));
    Array.from(root.querySelectorAll<HTMLButtonElement>(".lobby-packet-card button"))
      .find((button) => button.textContent === "Modify")!.click();
    await vi.waitFor(() => expect(root.querySelector("form.packet-editor")).not.toBeNull());
    expect(root.querySelector<HTMLInputElement>("[data-field=name]")!.disabled).toBe(true);
    const answer = root.querySelector<HTMLTextAreaElement>("[data-question-field=answer]")!;
    expect(answer.disabled).toBe(true);
    expect(root.querySelector<HTMLSelectElement>(".packet-field-author select")!.closest("fieldset")!.disabled).toBe(true);
    expect(root.querySelector("[data-field=name]")!.parentElement!.querySelectorAll(".packet-change-button")).toHaveLength(1);
    answer.parentElement!.querySelector<HTMLButtonElement>(".is-substitution")!.click();
    expect(answer.disabled).toBe(false);
    expect(answer.parentElement!.querySelector(".is-substitution")!.getAttribute("aria-pressed")).toBe("true");
    answer.value = "Replacement answer";
    Array.from(root.querySelectorAll<HTMLButtonElement>(".packet-page-nav button"))
      .find((button) => button.textContent === "Next")!.click();
    Array.from(root.querySelectorAll<HTMLButtonElement>(".packet-page-nav button"))
      .find((button) => button.textContent === "Previous")!.click();
    expect(root.querySelector<HTMLTextAreaElement>("[data-question-field=answer]")!.value).toBe("Replacement answer");
    expect(root.querySelector<HTMLTextAreaElement>("[data-question-field=answer]")!.disabled).toBe(false);
    expect(root.querySelector<HTMLTextAreaElement>("[data-question-field=text]")!.disabled).toBe(true);
    fetcher.mockResolvedValueOnce(response({ error: { code: "validation_failed" } }, 422));
    root.querySelector<HTMLFormElement>("form.packet-editor")!.requestSubmit();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(7));
    expect(fetcher.mock.calls[6]?.[0]).toBe("/api/miniapp/manager/tournaments/opaque-reference/packets/assignment-1/save");
    const saved = JSON.parse(String(fetcher.mock.calls[6]?.[1]?.body));
    expect(saved.changes).toEqual({ "themes.0.questions.0.answer": "substitution" });
    expect(saved.content.themes[0].questions[0].answer).toBe("Replacement answer");
    expect(saved.field_author_ids["themes.0.questions.0.author"]).toBe("ada");
  });

  it("saves manager-selected Swiss round count, game size, and scoring", async () => {
    window.history.replaceState({}, "", "/manager/tournaments/ref/settings");
    const resource = {
      kind: "manager_settings", state: "ready", settings_version: 4, finalized_at: "2026-09-01",
      available_actions: ["update_metadata"], policies: {}, default_parameters: {},
      player_mutable_parameters: [], author_names: [], authors: [], registration_requirements: [],
      type_options: ["classic"], ruleset_options: ["si"], registration_enabled: false,
      ignore_late_registrations: true, packet_assignment_count: 0, membership_count: 64,
      manager_count: 1, classic: { stages: [], schemes: [], players: [] },
      tournament: { id: "cup", name: "Cup", slug: "cup", description: "", status: "active",
        type_key: "classic", ruleset_key: "si", visibility: "public", language: "en",
        payment_type: "free", pricing_plans: [], authors: [] },
    };
    const mutations: Array<Record<string, any>> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01", locale: "en" });
      if (String(url).endsWith("/classic")) {
        mutations.push(JSON.parse(String(options?.body)));
        return response(resource);
      }
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);
    shell.start();
    await vi.waitFor(() => expect(root.querySelector(".classic-settings")).not.toBeNull());
    const first = root.querySelector<HTMLFieldSetElement>(".classic-settings fieldset")!;
    const type = first.querySelector<HTMLSelectElement>("select")!;
    type.value = "swiss";
    type.dispatchEvent(new Event("change"));
    const input = (label: string) => Array.from(first.querySelectorAll("label"))
      .find((item) => item.textContent === label)!.querySelector<HTMLInputElement>("input")!;
    expect(input("Rounds").closest("div")!.hidden).toBe(false);
    input("Rounds").value = "8";
    input("Players per game").value = "3";
    first.querySelector<HTMLButtonElement>("button")!.click();
    await vi.waitFor(() => expect(mutations).toHaveLength(1));
    expect(mutations[0]).toMatchObject({ expected_version: 4, command: "configure", kind: "first",
      values: { stage_type: "swiss", round_count: 8, players_per_game: 3,
        place_points: ["4", "3", "2", "1"], score_multiplier: "0.02" } });
  });

  it("saves Classic round access, manual seeding, and stage starts with current versions", async () => {
    window.history.replaceState({}, "", "/manager/tournaments/ref/management");
    const stage = { kind: "first", stage_type: "groups", scheme_key: "groups-9-4", started_at: null as string | null,
      completed_at: null, seeds: [] as Array<Array<string | null>>, place_points: ["4", "3", "2", "1"], score_multiplier: "0.02", standings: [],
      rounds: [{ id: "round-1", number: 1, assignment_id: null, discoverable: true, playable: true, start_deadline: null, packet_locked: false, matches: [] }] };
    const resource = { kind: "manager_management", state: "ready", settings_version: 4, finalized_at: "2026-09-01",
      registration_scheduled_open: false, registration_open: false, registration_open_override: false,
      registration_count: 1, approved_count: 1, participant_count: 1, packet_count: 1, registrations: [],
      sections: ["general", "packet_accessibility", "packet_management", "first_stage", "playoff_stage", "first_round_seeding"],
      available_actions: ["packet_access", "registration_override", "mark_finished"],
      tournament: { id: "cup", name: "Cup", status: "active", type_key: "classic", ruleset_key: "si" },
      packets: [{ assignment_id: "packet-1", packet_id: "logical-1", name: "Round packet", version: 1, player_access: [] }],
      classic: { stages: [stage], players: [{ id: "player-1", name: "Ada" }], schemes: [{ id: "groups-9-4", kind: "groups", size: 9, round_count: 4 }] } };
    const mutations: Array<Record<string, any>> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01", locale: "en" });
      if (String(url).endsWith("/classic")) {
        const body = JSON.parse(String(options?.body));
        mutations.push(body);
        resource.settings_version++;
        if (body.command === "seed") stage.seeds = body.values.mode === "automatic" ? [["player-1", ...Array(8).fill(null)]] : body.values.seeds;
        if (body.command === "start") stage.started_at = "2026-09-11";
        return response(resource);
      }
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);
    const button = (label: string) => Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === label)!;
    shell.start();
    await vi.waitFor(() => expect(button("Start first stage")).toBeDefined());
    expect(button("Start first stage").disabled).toBe(false);
    button("First stage management").click();
    root.querySelector<HTMLSelectElement>(".classic-rounds select")!.value = "packet-1";
    const switches = root.querySelectorAll<HTMLInputElement>(".classic-rounds input[type=checkbox]");
    switches.forEach((s) => { expect(s.disabled).toBe(true); expect(s.checked).toBe(true); });
    expect(root.querySelector(".classic-rounds [role=status]")?.textContent).toBe(
      "Start this stage before making its packets discoverable or playable.",
    );
    expect(button("Save round").classList.contains("primary-button")).toBe(true);
    root.querySelector<HTMLInputElement>(".classic-rounds input[type=datetime-local]")!.value = "2026-10-01T12:00";
    button("Save round").click();
    await vi.waitFor(() => expect(mutations).toHaveLength(1));
    expect(mutations[0]).toMatchObject({ expected_version: 4, command: "round", kind: "first",
      values: { round_id: "round-1", assignment_id: "packet-1",
        start_deadline: new Date("2026-10-01T12:00").toISOString() } });
    await vi.waitFor(() => expect(button("First round seeding")?.disabled).toBe(false));
    button("First round seeding").click();
    expect(button("Automatic seeding").classList.contains("secondary-button")).toBe(true);
    button("Automatic seeding").click();
    await vi.waitFor(() => expect(root.querySelectorAll(".classic-seeding select")).toHaveLength(9));
    const slots = root.querySelectorAll<HTMLSelectElement>(".classic-seeding select");
    slots[0]!.value = "";
    slots[1]!.value = "player-1";
    button("Save manual seeding").click();
    await vi.waitFor(() => expect(mutations).toHaveLength(3));
    expect(mutations[2]).toMatchObject({ expected_version: 6, command: "seed", values: { mode: "manual", seeds: [[null, "player-1", ...Array(7).fill(null)]] } });
    await vi.waitFor(() => expect(button("General")?.disabled).toBe(false));
    button("General").click();
    button("Start first stage").click();
    await vi.waitFor(() => expect(mutations).toHaveLength(4));
    expect(mutations[3]).toMatchObject({ command: "start", kind: "first", expected_version: 7 });
    await vi.waitFor(() => expect(button("Start first stage")?.disabled).toBe(true));
    button("First stage management").click();
    expect(root.querySelector(".classic-rounds [role=status]")).toBeNull();
    root.querySelectorAll<HTMLInputElement>(".classic-rounds input[type=checkbox]").forEach((s) => {
      expect(s.disabled).toBe(false);
      s.checked = true;
      s.dispatchEvent(new Event("change"));
    });
    button("Save round").click();
    await vi.waitFor(() => expect(mutations).toHaveLength(5));
    expect(mutations[4]).toMatchObject({ expected_version: 8, command: "round", kind: "first",
      values: { discoverable: true, playable: true } });
    await vi.waitFor(() => expect(button("Game statuses")?.disabled).toBe(false));
    expect(button("Use packet defaults")).toBeUndefined();
    button("Game statuses").click();
    expect(document.querySelector("dialog")?.textContent).toContain("Game statuses");
  });

  it.each(["classic", "ladder"])("shows current registration and packet defaults for %s", async (typeKey) => {
    window.history.replaceState({}, "", "/manager/tournaments/ref/management");
    const resource = {
      kind: "manager_management", state: "ready", settings_version: 1, finalized_at: "2026-09-01",
      registration_open: true, registration_scheduled_open: true, registration_open_override: null,
      registration_count: 0, approved_count: 0, participant_count: 0, packet_count: 1,
      registrations: [], sections: ["general", "packet_accessibility", "packet_management"],
      available_actions: ["registration_override", "packet_access", ...(typeKey === "ladder" ? ["start_tournament"] : [])],
      tournament: { id: "cup", name: "Cup", type_key: typeKey, status: "active", actual_starts_at: null as string | null },
      packets: [{ assignment_id: "packet-1", packet_id: "logical-1", name: "Packet", player_access: [],
        library_viewing_rule: "after-play",
        default_access: { playable: true, discoverable: true, readable: true } }],
    };
    const mutations: Array<{ url: string; body: any }> = [];
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01", locale: "en" });
      if (options?.method === "POST") {
        const body = JSON.parse(String(options.body));
        mutations.push({ url: String(url), body });
        resource.settings_version++;
        if (String(url).endsWith("/start")) {
          resource.tournament.actual_starts_at = "2026-09-12";
          resource.available_actions = resource.available_actions.filter((a) => a !== "start_tournament");
        } else if (body.right === "library_viewing_rule") resource.packets[0]!.library_viewing_rule = body.library_viewing_rule;
        else resource.registration_open = body.registration_open;
        return response(resource);
      }
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed", fetcher), new Router(), new FakePlatform(), false);
    shell.start();
    const button = (label: string) => Array.from(root.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent === label);
    await vi.waitFor(() => expect(root.querySelector("[role=switch]")).not.toBeNull());
    expect(root.querySelector<HTMLInputElement>("[role=switch]")!.checked).toBe(true);
    if (typeKey === "ladder") {
      button("Start tournament")!.click();
      await vi.waitFor(() => expect(button("Tournament started")?.disabled).toBe(true));
      expect(mutations[0]).toMatchObject({ url: "/api/miniapp/manager/tournaments/ref/start", body: { expected_version: 1 } });
    } else expect(button("Start tournament")).toBeUndefined();
    const toggle = root.querySelector<HTMLInputElement>("[role=switch]")!;
    toggle.checked = false;
    toggle.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(resource.registration_open).toBe(false));
    await vi.waitFor(() => expect(button("Packet accessibility")?.disabled).toBe(false));
    expect(mutations.at(-1)?.body.registration_open).toBe(false);
    button("Packet accessibility")!.click();
    const defaults = root.querySelectorAll<HTMLInputElement>(".set-all-row input");
    expect(defaults).toHaveLength(typeKey === "classic" ? 1 : 3);
    defaults.forEach((input) => expect(input.checked).toBe(true));
    expect(root.querySelector(".packet-access-table-classic") !== null).toBe(typeKey === "classic");
    button("Packet management")!.click();
    const rule = root.querySelector<HTMLSelectElement>("[data-packet-id] select")!;
    expect(rule.value).toBe("after-play");
    expect(Array.from(rule.options, (option) => option.value)).toEqual(["never", "after-play", "anytime"]);
    expect(rule.parentElement?.textContent).toContain("Library viewing rule");
    rule.value = "anytime";
    rule.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(resource.packets[0]!.library_viewing_rule).toBe("anytime"));
    expect(mutations.at(-1)?.body).toMatchObject({
      assignment_id: "packet-1", right: "library_viewing_rule", library_viewing_rule: "anytime",
      expected_version: resource.settings_version - 1,
    });
  });

  it("associates packet authors, creates a lead author, and preserves theme edits", async () => {
    window.history.replaceState({}, "", "/manager/packets/opaque-reference/edit");
    const question = (text: string) => ({
      value: 10,
      form: "",
      text,
      answer: "Old answer",
      accepted_answers: [],
      commentary: "",
      source: "",
      author: "Ada",
    });
    const resource = {
      kind: "packet_draft",
      state: "ready",
      draft_id: "00000000-0000-0000-0000-000000000010",
      version: 2,
      status: "awaiting_confirmation",
      source_filename: "packet.json",
      ruleset_key: "si",
      ruleset_version: 1,
      errors: [],
      warnings: ["Missing source"],
      can_publish: true,
      can_reject: true,
      packet: {
        name: "Final",
        language: "en",
        lead_author: "Ada",
        year: 2026,
        themes: [
          { name: "Theme one", author: "Ada", questions: [question("First")] },
          { name: "Theme two", author: "Grace", questions: [question("Second")] },
        ],
      },
      editor: {
        schema: "si.packet.v1",
        page_collection: "themes",
        packet_fields: ["name", "language", "lead_author", "year"],
        theme_fields: ["name", "author"],
        question_fields: ["value", "text", "answer", "accepted_answers", "commentary", "source", "author"],
        question_values: [10],
      },
    } as const;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" }),
      )
      .mockResolvedValueOnce(
        response({ locale: "en", authorization: { allowed: true }, resource }),
      )
      .mockResolvedValueOnce(response({ items: [{ author_id: "registered-ada", display_name: "Ada Lovelace" }] }))
      .mockResolvedValueOnce(response({ author_id: "new-lead", display_name: "New Editor" }))
      .mockResolvedValueOnce(response({ ...resource, version: 3 }));
    const root = document.createElement("div");
    document.body.append(root);
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform(), false);

    shell.start();
    await vi.waitFor(() => expect(root.textContent).toContain("Theme one"));
    const answer = root.querySelector<HTMLTextAreaElement>("[data-question-field=answer]");
    expect(answer).not.toBeNull();
    if (answer) answer.value = "New answer";
    expect(Array.from(root.querySelectorAll("[data-packet-author] h3")).map((item) => item.textContent))
      .toEqual(["Ada", "Grace"]);
    const authorSelect = root.querySelector<HTMLSelectElement>('[data-packet-author="Ada"] select')!;
    authorSelect.dispatchEvent(new Event("focus"));
    await vi.waitFor(() => expect(authorSelect.textContent).toContain("Ada Lovelace"));
    authorSelect.value = "registered-ada";
    authorSelect.dispatchEvent(new Event("change", { bubbles: true }));
    expect(Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Publish packet")?.disabled).toBe(true);

    const createLead = root.querySelector<HTMLButtonElement>("fieldset > .packet-author-picker button")!;
    createLead.click();
    const dialog = document.querySelector<HTMLDialogElement>("dialog")!;
    Object.defineProperty(dialog, "close", { value: () => dialog.remove() });
    dialog.querySelector<HTMLInputElement>('[name="first_name"]')!.value = "New";
    dialog.querySelector<HTMLInputElement>('[name="surname"]')!.value = "Editor";
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector<HTMLInputElement>("[data-field=lead_author]")!.value).toBe("New Editor"));
    expect(fetcher.mock.calls[3]?.[0]).toBe("/api/miniapp/manager/packets/opaque-reference/authors");
    expect(JSON.parse(String(fetcher.mock.calls[3]?.[1]?.body))).toMatchObject({ first_name: "New", surname: "Editor" });
    const next = Array.from(root.querySelectorAll<HTMLButtonElement>(".packet-page-nav button"))
      .find((button) => button.textContent === "Next");
    next?.click();
    expect(root.textContent).toContain("Theme two");
    (root.querySelector("form.packet-editor") as HTMLFormElement).requestSubmit();

    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
    const submitted = JSON.parse(String(fetcher.mock.calls[4]?.[1]?.body));
    expect(submitted.expected_version).toBe(2);
    expect(submitted.content.themes[0].questions[0].answer).toBe("New answer");
    expect(submitted.content.themes[1].name).toBe("Theme two");
    expect(submitted.author_bindings).toEqual({ Ada: "registered-ada" });
    expect(submitted.lead_author_id).toBe("new-lead");
    expect(submitted.content.lead_author).toBe("New Editor");
  });

  function ongoingShell(confirm: boolean, managed = false) {
    window.history.replaceState({}, "", "/ongoing");
    vi.spyOn(window, "confirm").mockReturnValue(confirm);
    const resource = {
      kind: "ongoing",
      state: "ready",
      lobbies: [{
        id: "lobby-1", version: 3, tournament_id: "cup-id", tournament_name: "Autumn Cup",
        invitation_code: "invite-code", max_players: 4, searching: false,
        expires_at: "2099-01-01T00:00:00Z",
        members: [{ display_name: "Alice", role: "player", ready: true }],
        selected_packets: [{
          packet_id: "packet", name: "Selected packet", lead_author: null, year: 2020,
          fresh_play_unit_count: 2, total_play_unit_count: 5, playable_for_all: true,
        }],
        is_member: false, viewer_role: null, viewer_manages: managed,
      }],
      games: [{
        id: "game-1", tournament_id: "cup-id", tournament_name: "Autumn Cup",
        status: "active", phase: "reading", participant_count: 2,
        participants: ["Alice", "Bob"], observing: false, observing_policy: "forbidden",
        managed, fresh_content_count: 2, confirmation_required: true, can_observe: true,
      }],
    };
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
      if (String(url).endsWith("/ongoing/lobbies/join")) return response({ joined: true });
      if (String(url).endsWith("/observe")) {
        const body = JSON.parse(String(options?.body));
        if (!body.confirm_fresh) {
          return response({ game_id: "game-1", joined: false, confirmation_required: true, fresh_content_count: 2 });
        }
        return response({ game_id: "game-1", joined: true, confirmation_required: false, fresh_content_count: 2 });
      }
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    const platform = new FakePlatform();
    shell = new MiniAppShell(root, new ApiClient("signed-init-data", fetcher), new Router(), platform, false);
    shell.start();
    return { root, fetcher, platform };
  }

  function ongoingButton(root: HTMLElement, label: string): HTMLButtonElement {
    const button = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((item) => item.textContent === label);
    expect(button).toBeDefined();
    return button!;
  }

  it("renders ongoing lobby and game cards with join and observe actions", async () => {
    const { root } = ongoingShell(true);
    await vi.waitFor(() => expect(root.textContent).toContain("Autumn Cup"));
    expect(root.textContent).toContain("Join as player");
    expect(root.textContent).toContain("Join as observer");
    expect(root.textContent).toContain("Watch as observer");
    expect(root.textContent).toContain("Selected packet");
    expect(root.textContent).toContain("Alice");
    expect(root.querySelectorAll("article.resource-card")).toHaveLength(2);
  });

  it("joins an ongoing lobby and returns to the bot", async () => {
    const { root, fetcher, platform } = ongoingShell(true);
    await vi.waitFor(() => expect(root.textContent).toContain("Join as observer"));
    ongoingButton(root, "Join as observer").click();
    await vi.waitFor(() => {
      const calls = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/ongoing/lobbies/join"));
      expect(calls.map(([, options]) => JSON.parse(String(options?.body)))).toEqual([
        { invitation_code: "invite-code", role: "observer" },
      ]);
    });
    expect(platform.returnToBot).toHaveBeenCalled();
  });

  it("requires confirmation before burning fresh content to observe a game", async () => {
    const { root, fetcher, platform } = ongoingShell(true);
    await vi.waitFor(() => expect(root.textContent).toContain("Watch as observer"));
    ongoingButton(root, "Watch as observer").click();
    await vi.waitFor(() => expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("burn that content")));
    const calls = fetcher.mock.calls.filter(([url]) => String(url).endsWith("/observe"));
    expect(calls.map(([, options]) => JSON.parse(String(options?.body)))).toEqual([
      { confirm_fresh: false },
      { confirm_fresh: true },
    ]);
    expect(platform.returnToBot).toHaveBeenCalled();
  });

  it("does not observe a game when the fresh-content warning is declined", async () => {
    const { root, fetcher, platform } = ongoingShell(false);
    await vi.waitFor(() => expect(root.textContent).toContain("Watch as observer"));
    ongoingButton(root, "Watch as observer").click();
    await vi.waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith("/observe"))).toHaveLength(1);
    expect(platform.returnToBot).not.toHaveBeenCalled();
  });

  it("shows a managing note instead of lobby join buttons for managers", async () => {
    const { root } = ongoingShell(true, true);
    await vi.waitFor(() => expect(root.textContent).toContain("You manage this tournament"));
    expect(root.textContent).not.toContain("Join as player");
    expect(root.textContent).not.toContain("Join as observer");
    expect(root.textContent).toContain("Watch as observer");
  });
});
