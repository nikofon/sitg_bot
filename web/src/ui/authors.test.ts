import { describe, expect, it, vi } from "vitest";
import type { AuthorsResource, AuthorStatistics } from "../api/types";
import { I18n } from "../i18n";
import { matchRoute } from "../routing/routes";
import { renderAuthorProfile, renderAuthors } from "./authors";

const id = "11111111-1111-1111-1111-111111111111";
const catalogue: AuthorsResource = {
  kind: "authors", state: "ready", items: [
    { id, display_name: "Ada <b>Author</b>", tournament_count: 2, question_count: 20 },
    { id: "22222222-2222-2222-2222-222222222222", display_name: "Bob", tournament_count: 4, question_count: 10 },
  ],
};
const statistics: AuthorStatistics = {
  presentations: 4, solved: 1, exposures: 8, buzzes: 2, attempts: 2, correct: 1, timeouts: 1,
  buzz_rate: 25, accuracy: 50, solved_rate: 25,
};

describe("player author views", () => {
  it("supports name search, numeric sorting, saved filters and profile navigation", () => {
    const save = vi.fn(), navigate = vi.fn();
    const node = renderAuthors(catalogue, new I18n("en"), {}, save, navigate);
    expect(node.querySelector("b")).toBeNull();
    expect(node.querySelectorAll("article")).toHaveLength(2);
    const sort = node.querySelector("select")!;
    sort.value = "question_count:asc";
    sort.dispatchEvent(new Event("change"));
    expect(node.querySelector("article")?.textContent).toContain("Bob");
    const search = node.querySelector("input")!;
    search.value = "ADA";
    search.dispatchEvent(new Event("input"));
    expect(node.querySelectorAll("article")).toHaveLength(1);
    expect(save).toHaveBeenLastCalledWith({ search: "ADA", order: "question_count:asc" });
    node.querySelector("a")!.click();
    expect(navigate).toHaveBeenCalledWith(`/authors/${id}`);
    search.value = "nobody";
    search.dispatchEvent(new Event("input"));
    expect(node.textContent).toContain("No authors found.");
  });

  it("renders aggregates, per-value accuracy and honest empty samples", () => {
    const navigate = vi.fn();
    const node = renderAuthorProfile({
      kind: "author_profile", state: "ready", author: catalogue.items[0]!, statistics,
      by_value: [{ ...statistics, value: 10 }, { ...statistics, value: 20, attempts: 0, accuracy: null }],
    }, new I18n("en"), navigate);
    expect(node.textContent).toContain("50%");
    expect(node.textContent).toContain("25%");
    expect(node.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(node.querySelectorAll("tbody tr")[1]?.textContent).toContain("—");
    expect(node.querySelector("b")).toBeNull();
    node.querySelector("button")!.click();
    expect(navigate).toHaveBeenCalledWith("/authors");
    const empty = renderAuthorProfile({
      kind: "author_profile", state: "ready", author: catalogue.items[0]!,
      statistics: { ...statistics, presentations: 0 }, by_value: [],
    }, new I18n("en"), navigate);
    expect(empty.textContent).toContain("No finalized question statistics yet.");
    expect(empty.querySelector("table")).toBeNull();
  });

  it("resolves the catalogue, profile and existing link form independently", () => {
    expect(matchRoute({ pathname: "/authors", search: "" }).id).toBe("authors");
    expect(matchRoute({ pathname: `/authors/${id}`, search: "" }).params.author_id).toBe(id);
    expect(matchRoute({ pathname: "/authors/link", search: "" }).id).toBe("authors_link");
  });
});
