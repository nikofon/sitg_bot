export class FilterStore {
  constructor(
    private readonly storage: Storage = window.sessionStorage,
    private readonly prefix = "sitg:miniapp:filters:",
  ) {}

  read(routeId: string): Record<string, string> {
    try {
      const raw = this.storage.getItem(`${this.prefix}${routeId}`);
      if (!raw) return {};
      const value: unknown = JSON.parse(raw);
      if (!value || typeof value !== "object" || Array.isArray(value)) return {};
      return Object.fromEntries(
        Object.entries(value).filter(
          (entry): entry is [string, string] =>
            typeof entry[0] === "string" && typeof entry[1] === "string",
        ),
      );
    } catch {
      return {};
    }
  }

  write(routeId: string, filters: Record<string, string>): void {
    try {
      this.storage.setItem(`${this.prefix}${routeId}`, JSON.stringify(filters));
    } catch {
      // Storage may be unavailable in privacy modes; URL state remains authoritative.
    }
  }

  mergeWithQuery(routeId: string, query: URLSearchParams): URLSearchParams {
    const merged = new URLSearchParams();
    for (const [key, value] of Object.entries(this.read(routeId))) merged.set(key, value);
    for (const [key, value] of query) merged.set(key, value);
    return merged;
  }
}
