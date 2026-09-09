import { describe, expect, it } from "vitest";

import { en } from "./en";
import { ru } from "./ru";

describe("Mini App catalogs", () => {
  it("contain the same canonical keys", () => {
    expect(Object.keys(ru).sort()).toEqual(Object.keys(en).sort());
  });

  it("contain no empty messages", () => {
    expect(Object.values(en).every((message) => message.trim().length > 0)).toBe(true);
    expect(Object.values(ru).every((message) => message.trim().length > 0)).toBe(true);
  });
});
