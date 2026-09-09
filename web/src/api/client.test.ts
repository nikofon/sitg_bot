import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "./client";

const session = {
  csrf_token: "csrf-test-token",
  expires_at: "2099-01-01T00:00:00Z",
  locale: "en",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ApiClient", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("authenticates and protects mutations", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    const client = new ApiClient("signed-init-data", fetcher);

    await client.request("/api/miniapp/action", { method: "POST", body: { value: 1 } });

    const authentication = fetcher.mock.calls[0];
    expect(authentication?.[0]).toBe("/api/miniapp/session");
    const mutation = fetcher.mock.calls[1]?.[1];
    expect(new Headers(mutation?.headers).get("X-CSRF-Token")).toBe("csrf-test-token");
    expect(new Headers(mutation?.headers).get("X-Idempotency-Key")?.length).toBeGreaterThanOrEqual(8);
    expect(mutation?.credentials).toBe("include");
  });

  it("refreshes and retries once after an expired cookie", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(jsonResponse({ error: { code: "authentication_required" } }, 401))
      .mockResolvedValueOnce(jsonResponse({ ...session, csrf_token: "rotated-token" }))
      .mockResolvedValueOnce(jsonResponse({ items: [] }));
    const client = new ApiClient("signed-init-data", fetcher);

    await expect(client.request("/api/miniapp/items")).resolves.toEqual({ items: [] });
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      "/api/miniapp/session",
      "/api/miniapp/items",
      "/api/miniapp/session/refresh",
      "/api/miniapp/items",
    ]);
  });

  it("maps server failures to stable errors without exposing server text", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(session))
      .mockResolvedValueOnce(
        jsonResponse({ error: { code: "forbidden", message: "sensitive details" } }, 403),
      );
    const client = new ApiClient("signed-init-data", fetcher);

    const failure = await client.request("/api/miniapp/private").catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "forbidden", message: "forbidden" });
  });
});
