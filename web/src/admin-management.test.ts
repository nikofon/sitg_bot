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
});
