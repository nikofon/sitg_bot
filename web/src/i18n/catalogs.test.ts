import { describe, expect, it } from "vitest";

import { en } from "./en";
import { I18n } from "./index";
import { ru } from "./ru";

describe("Mini App catalogs", () => {
  it("contain the same canonical keys", () => {
    expect(Object.keys(ru).sort()).toEqual(Object.keys(en).sort());
  });

  it("contain no empty messages", () => {
    expect(Object.values(en).every((message) => message.trim().length > 0)).toBe(true);
    expect(Object.values(ru).every((message) => message.trim().length > 0)).toBe(true);
  });

  it.each(["en", "ru"] as const)("formats lobby failure counts in %s", (locale) => {
    const text = new I18n(locale).t("lobby.readiness.insufficient_fresh_content", {
      available: 1, required: 3,
    });
    expect(text).toContain(locale === "en" ? "1 available, 3 required" : "доступно 1, требуется 3");
    expect(text).not.toContain("{");
  });
});
