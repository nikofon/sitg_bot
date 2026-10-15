import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api/client";
import type { TournamentManagerManagementResource } from "./api/types";
import { MiniAppShell } from "./app";
import { TelegramPlatform } from "./platform/telegram";
import { Router } from "./routing/router";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
}

describe("management loading", () => {
  let shell: MiniAppShell;
  afterEach(() => {
    shell?.stop();
    document.body.replaceChildren();
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  function setup(packetCount = 2) {
    window.history.replaceState({}, "", "/manager/tournaments/ref/management");
    const resource: TournamentManagerManagementResource = {
      kind: "manager_management", state: "ready", settings_version: 1,
      sections: ["general", "registrations", "packet_accessibility", "packet_management"],
      registration_scheduled_open: false, registration_open: false, registration_open_override: null,
      registration_count: 0, approved_count: 0, participant_count: 1, packet_count: packetCount,
      registrations: [], available_actions: ["packet_access", "packet_management"],
      packets: Array.from({ length: packetCount }, (_, i) => ({
        assignment_id: `a${i}`, packet_id: `p${i}`, name: `Packet ${i}`, version: 1,
        player_access: null,
      })),
      tournament: {
        id: "t1", name: "Cup", slug: "cup", status: "active", visibility: "private",
        phase: "ongoing", language: "en", payment_type: "free", pricing_plans: [],
        registration_open: false, authors: [], type_key: "ladder", type_version: 1,
        ruleset_key: "si", ruleset_version: 1, managed: true, policy_version: 1,
        available_actions: [],
      },
    };
    const accessRow = (name = "Participant", playable = true) => ({
      player_id: "player", display_name: name, playable, discoverable: true, readable: false,
    });
    const access = vi.fn<(id: string, options?: RequestInit) => Promise<Response>>()
      .mockImplementation(async (id) => response({ assignment_id: id, player_access: [accessRow()] }));
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({
        csrf_token: "csrf-token", expires_at: "2099-01-01T00:00:00Z", locale: "en",
      });
      if (String(url).endsWith("/access")) {
        return access(String(url).split("/").at(-2)!, options);
      }
      if (options?.method === "POST") return response(resource);
      return response({ locale: "en", authorization: { allowed: true }, resource });
    });
    const root = document.createElement("div");
    document.body.append(root);
    const router = new Router();
    shell = new MiniAppShell(root, new ApiClient("signed-init", fetcher), router, new TelegramPlatform(), false);
    const started = performance.now();
    shell.start();
    const section = (label: string) => Array.from(root.querySelectorAll<HTMLButtonElement>(
      ".management-section-nav button",
    )).find((button) => button.textContent === label)!.click();
    const select = (id: string) => {
      const picker = root.querySelector<HTMLSelectElement>(".packet-accessibility select")!;
      picker.value = id;
      picker.dispatchEvent(new Event("change"));
    };
    return { resource, root, router, fetcher, access, accessRow, section, select, started };
  }

  it("opens 200 packets without constructing hidden cards or fetching access", async () => {
    const createElement = vi.spyOn(document, "createElement");
    const { root, fetcher, access, section, started } = setup(200);
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    const initialMs = performance.now() - started;
    expect(String(fetcher.mock.calls[1]![0])).toContain("include_player_access=false");
    expect(access).not.toHaveBeenCalled();
    expect(createElement.mock.calls.some(([tag]) => tag === "article")).toBe(false);
    const cardsStarted = performance.now();
    section("Packet management");
    const cardsMs = performance.now() - cardsStarted;
    expect(root.querySelectorAll(".lobby-packet-card")).toHaveLength(200);
    section("General");
    section("Packet management");
    expect(root.querySelectorAll(".lobby-packet-card")).toHaveLength(200);
    if (import.meta.env.VITE_MANAGEMENT_BENCHMARK === "1") {
      console.info(JSON.stringify({ packets: 200, initial_ms: initialMs, packet_panel_ms: cardsMs,
        environment: "jsdom; initial time includes asynchronous test polling" }));
    }
  });

  it("caches selected access and refetches it after a mutation", async () => {
    const { root, fetcher, access, section, select } = setup();
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    section("Packet accessibility");
    await vi.waitFor(() => expect(root.querySelector(".packet-access-table")).not.toBeNull());
    select("a1");
    await vi.waitFor(() => expect(access).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(root.querySelector(".packet-access-table")).not.toBeNull());
    select("a0");
    expect(access).toHaveBeenCalledTimes(2);
    root.querySelector<HTMLInputElement>(".packet-access-table tbody tr:last-child input")!.click();
    await vi.waitFor(() => expect(access).toHaveBeenCalledTimes(3));
    const mutation = fetcher.mock.calls.find(([, options]) => options?.method === "POST"
      && String(options.body).includes("assignment_id"))!;
    expect(JSON.parse(String(mutation[1]!.body))).toMatchObject({
      assignment_id: "a0", include_player_access: false,
    });
    await vi.waitFor(() => expect(root.querySelector(".packet-access-table")).not.toBeNull());
    expect(root.querySelector<HTMLSelectElement>(".packet-accessibility select")!.value).toBe("a0");
  });

  it("aborts older selections and ignores late responses", async () => {
    const { root, access, accessRow, section, select } = setup();
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    let resolveOld!: (value: Response) => void;
    access.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    section("Packet accessibility");
    await vi.waitFor(() => expect(access).toHaveBeenCalledTimes(1));
    expect(root.textContent).not.toContain("No participating players");
    select("a1");
    await vi.waitFor(() => expect(root.querySelector(".packet-access-table")).not.toBeNull());
    expect(access.mock.calls[0]![1]!.signal!.aborted).toBe(true);
    resolveOld(response({ assignment_id: "a0", player_access: [accessRow("Old player")] }));
    await Promise.resolve();
    await Promise.resolve();
    expect(root.textContent).not.toContain("Old player");
    expect(root.querySelector<HTMLSelectElement>(".packet-accessibility select")!.value).toBe("a1");
  });

  it("retries errors inside the access panel and distinguishes an empty result", async () => {
    const { root, access, section } = setup();
    access.mockRejectedValueOnce(new Error("offline"));
    access.mockResolvedValueOnce(response({ assignment_id: "a0", player_access: [] }));
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    section("Packet accessibility");
    const retry = () => Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent === "Try again");
    await vi.waitFor(() => expect(retry()).toBeDefined());
    expect(root.querySelectorAll(".management-section-nav button")).toHaveLength(4);
    retry()!.click();
    await vi.waitFor(() => expect(root.querySelector(".empty-table-cell")).not.toBeNull());
    expect(access).toHaveBeenCalledTimes(2);
  });

  it("cancels reads when leaving a section or replacing the view", async () => {
    const { root, access, section, router } = setup();
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    let resolveOld!: (value: Response) => void;
    access.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    section("Packet accessibility");
    await vi.waitFor(() => expect(access).toHaveBeenCalledTimes(1));
    section("General");
    expect(access.mock.calls[0]![1]!.signal!.aborted).toBe(true);
    section("Packet accessibility");
    await vi.waitFor(() => expect(access).toHaveBeenCalledTimes(2));
    router.navigate("/manager/tournaments/other/management");
    await vi.waitFor(() => expect(root.querySelector("[data-section=general]")).not.toBeNull());
    resolveOld(response({ assignment_id: "a0", player_access: [] }));
    await Promise.resolve();
    expect(root.querySelector("[data-section=packet_accessibility]")).toBeNull();
  });
});
