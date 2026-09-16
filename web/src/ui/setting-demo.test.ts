import { afterEach, describe, expect, it, vi } from "vitest";
import { I18n } from "../i18n";
import { announcementTokens, createSettingDemo, MESSAGE_FLOW_SETTINGS } from "./setting-demo";

afterEach(() => {
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("announcementTokens", () => {
  it("mirrors the backend chunking rules", () => {
    expect(announcementTokens("a few extraordinarilylong words fit", 10))
      .toEqual(["a few", "extraordinarilylong", "words fit"]);
    expect(announcementTokens("", 5)).toEqual([]);
    expect(announcementTokens("one two three", 7)).toEqual(["one two", "three"]);
    expect(announcementTokens("lengthy tail", 7)).toEqual(["lengthy", "tail"]);
  });
});

describe("MESSAGE_FLOW_SETTINGS", () => {
  it("covers message pacing options only", () => {
    for (const name of ["ready_delay", "message_delay", "question_token_delay", "buzz_timeout"]) {
      expect(MESSAGE_FLOW_SETTINGS.has(name)).toBe(true);
    }
    for (const name of ["theme_count", "question_values", "pausing_allowed", "minus_multiplier"]) {
      expect(MESSAGE_FLOW_SETTINGS.has(name)).toBe(false);
    }
  });
});

describe("createSettingDemo", () => {
  const messages = (root: HTMLElement): string[] => Array.from(
    root.querySelectorAll(".setting-demo-message"),
    (node) => node.textContent ?? "",
  );

  it("plays messages with the configured spacing and marks the focused delay", () => {
    vi.useFakeTimers();
    const demo = createSettingDemo("message_delay", () => ({ message_delay: 2 }), new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    expect(messages(demo.root)).toEqual(["A regular game message"]);
    expect(demo.root.querySelector(".setting-demo-status")?.textContent).toContain("this delay");
    vi.advanceTimersByTime(2_100);
    expect(messages(demo.root)).toEqual(["A regular game message", "The next message"]);
    expect(demo.root.querySelector(".setting-demo-status")?.textContent).toContain("Preview finished");
  });

  it("falls back to default pacing when the value is unreadable", () => {
    vi.useFakeTimers();
    const demo = createSettingDemo("message_delay", () => ({ message_delay: "not-a-number" }), new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    vi.advanceTimersByTime(2_900);
    expect(messages(demo.root)).toHaveLength(1);
    vi.advanceTimersByTime(200);
    expect(messages(demo.root)).toHaveLength(2);
  });

  it("streams the sample question in chunks with the token delay", () => {
    vi.useFakeTimers();
    const settings = { question_token_delay: 1, question_token_target_chars: 10 };
    const demo = createSettingDemo("question_token_delay", () => settings, new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    expect(messages(demo.root)).toEqual(["Question 1 is worth 10 points"]);
    const chunks = announcementTokens(new I18n("en").t("setting_demo.question_text"), 10);
    for (let index = 0; index < chunks.length; index += 1) {
      vi.advanceTimersByTime(1_100);
      expect(messages(demo.root)).toHaveLength(2 + index);
      expect(messages(demo.root)[1 + index]).toBe(chunks[index]);
    }
  });

  it("runs buzz countdowns and finishes with the timeout message", () => {
    vi.useFakeTimers();
    const demo = createSettingDemo("buzz_timeout", () => ({}), new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    expect(demo.root.querySelector(".setting-demo-status")?.textContent).toContain("Time to buzz");
    vi.advanceTimersByTime(10_100);
    expect(messages(demo.root)).toEqual(["Buzz window opens", "Nobody buzzed in time — the question passes"]);
    expect(demo.root.querySelector(".setting-demo-status")?.textContent).toContain("Preview finished");
  });

  it("shortens extreme waits and notes the truncation", () => {
    vi.useFakeTimers();
    const demo = createSettingDemo("ready_delay", () => ({ ready_delay: 100 }), new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    expect(messages(demo.root)).toHaveLength(0);
    vi.advanceTimersByTime(6_000);
    expect(demo.root.querySelector(".setting-demo-status")?.textContent).toContain("shortened for the preview");
    vi.advanceTimersByTime(6_100);
    expect(messages(demo.root)).toEqual(["All players are ready — the game starts now."]);
  });

  it("stops on demand, restarts from scratch, and halts when detached", () => {
    vi.useFakeTimers();
    const demo = createSettingDemo("message_delay", () => ({ message_delay: 1 }), new I18n("en"));
    document.body.append(demo.root);
    demo.restart();
    vi.advanceTimersByTime(1_100);
    expect(messages(demo.root)).toHaveLength(2);
    demo.restart();
    expect(messages(demo.root)).toHaveLength(1);
    demo.stop();
    vi.advanceTimersByTime(5_000);
    expect(messages(demo.root)).toHaveLength(1);
    demo.root.remove();
    demo.restart();
    vi.advanceTimersByTime(5_000);
    expect(messages(demo.root)).toHaveLength(1);
  });
});
