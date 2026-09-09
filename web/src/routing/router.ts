import { matchRoute, type RouteMatch } from "./routes";

export type RouteListener = (route: RouteMatch) => void;

export class Router {
  private readonly listeners = new Set<RouteListener>();
  private readonly onPopState = (): void => this.publish();

  start(): void {
    window.addEventListener("popstate", this.onPopState);
    this.publish();
  }

  stop(): void {
    window.removeEventListener("popstate", this.onPopState);
  }

  subscribe(listener: RouteListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  current(): RouteMatch {
    return matchRoute(window.location);
  }

  navigate(path: string, options: { replace?: boolean } = {}): void {
    const target = new URL(path, window.location.origin);
    if (target.origin !== window.location.origin) throw new TypeError("Route must be same-origin");
    const method = options.replace ? "replaceState" : "pushState";
    window.history[method]({}, "", `${target.pathname}${target.search}${target.hash}`);
    this.publish();
  }

  back(): void {
    if (window.history.length > 1) window.history.back();
    else this.navigate("/tournaments", { replace: true });
  }

  private publish(): void {
    const route = this.current();
    for (const listener of this.listeners) listener(route);
  }
}
