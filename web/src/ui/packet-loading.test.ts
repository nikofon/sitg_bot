import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PacketLoader, type PacketPage } from "./packet-loading";

type Packet = { packet_id: string; fresh: number };
const packet = (id: string, fresh = 1): Packet => ({ packet_id: id, fresh });

describe("PacketLoader", () => {
  let controller: AbortController;
  beforeEach(() => {
    vi.useFakeTimers();
    controller = new AbortController();
  });
  afterEach(() => {
    controller.abort();
    vi.useRealTimers();
  });

  it("loads sequential batches of 25 with a 200 ms pause and reconciles later", async () => {
    const fetch = vi.fn(async (query: URLSearchParams) => {
      const start = Number(query.get("cursor") ?? 0);
      return { items: Array.from({ length: Math.min(25, 51 - start) }, (_, i) => packet(String(start + i))),
        next_cursor: start < 50 ? String(start + 25) : null };
    });
    const update = vi.fn();
    const loader = new PacketLoader(controller.signal, fetch, update, () => false);
    loader.start({ author: "Anna" });
    await vi.advanceTimersByTimeAsync(0);
    expect(update).toHaveBeenLastCalledWith(expect.any(Array), true);
    expect(update.mock.lastCall?.[0]).toHaveLength(25);
    expect(fetch.mock.calls[0]?.[0].get("limit")).toBe("25");
    expect(fetch.mock.calls[0]?.[0].get("author")).toBe("Anna");
    await vi.advanceTimersByTimeAsync(199);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(201);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(update.mock.lastCall?.[0]).toHaveLength(51);
    expect(update.mock.lastCall?.[1]).toBe(false);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("debounces rapid filter changes and discards obsolete late responses", async () => {
    let finishOld!: (page: PacketPage<Packet>) => void;
    const fetch = vi.fn((_query: URLSearchParams, _signal: AbortSignal): Promise<PacketPage<Packet>> =>
      new Promise((resolve) => { finishOld = resolve; }));
    const update = vi.fn();
    const loader = new PacketLoader(controller.signal, fetch, update, () => false);
    loader.start({ name: "old" });
    const oldSignal = fetch.mock.calls[0]![1];
    loader.start({ name: "n" }, true);
    await vi.advanceTimersByTimeAsync(100);
    loader.start({ name: "new" }, true);
    expect(oldSignal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(249);
    expect(fetch).toHaveBeenCalledTimes(1);
    fetch.mockResolvedValue({ items: [packet("new")], next_cursor: null });
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]![0].get("name")).toBe("new");
    finishOld({ items: [packet("old")], next_cursor: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(update).toHaveBeenLastCalledWith([packet("new")], false);
  });

  it("protects a blocked card from pages fetched before its mutation", async () => {
    let resolve!: (page: PacketPage<Packet>) => void;
    const fetch = vi.fn(() => new Promise<PacketPage<Packet>>((done) => { resolve = done; }));
    const update = vi.fn();
    const loader = new PacketLoader(controller.signal, fetch, update, () => false, [packet("a")]);
    loader.start({});
    loader.protect("a");
    loader.finish("a", packet("a", 0));
    resolve({ items: [packet("a", 10), packet("b", 3)], next_cursor: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(update).toHaveBeenLastCalledWith([packet("a", 0), packet("b", 3)], false);
  });

  it("retries transient failures on the same page and removes revoked entries on reconciliation", async () => {
    const fetch = vi.fn<() => Promise<PacketPage<Packet>>>()
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce({ items: [packet("a"), packet("b")], next_cursor: null })
      .mockResolvedValue({ items: [packet("b")], next_cursor: null });
    const update = vi.fn();
    const loader = new PacketLoader(controller.signal, fetch, update, () => false);
    loader.start({});
    await vi.advanceTimersByTimeAsync(1000);
    expect(update).toHaveBeenLastCalledWith([packet("a"), packet("b")], false);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(update).toHaveBeenLastCalledWith([packet("b")], false);
  });

  it("stops on permission failure and on route cancellation", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("forbidden"));
    const failed = vi.fn(() => true);
    const loader = new PacketLoader(controller.signal, fetch, vi.fn(), failed);
    loader.start({});
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(failed).toHaveBeenCalledTimes(1);
    loader.start({}, true);
    controller.abort();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("can update a single packet without starting background requests outside the picker", async () => {
    const fetch = vi.fn();
    const update = vi.fn();
    const loader = new PacketLoader<Packet>(controller.signal, fetch, update, () => false, [], false);
    loader.start({});
    loader.reconcile();
    expect(loader.protect("a")).toBe(true);
    loader.finish("a", packet("a", 0));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).not.toHaveBeenCalled();
    expect(update).toHaveBeenLastCalledWith([packet("a", 0)], false);
  });
});
