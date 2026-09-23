import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./api/client";
import { MiniAppShell } from "./app";
import { Router } from "./routing/router";
import type { MiniAppPlatform } from "./platform/telegram";

const response = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});

describe("management shell", () => {
  let shell: MiniAppShell;
  afterEach(() => { shell?.stop(); document.body.replaceChildren(); sessionStorage.clear(); vi.restoreAllMocks(); });
  function start(section: string, items: unknown[], fetcher: typeof fetch): HTMLElement {
    window.history.replaceState({}, "", `/admin/management?section=${section}`);
    const platform: MiniAppPlatform = {
      initData: "signed", isTelegram: true, initialize: vi.fn(), destroy: vi.fn(),
      setBackHandler: vi.fn(), setMainAction: vi.fn(), notifySuccess: vi.fn(),
      notifyError: vi.fn(), returnToBot: vi.fn(),
    };
    const root = document.createElement("div"); document.body.append(root);
    const api = new ApiClient(platform.initData, vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
      if (String(url).endsWith("/session")) return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01", locale: "en" });
      if (String(url).includes("/routes/resolve")) return response({ locale: "en", authorization: { allowed: true },
        resource: { kind: "admin_management", state: "ready", section, items } });
      return fetcher(url, options);
    }));
    shell = new MiniAppShell(root, api, new Router(), platform, false); shell.start();
    return root;
  }

  it.each(["halt", "resume", "abolish"])("requires explicit confirmation for %s and sends the current version", async (command) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({}));
    const root = start("tournaments", [{ id: "t1", name: "Cup", status: "active",
      moderation_status: command === "resume" ? "halted" : "normal",
      actual_starts_at: "2026-01-01", settings_version: 7 }], fetcher);
    const label = command[0]!.toUpperCase() + command.slice(1);
    await vi.waitFor(() => expect(root.textContent).toContain(label));
    const button = [...root.querySelectorAll("button")].find(b => b.textContent === label)!;
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    button.click(); expect(fetcher).not.toHaveBeenCalled();
    confirm.mockReturnValue(true); button.click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
    expect(String(fetcher.mock.calls[0]![0])).toContain(`/admin/management/tournaments/t1/${command}`);
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]?.body))).toEqual({ confirm: true, expected_version: 7 });
  });

  it("saves the tournament rating weight through the explicit slider save button", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({}));
    const root = start("tournaments", [{ id: "t1", name: "Cup", status: "active",
      moderation_status: "normal", actual_starts_at: "2026-01-01", settings_version: 7,
      settings: { policies: { ruleset_rating_weight: 1 } } }], fetcher);
    await vi.waitFor(() => expect(root.querySelector("input[type=range]")).toBeTruthy());
    const slider = root.querySelector<HTMLInputElement>("input[type=range]")!;
    const save = [...root.querySelectorAll("button")].find(b => b.textContent === "V")!;
    expect((save as HTMLButtonElement).disabled).toBe(true);
    slider.value = "0.5";
    slider.dispatchEvent(new Event("input"));
    (save as HTMLButtonElement).click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
    expect(String(fetcher.mock.calls[0]![0])).toContain("/admin/management/tournaments/t1/rating_weight");
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]?.body))).toEqual({ weight: 0.5, expected_version: 7 });
  });

  it("uses administrative packet access with confirmation and the existing reader", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response({ confirmation_required: true, fresh_unit_count: 1 }))
      .mockResolvedValueOnce(response({ confirmation_required: false, name: "Secret", pages: [
        { title: "Theme", author: "", questions: [{ value: 10, text: "Question", answer: "Answer", accepted_answers: [] }] },
      ] }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const root = start("packets", [{ id: "v1", name: "Secret" }], fetcher);
    await vi.waitFor(() => expect(root.textContent).toContain("View"));
    [...root.querySelectorAll("button")].find(b => b.textContent === "View")!.click();
    await vi.waitFor(() => expect(root.querySelector(".library-question")?.textContent).toContain("Question"));
    expect(fetcher.mock.calls.map(call => String(call[0]))).toEqual([
      "/api/miniapp/admin/management/packets/v1/view", "/api/miniapp/admin/management/packets/v1/view",
    ]);
    expect(fetcher.mock.calls.map(call => JSON.parse(String(call[1]?.body)))).toEqual([
      { confirm: false }, { confirm: true },
    ]);
  });

  it("approves a pending author link request after confirmation", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({}));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const root = start("link_requests", [{ id: "r1", name: "Ada Lovelace", status: "pending",
      player: { id: "p1", public_nickname: "Alice" }, author: { id: "a1", display_name: "Ada Lovelace" } }], fetcher);
    await vi.waitFor(() => expect(root.textContent).toContain("Approve"));
    [...root.querySelectorAll("button")].find(b => b.textContent === "Approve")!.click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
    expect(String(fetcher.mock.calls[0]![0])).toContain("/admin/management/link_requests/r1/approve");
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]?.body))).toEqual({ approve: true });
  });

  it("rejects a pending author link request after confirmation", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({}));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const root = start("link_requests", [{ id: "r1", name: "Ada Lovelace", status: "pending",
      player: { id: "p1", public_nickname: "Alice" }, author: { id: "a1", display_name: "Ada Lovelace" } }], fetcher);
    await vi.waitFor(() => expect(root.textContent).toContain("Reject"));
    [...root.querySelectorAll("button")].find(b => b.textContent === "Reject")!.click();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
    expect(String(fetcher.mock.calls[0]![0])).toContain("/admin/management/link_requests/r1/reject");
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]?.body))).toEqual({ approve: false });
  });

  it("joins authors through a searchable dialog with explicit confirmation", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url) => {
      if (String(url).includes("/api/miniapp/authors?query=")) {
        return response({ items: [
          { author_id: "a1", display_name: "Ada Lovelace", authorship: null },
          { author_id: "a2", display_name: "Grace Hopper", authorship: null },
        ], next_cursor: null });
      }
      return response({ merged: true });
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const root = start("authors", [{ id: "a1", display_name: "Ada Lovelace" }], fetcher);
    await vi.waitFor(() => expect(root.textContent).toContain("Join"));
    [...root.querySelectorAll<HTMLButtonElement>("article button")].find(b => b.textContent === "Join")!.click();
    const dialog = document.querySelector<HTMLDialogElement>("dialog")!;
    const select = dialog.querySelector<HTMLSelectElement>("select")!;
    await vi.waitFor(() => expect(select.textContent).toContain("Grace Hopper"));
    expect(select.textContent).not.toContain("Ada Lovelace");
    select.value = "a2";
    dialog.querySelector<HTMLFormElement>("form")!.requestSubmit();
    const mergeCall = await vi.waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => String(url).endsWith("/admin/management/authors/a1/merge"));
      expect(call).toBeDefined();
      return call!;
    });
    expect(JSON.parse(String(mergeCall[1]?.body))).toEqual({ merge_author_id: "a2", confirm: true });
  });
});
