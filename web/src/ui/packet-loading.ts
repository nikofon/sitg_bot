export interface PacketPage<T> {
  items: T[];
  next_cursor: string | null;
}

/** One cancellable, paced sequence, with packet mutations protected from late pages. */
export class PacketLoader<T extends { packet_id: string }> {
  private items = new Map<string, T>();
  private touched = new Map<string, number>();
  private pending = new Set<string>();
  private revision = 0;
  private sequence = 0;
  private controller?: AbortController;
  private timer?: number;
  private filters: Record<string, string> = {};

  constructor(
    private readonly signal: AbortSignal,
    private readonly fetchPage: (query: URLSearchParams, signal: AbortSignal) => Promise<PacketPage<T>>,
    private readonly update: (items: T[], loading: boolean) => void,
    private readonly failed: (error: unknown) => boolean,
    initial: T[] = [],
  ) {
    initial.forEach((item) => this.items.set(item.packet_id, item));
    signal.addEventListener("abort", () => this.stop(), { once: true });
  }

  start(filters: Record<string, string>, debounce = false): void {
    this.stop();
    this.filters = { ...filters };
    if (this.signal.aborted) return;
    this.update([...this.items.values()], true);
    if (debounce) this.timer = window.setTimeout(() => void this.run(), 250);
    else void this.run();
  }

  reconcile(): void {
    if (!this.controller && !this.signal.aborted) {
      window.clearTimeout(this.timer);
      void this.run();
    }
  }

  protect(packetId: string): boolean {
    if (this.pending.has(packetId)) return false;
    this.pending.add(packetId);
    this.touched.set(packetId, ++this.revision);
    return true;
  }

  finish(packetId: string, item?: T): void {
    this.pending.delete(packetId);
    this.touched.set(packetId, ++this.revision);
    if (item) this.items.set(packetId, item);
    else this.items.delete(packetId);
    this.update([...this.items.values()], !!this.controller);
  }

  stop(): void {
    ++this.sequence;
    this.controller?.abort();
    this.controller = undefined;
    window.clearTimeout(this.timer);
  }

  private async run(): Promise<void> {
    if (this.signal.aborted) return;
    const controller = new AbortController();
    this.controller = controller;
    const sequence = ++this.sequence;
    const active = (): boolean => !this.signal.aborted && !controller.signal.aborted
      && sequence === this.sequence;
    this.update([...this.items.values()], true);
    const startedRevision = this.revision;
    const seen = new Set<string>();
    let cursor: string | null = null;
    try {
      while (active()) {
        const query = new URLSearchParams(Object.entries(this.filters).filter(([, value]) => !!value));
        query.set("limit", "25");
        if (cursor) query.set("cursor", cursor);
        const revision = this.revision;
        let page: PacketPage<T>;
        try {
          page = await this.fetchPage(query, controller.signal);
          if (!Array.isArray(page.items) || !(page.next_cursor === null || typeof page.next_cursor === "string")) {
            throw new Error("Invalid packet page");
          }
        } catch (error) {
          if (!active()) return;
          if (this.failed(error)) {
            this.stop();
            return;
          }
          await this.pause(1000, controller.signal);
          continue;
        }
        if (!active()) return;
        for (const item of page.items) {
          seen.add(item.packet_id);
          if (!this.pending.has(item.packet_id) && (this.touched.get(item.packet_id) ?? 0) <= revision) {
            this.items.set(item.packet_id, item);
          }
        }
        cursor = page.next_cursor;
        this.update([...this.items.values()], cursor !== null);
        if (cursor === null) break;
        await this.pause(200, controller.signal);
      }
      if (!active()) return;
      for (const id of this.items.keys()) {
        if (!seen.has(id) && !this.pending.has(id) && (this.touched.get(id) ?? 0) <= startedRevision) {
          this.items.delete(id);
        }
      }
      // Reconciliation restores the endpoint's source order for the default sort.
      const ordered = [...seen, ...this.items.keys()].filter((id) => this.items.has(id));
      this.items = new Map(ordered.map((id) => [id, this.items.get(id)!]));
      this.update([...this.items.values()], false);
    } finally {
      if (active()) {
        this.controller = undefined;
        this.timer = window.setTimeout(() => void this.run(), 30_000);
      }
    }
  }

  private pause(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve) => {
      const done = (): void => {
        window.clearTimeout(timer);
        signal.removeEventListener("abort", done);
        resolve();
      };
      const timer = window.setTimeout(done, ms);
      signal.addEventListener("abort", done, { once: true });
      if (signal.aborted) done();
    });
  }
}
