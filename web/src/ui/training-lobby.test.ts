import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { I18n } from "../i18n";
import { renderTraining } from "./training";

const controllers: AbortController[] = [];
afterEach(() => controllers.splice(0).forEach((controller) => controller.abort()));
const lobby = { version: 3, version_id: "one", packet_name: "Saved packet", settings: {
  reversed: true, hidden_after: 2, buzz_timeout: 8, best_buzz: false,
} };

function create(upload = false) {
  const controller = new AbortController(); controllers.push(controller);
  const request = vi.fn(async (path: string) => path.includes("/packets?")
    ? { items: [{ id: "two", name: "New packet", packet_id: "two", scope: "tournament" }], next_cursor: null }
    : { lobby: { ...lobby, version: 4 } });
  const root = renderTraining({ session: null, lobby }, { request } as unknown as ApiClient,
    new I18n("en"), controller.signal, upload);
  return { root, request, controller };
}

describe("training lobby", () => {
  it("uses the regular lobby sections and restores saved settings", () => {
    const { root } = create();
    expect([...root.querySelectorAll("nav button")].map((button) => button.textContent))
      .toEqual(["Overview", "Packets", "Options"]);
    const boxes = root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    expect(boxes[0]!.checked).toBe(true);
    expect(boxes[1]!.checked).toBe(true);
    expect(boxes[2]!.disabled).toBe(true);
    expect(root.textContent).toContain("Saved packet");
  });

  it("selects without starting, then explicitly saves the packet and modifiers", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(root.querySelector("article")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    expect(request).toHaveBeenCalledTimes(1);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(request.mock.calls[1]![0]).toBe("/api/miniapp/training/lobby");
    const options = (request.mock.calls[1] as unknown as [string, { body: unknown }])[1];
    expect(options.body).toMatchObject({ command: "save", expected_version: 3, version_id: "two",
      settings: { reversed: true, hidden_after: 2, buzz_timeout: 8, best_buzz: false } });
    await vi.waitFor(() => expect(root.textContent).toContain("Return to Telegram"));
    expect(request.mock.calls.some(([path]) => path.endsWith("/start"))).toBe(false);
  });

  it("shows upload controls separately without loading the packet picker", () => {
    const { root, request } = create(true);
    expect(root.querySelector('input[type="file"]')).not.toBeNull();
    expect(root.querySelector("nav")).toBeNull();
    expect(root.querySelector("fieldset")).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it("does not report success for stale saves", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(root.querySelector("article")).not.toBeNull());
    request.mockRejectedValueOnce(new ApiError("stale_write", 409, false));
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector('[role="status"]')!.textContent).not.toBe(""));
    expect(root.textContent).not.toContain("Saved. Return");
  });
});
