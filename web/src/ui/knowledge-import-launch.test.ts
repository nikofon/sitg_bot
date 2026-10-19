import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api/client";
import { MiniAppShell } from "../app";
import type { MiniAppPlatform } from "../platform/telegram";
import { Router } from "../routing/router";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

class FakePlatform implements MiniAppPlatform {
  constructor(readonly initData: string) {}
  readonly isTelegram = true;
  initialize = vi.fn();
  destroy = vi.fn();
  setBackHandler = vi.fn();
  setMainAction = vi.fn();
  notifySuccess = vi.fn();
  notifyError = vi.fn();
  returnToBot = vi.fn();
}

const knowledgePayload = {
  locale: "en",
  authorization: { allowed: true },
  resource: {
    kind: "knowledge",
    state: "ready",
    facts: {
      state: "ready", order: "title_asc", total: 0, next_cursor: null, items: [],
    },
    sharing: { grants: [], shared: [], links: [] },
    dissection: { requests: [], packets: [], next_cursor: null },
  },
};

function authenticatedFetcher(): ReturnType<typeof vi.fn> {
  return vi.fn<typeof fetch>().mockImplementation(async (url, options) => {
    const path = String(url);
    if (path.endsWith("/session")) {
      return response({ csrf_token: "csrf-test-token", expires_at: "2099-01-01T00:00:00Z", locale: "en" });
    }
    if (path.includes("/routes/resolve")) return response(knowledgePayload);
    if (path.endsWith("/knowledge/tags")) return response({ items: [] });
    if (path.endsWith("/training/entitlements")) return response({ features: {} });
    if (path.endsWith("/knowledge/import") && options?.method === "POST") {
      return response({ owner_id: "owner-1", recipient_id: "me", imported_count: 2, skipped_count: 1 });
    }
    throw new Error(`unexpected request: ${path}`);
  });
}

const flush = async (): Promise<void> => {
  for (let index = 0; index < 200; index += 1) await Promise.resolve();
};

describe("recipient import link launch evidence", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
    document.body.replaceChildren();
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("blocks import when the shared URL is opened without Telegram initData (external/in-app browser)", async () => {
    const fetcher = vi.fn<typeof fetch>();
    window.history.replaceState({}, "", "/knowledge#import=token-1");
    const root = document.createElement("div");
    document.body.append(root);
    const shell = new MiniAppShell(
      root, new ApiClient("", fetcher), new Router(), new FakePlatform(""), false,
    );
    shell.start();
    await flush();
    shell.stop();

    // No request of any kind leaves the page: no session bootstrap, no import mutation.
    expect(fetcher).not.toHaveBeenCalled();
    expect(root.querySelector(".status-card.status-error")).not.toBeNull();
    expect(document.querySelector(".knowledge-dialog")).toBeNull();
    // The token stays in the fragment for a real Mini App launch of the same URL.
    expect(window.location.hash).toBe("#import=token-1");
  });

  it("completes the recipient transition when the bot WebApp button launches /knowledge with the import fragment", async () => {
    const fetcher = authenticatedFetcher();
    window.history.replaceState({}, "", "/knowledge#import=token-1");
    const root = document.createElement("div");
    document.body.append(root);
    const shell = new MiniAppShell(
      root, new ApiClient("signed-init-data", fetcher), new Router(), new FakePlatform("signed-init-data"), false,
    );
    shell.start();
    await flush();

    const dialog = document.querySelector<HTMLDialogElement>(".knowledge-dialog")!;
    expect(dialog.textContent).toContain("Import shared library");
    [...dialog.querySelectorAll("button")].find((item) => item.textContent === "Import")!.click();
    await flush();
    shell.stop();

    const importCall = fetcher.mock.calls.find(([url, options]) =>
      String(url).endsWith("/knowledge/import") && options?.method === "POST");
    expect(importCall).toBeDefined();
    expect(JSON.parse(String(importCall![1]!.body))).toEqual({ token: "token-1" });
    expect(dialog.textContent).toContain("Imported 2 facts, skipped 1 already-known facts.");
    // The bearer token is consumed from the fragment after the confirmed import.
    expect(window.location.hash).toBe("");
  });
});
