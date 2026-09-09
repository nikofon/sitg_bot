import { describe, expect, it } from "vitest";

import { FilterStore } from "./filter-store";

describe("FilterStore", () => {
  it("persists route filters and lets URL values win", () => {
    const store = new FilterStore(sessionStorage, "test:");
    store.write("tournaments", { status: "ongoing", relationship: "registered" });

    const merged = store.mergeWithQuery(
      "tournaments",
      new URLSearchParams("relationship=managed"),
    );

    expect(merged.get("status")).toBe("ongoing");
    expect(merged.get("relationship")).toBe("managed");
  });
});
