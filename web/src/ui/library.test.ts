import { describe, expect, it, vi } from "vitest";
import type { LibraryPacket, LibraryPage } from "../api/types";
import { I18n } from "../i18n";
import { renderLibrary, renderLibraryReader } from "./library";

const packet: LibraryPacket = {
  packet_id: "packet", version_id: "version", name: "Example <packet>", year: 2020,
  published_at: "2024-01-01", lead_author: "Anna", authors: ["Anna", "Boris"],
  tournaments: [{ id: "cup", name: "Autumn Cup", slug: "autumn-2026", role: "player" }],
};

describe("library", () => {
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
    select.value = "1";
    select.dispatchEvent(new Event("change"));
    expect(root.querySelector(".library-page")!.textContent).toContain("Theme: Second");
    expect(root.querySelector('[aria-current="page"]')!.textContent).toBe("2");
    root.querySelector<HTMLButtonElement>("nav button")!.click();
    expect(select.value).toBe("0");
  });
});
