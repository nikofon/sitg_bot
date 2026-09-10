import { describe, expect, it } from "vitest";

import { matchRoute } from "./routes";

describe("route matching", () => {
  it.each([
    ["/tournaments", "tournaments"],
    ["/ongoing", "ongoing"],
    ["/history", "history"],
    ["/library", "library"],
    ["/library/version-id", "library_reader"],
    ["/authors/link", "authors_link"],
    ["/manager/appeals", "manager_appeals"],
  ])("matches %s", (pathname, expected) => {
    expect(matchRoute({ pathname, search: "" }).id).toBe(expected);
  });

  it("extracts opaque launch references without granting meaning to them", () => {
    const route = matchRoute({ pathname: "/lobbies/opaque%2Dreference", search: "?view=players" });
    expect(route.id).toBe("lobby");
    expect(route.params.launch_ref).toBe("opaque-reference");
    expect(route.query.get("view")).toBe("players");
  });

  it("matches tournament management with an opaque launch reference", () => {
    const route = matchRoute({
      pathname: "/manager/tournaments/opaque-reference/management",
      search: "",
    });

    expect(route.id).toBe("manager_management");
    expect(route.params.launch_ref).toBe("opaque-reference");
  });

  it("matches the packet editor with an opaque launch reference", () => {
    const route = matchRoute({
      pathname: "/manager/packets/opaque-reference/edit",
      search: "",
    });

    expect(route.id).toBe("packet_editor");
    expect(route.params.launch_ref).toBe("opaque-reference");
  });

  it("does not accept extra route segments", () => {
    expect(matchRoute({ pathname: "/reports/ref/admin", search: "" }).id).toBe("not_found");
  });
});
