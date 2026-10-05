import { describe, expect, it, vi } from "vitest";
import type { LibraryPacket, LibraryPage, LibraryQuestionStatistics } from "../api/types";
import { I18n } from "../i18n";
import { renderLibrary, renderLibraryReader } from "./library";

const packet: LibraryPacket = {
  packet_id: "packet", version_id: "version", name: "Example <packet>", year: 2020,
  published_at: "2024-01-01", lead_author: "Anna", authors: ["Anna", "Boris"],
  fresh_play_unit_count: 3, total_play_unit_count: 5,
  tournaments: [{ id: "cup", name: "Autumn Cup", slug: "autumn-2026", role: "player" }],
};

describe("library", () => {
  it("sorts by fresh themes ascending by default and shows fresh counts on cards", () => {
    const read: LibraryPacket = { ...packet, version_id: "read", name: "Read packet", fresh_play_unit_count: 0 };
    const fresh: LibraryPacket = { ...packet, version_id: "fresh", name: "Fresh packet", fresh_play_unit_count: 8 };
    const root = renderLibrary([fresh, packet, read], new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    const ids = (): string[] => Array.from(root.querySelectorAll("article"), (card) => card.getAttribute("data-packet-id") ?? "");
    expect(ids()).toEqual(["read", "version", "fresh"]);
    expect(root.querySelector("article")!.textContent).toContain("0 / 5");

    const sort = root.querySelector<HTMLSelectElement>("select[aria-label='Sort packets']");
    expect(sort).not.toBeNull();
    expect(sort!.value).toBe("fresh");
    sort!.value = "default";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["fresh", "version", "read"]);
  });

  it("filters by tournament name/slug, authors and inclusive years, and resets", () => {
    const save = vi.fn();
    const root = renderLibrary([packet], new I18n("en"), {}, save, vi.fn(), vi.fn());
    const input = (name: string, value: string): void => {
      const field = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
      field.value = value;
      field.dispatchEvent(new Event("input"));
    };
    for (const term of ["AUTUMN CUP", "autumn-2026", "example"]) {
      input("search", term);
      expect(root.querySelectorAll("article")).toHaveLength(1);
    }
    input("author", "boris");
    input("year_from", "2020");
    input("year_to", "2020");
    input("publication_from", "2024");
    input("publication_to", "2024");
    expect(root.querySelectorAll("article")).toHaveLength(1);
    input("publication_to", "2023");
    expect(root.querySelectorAll("article")).toHaveLength(0);
    root.querySelector<HTMLButtonElement>('[role="search"] button')!.click();
    expect(root.querySelectorAll("article")).toHaveLength(1);
    expect(save).toHaveBeenLastCalledWith({});
    expect(root.querySelector("packet")).toBeNull();
  });

  it("provides view/download actions and a tournament profile link", () => {
    const access = vi.fn();
    const profile = vi.fn();
    const root = renderLibrary([packet], new I18n("en"), {}, vi.fn(), access, profile);
    const buttons = root.querySelectorAll<HTMLButtonElement>("article button");
    buttons[0]!.click();
    buttons[1]!.click();
    expect(access.mock.calls.map((call) => call[1])).toEqual(["view", "download"]);
    const link = root.querySelector<HTMLAnchorElement>("article a")!;
    expect(link.getAttribute("href")).toBe("/tournaments?role=player&info=cup");
    link.click();
    expect(profile).toHaveBeenCalledWith(packet.tournaments[0]);
  });

  it("renders one theme at a time with dropdown and page-number navigation", () => {
    const question = { value: 10, text: "Question <script>", answer: "Answer", accepted_answers: ["Alternative"], commentary: "Explanation", author: "Writer", source: "Book", form: "Name" };
    const pages: LibraryPage[] = [
      { title: "First", author: "Theme Writer", questions: [question] },
      { title: "Second", author: "", questions: [{ ...question, text: "Second question" }] },
    ];
    const root = renderLibraryReader("Packet", pages, new I18n("en"));
    expect(root.querySelector(".library-page")!.textContent).toContain("Theme: First");
    for (const value of ["Theme Writer", "10) [Form: Name]\nQuestion <script>", "Answer", "Alternative", "Explanation", "Writer", "Book", "Name"]) {
      expect(root.querySelector(".library-page")!.textContent).toContain(value);
    }
    expect(root.querySelector("script")).toBeNull();
    expect(root.querySelector(".library-page")!.textContent).not.toContain("Second question");
    const select = root.querySelector("select")!;
    expect(select.className).toBe("library-theme-select");
    select.value = "1";
    select.dispatchEvent(new Event("change"));
    expect(root.querySelector(".library-page")!.textContent).toContain("Theme: Second");
    expect(root.querySelector('[aria-current="page"]')!.textContent).toBe("2");
    root.querySelector<HTMLButtonElement>("nav button")!.click();
    expect(select.value).toBe("0");
  });

  it("links authors to profiles, shows theme commentary and question statistics", () => {
    const authorId = "11111111-1111-1111-1111-111111111111";
    const playedId = "22222222-2222-2222-2222-222222222222";
    const unplayedId = "33333333-3333-3333-3333-333333333333";
    const question = { value: 10, text: "Question", answer: "Answer", accepted_answers: [], commentary: "", author: "Writer", source: "", form: "" };
    const pages: LibraryPage[] = [
      { title: "First", author: "Theme Writer", commentary: "Theme commentary", author_id: authorId, questions: [{ ...question, id: playedId, author_id: authorId }] },
      { title: "Second", author: "", questions: [{ ...question, text: "Second question", id: unplayedId, author_id: null }] },
    ];
    const statistics: Record<string, LibraryQuestionStatistics> = {
      [playedId]: { views: 8, buzzes: 2, attempts: 2, correct: 1, incorrect: 1, correct_rate: 50, incorrect_rate: 50 },
    };
    const navigate = vi.fn();
    const root = renderLibraryReader("Packet", pages, new I18n("en"), statistics, navigate);
    expect(root.querySelector(".library-page")!.textContent).toContain("Theme commentary");
    const links = root.querySelectorAll<HTMLAnchorElement>(".library-page a");
    expect(links).toHaveLength(2);
    for (const link of links) expect(link.getAttribute("href")).toBe(`/authors/${authorId}`);
    links[0]!.click();
    expect(navigate).toHaveBeenCalledWith(`/authors/${authorId}`);
    const stats = root.querySelector(".library-question-statistics")!;
    expect(stats.textContent).toContain("Views 8");
    expect(stats.textContent).toContain("Buzzes 2");
    expect(stats.textContent).toContain("Correct 50%");
    expect(stats.textContent).toContain("Incorrect 50%");
    const select = root.querySelector("select")!;
    select.value = "1";
    select.dispatchEvent(new Event("change"));
    expect(root.querySelector(".library-page")!.textContent).toContain("has not been played");
    expect(root.querySelector(".library-page a")).toBeNull();
  });

  it("marks the current pagination button so it stands out from its siblings", () => {
    const question = { value: 10, text: "Question", answer: "Answer", accepted_answers: [], commentary: "", author: "", source: "", form: "" };
    const pages: LibraryPage[] = [
      { title: "First", author: "", questions: [question] },
      { title: "Second", author: "", questions: [question] },
    ];
    const root = renderLibraryReader("Packet", pages, new I18n("en"));
    const buttons = root.querySelectorAll<HTMLButtonElement>("nav button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0]!.className).toBe("pagination-page");
    expect(buttons[0]!.getAttribute("aria-current")).toBe("page");
    expect(buttons[1]!.className).toBe("pagination-page");
    expect(buttons[1]!.getAttribute("aria-current")).toBeNull();
    buttons[1]!.click();
    const refreshed = root.querySelectorAll<HTMLButtonElement>("nav button");
    expect(refreshed[1]!.getAttribute("aria-current")).toBe("page");
    expect(refreshed[0]!.getAttribute("aria-current")).toBeNull();
  });
});
