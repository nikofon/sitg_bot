import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { I18n } from "../i18n";
import { renderTraining } from "./training";

const controllers: AbortController[] = [];
afterEach(() => controllers.splice(0).forEach((controller) => controller.abort()));
const lobby = { version: 3, version_id: "one", packet_name: "Alpha packet", settings: {
  reversed: true, hidden_after: 2, buzz_timeout: 8, best_buzz: false,
} };
const packets = [
  { id: "one", packet_id: "one", name: "Alpha packet", year: 2023, scope: "training",
    authors: [{ id: "a1", name: "Alice Author" }, { id: "a2", name: "Bob Writer" }],
    tournaments: [{ id: "t1", name: "Spring Cup" }],
    total_theme_count: 12, fresh_theme_count: 4, dissected_theme_count: 10,
    dissected: true, best_buzz_calculated: false },
  { id: "two", packet_id: "two", name: "Beta packet", year: 2024, scope: "tournament",
    authors: [{ id: "a3", name: "Carol Maker" }],
    tournaments: [{ id: "t2", name: "Autumn League" }],
    total_theme_count: 9, fresh_theme_count: 7, dissected_theme_count: null,
    dissected: false, best_buzz_calculated: true },
];

function create(upload = false, locale: "en" | "ru" = "en") {
  const controller = new AbortController(); controllers.push(controller);
  const request = vi.fn(async (path: string) => path.includes("/packets?")
    ? { items: packets.map((packet) => ({ ...packet })), next_cursor: null }
    : { lobby: { ...lobby, version: 4 } });
  const root = renderTraining({ session: null, lobby }, { request } as unknown as ApiClient,
    new I18n(locale), controller.signal, upload);
  return { root, request, controller };
}

const tab = (root: HTMLElement, index: number): void => {
  root.querySelectorAll<HTMLButtonElement>("nav button")[index]!.click();
};

const packetCards = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll<HTMLElement>(".training-packet-card")];

const detailValue = (card: HTMLElement, label: string): string => {
  for (const row of card.querySelectorAll(".lobby-packet-detail")) {
    if (row.querySelector("dt")!.textContent === label) return row.querySelector("dd")!.textContent ?? "";
  }
  throw new Error(`missing detail row: ${label}`);
};

describe("training lobby", () => {
  it("uses the regular lobby sections and restores saved settings", () => {
    const { root } = create();
    expect([...root.querySelectorAll("nav button")].map((button) => button.textContent))
      .toEqual(["Overview", "Packets", "Options"]);
    expect(root.querySelector(".training-overview h2")!.textContent).toBe("Training lobby");
    // Overview: selected packet and enabled modifiers beside it, no save button.
    const overview = root.querySelector<HTMLElement>(".training-overview")!;
    expect(overview.querySelector(".training-selection-packet")!.textContent).toBe("Alpha packet");
    expect([...overview.querySelectorAll(".training-modifier-badge")].map((badge) => badge.textContent))
      .toEqual(["Reverse word order", "Hide question text"]);
    expect(overview.querySelector('button[type="submit"]')).toBeNull();
  });

  it("keeps the save action inside the grouped settings section only", () => {
    const { root } = create();
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    expect(options.querySelector('button[type="submit"]')!.textContent).toBe("Save lobby settings");
    // Selected packet and modifiers are shown beside the settings too.
    expect(options.querySelector(".training-selection-packet")!.textContent).toBe("Alpha packet");
    expect([...options.querySelectorAll(".training-modifier-badge")].map((badge) => badge.textContent))
      .toEqual(["Reverse word order", "Hide question text"]);
    const groups = [...options.querySelectorAll(".descriptor-category")].map((node) => node.textContent);
    expect(groups).toEqual(["Modifiers", "Timing", "Reading"]);
    // Only the picked modifier's enable control is shown; the viewed one is enabled.
    const reversedBox = options.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    expect(reversedBox.checked).toBe(true);
    const numbers = [...options.querySelectorAll<HTMLInputElement>('input[type="number"]')]
      .map((input) => input.value);
    expect(numbers).toEqual(["8", "15", "0.6", "18"]);
    const picker = options.querySelector<HTMLSelectElement>('.training-modifier-picker select')!;
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const hiddenBox = options.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    expect(hiddenBox.checked).toBe(true);
    expect(options.querySelector<HTMLInputElement>('.training-modifier-config input[type="number"]')!.value).toBe("2");
  });

  it("configures modifiers from the dropdown without losing enabled state or values", () => {
    const { root } = create();
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    const picker = options.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    expect(picker.value).toBe("reversed");
    const config = options.querySelector<HTMLElement>(".training-modifier-config")!;
    expect(config.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true);

    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const hiddenBox = config.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(hiddenBox.checked).toBe(true);
    const seconds = config.querySelector<HTMLInputElement>('input[type="number"]')!;
    expect(seconds.value).toBe("2");
    seconds.value = "5";
    seconds.dispatchEvent(new Event("change", { bubbles: true }));

    picker.value = "best_buzz";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const bestBuzz = config.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(bestBuzz.disabled).toBe(true); // incompatible with reversed/hidden

    hiddenBox.checked = false;
    hiddenBox.dispatchEvent(new Event("change", { bubbles: true }));
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    config.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked = false;
    config.querySelector<HTMLInputElement>('input[type="checkbox"]')!.dispatchEvent(new Event("change", { bubbles: true }));
    picker.value = "best_buzz";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const bestBuzz2 = config.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(bestBuzz2.disabled).toBe(false);
    bestBuzz2.checked = true;
    bestBuzz2.dispatchEvent(new Event("change", { bubbles: true }));
    expect([...root.querySelectorAll(".training-overview .training-modifier-badge")].map((b) => b.textContent))
      .toEqual(["Best buzz"]);

    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(config.querySelector<HTMLInputElement>('input[type="checkbox"]')!.disabled).toBe(true);
    expect(config.querySelector<HTMLInputElement>('input[type="number"]')!.value).toBe("5");
  });

  it("renders packet cards with metadata and only contextual actions", async () => {
    const { root } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    const [alpha, beta] = packetCards(root) as [HTMLElement, HTMLElement];
    expect(alpha.querySelector("h3")!.textContent).toBe("Alpha packet");
    expect(detailValue(alpha, "Packet year")).toBe("2023");
    expect(detailValue(alpha, "Authors")).toBe("Alice Author, Bob Writer");
    expect(detailValue(alpha, "Tournaments")).toBe("Spring Cup");
    expect(detailValue(alpha, "Themes")).toBe("12");
    expect(detailValue(alpha, "Fresh themes for you / total")).toBe("4 / 12");
    expect(detailValue(alpha, "In knowledge library")).toBe("Yes");
    expect(detailValue(alpha, "Dissected themes")).toBe("10 / 12");
    expect(detailValue(alpha, "Best buzz calculated")).toBe("No");
    expect(detailValue(beta, "Tournaments")).toBe("Autumn League");
    expect(detailValue(beta, "In knowledge library")).toBe("No");
    expect(detailValue(beta, "Dissected themes")).toBe("—");
    expect(detailValue(beta, "Best buzz calculated")).toBe("Yes");
    expect(alpha.querySelector(".lobby-packet-selected")!.textContent).toBe("Selected");
    expect(beta.querySelector(".lobby-packet-selected")).toBeNull();
    // No library download/view actions in the training lobby.
    for (const card of packetCards(root)) {
      const labels = [...card.querySelectorAll("button")].map((button) => button.textContent);
      expect(labels).not.toContain("Download");
      expect(labels).not.toContain("View");
      expect(labels).toContain("Select packet");
      expect(labels).toContain("Prepare Best buzz");
    }
    expect([...alpha.querySelectorAll("button")].map((b) => b.textContent)).toContain("Delete private packet");
    expect([...beta.querySelectorAll("button")].map((b) => b.textContent)).not.toContain("Delete private packet");
  });

  it("sends search, sorting and year filters to the catalogue from one bubble", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    const filters = root.querySelector<HTMLElement>(".training-packet-filters")!;
    expect(filters.getAttribute("role")).toBe("search");
    expect(filters.querySelectorAll(".lobby-packet-year-range")).toHaveLength(2);
    const [search, author] = [...filters.querySelectorAll<HTMLInputElement>('input[type="search"]')] as [HTMLInputElement, HTMLInputElement];
    const order = filters.querySelector<HTMLSelectElement>("select")!;

    author.value = "carol";
    author.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?author=carol&limit=25", expect.anything()));

    order.value = "name_desc";
    order.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?author=carol&order=name_desc&limit=25", expect.anything()));

    const yearFrom = filters.querySelector<HTMLInputElement>('input[name="year_from"]')!;
    yearFrom.value = "2020";
    yearFrom.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?author=carol&order=name_desc&year_from=2020&limit=25", expect.anything()));

    search.value = "alpha";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?author=carol&order=name_desc&year_from=2020&name=alpha&limit=25", expect.anything()));

    filters.querySelector<HTMLButtonElement>("button")!.click(); // reset
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?limit=25", expect.anything()));
    expect(search.value).toBe("");
    expect(author.value).toBe("");
    expect(yearFrom.value).toBe("");
    expect(order.value).toBe("id_asc");
  });

  it("selects without starting, then explicitly saves the packet and modifiers", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    expect(request).toHaveBeenCalledTimes(1);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(request.mock.calls[1]![0]).toBe("/api/miniapp/training/lobby");
    const options = (request.mock.calls[1] as unknown as [string, { body: unknown }])[1];
    expect(options.body).toMatchObject({ command: "save", expected_version: 3, version_id: "two",
      settings: { reversed: true, hidden_after: 2, buzz_timeout: 8, best_buzz: false } });
    await vi.waitFor(() => expect(root.textContent).toContain("Return to Telegram"));
    expect(request.mock.calls.some(([path]) => path.endsWith("/start"))).toBe(false);
    // Selection summary follows the newly chosen packet everywhere.
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent).toBe("Beta packet");
    expect(root.querySelector(".training-overview .lobby-packet-selected")).toBeNull();
  });

  it("prevents Enter in every packet-filter input from saving the lobby", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    tab(root, 1);
    const inputs = root.querySelectorAll<HTMLInputElement>(".training-packet-filters input");
    expect(inputs).toHaveLength(6);
    for (const input of inputs) {
      const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
      input.dispatchEvent(enter);
      expect(input.form).toBeNull();
      if (!enter.defaultPrevented) input.form?.requestSubmit();
      const letter = new KeyboardEvent("keydown", { key: "a", bubbles: true, cancelable: true });
      input.dispatchEvent(letter);
      expect(letter.defaultPrevented).toBe(false);
    }
    expect(request.mock.calls.some(([path]) => path.endsWith("/lobby"))).toBe(false);
  });

  it("saves valid settings independently of invalid catalogue year bounds", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    tab(root, 1);
    const bounds = root.querySelectorAll<HTMLInputElement>('.training-packet-filters input[type="number"]');
    expect(bounds).toHaveLength(4);
    bounds.forEach((input) => { input.value = "999"; expect(input.checkValidity()).toBe(false); });
    tab(root, 2);
    const form = root.querySelector<HTMLFormElement>(".training-options form")!;
    expect(form.checkValidity()).toBe(true);
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/lobby", expect.objectContaining({ method: "POST" }),
    ));
  });

  it.each(["", "0", "3600"])("validates detached hidden time %j before saving", async (value) => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    tab(root, 2);
    const picker = root.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const seconds = root.querySelector<HTMLInputElement>('.training-modifier-config input[type="number"]')!;
    seconds.value = value;
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(root.contains(seconds)).toBe(false);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(picker.value).toBe("hidden");
    expect(root.contains(seconds)).toBe(true);
    expect(seconds.validity.valid).toBe(false);
    expect(request.mock.calls.some(([path]) => path.endsWith("/lobby"))).toBe(false);
    seconds.value = "5";
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/lobby", expect.objectContaining({ body: expect.objectContaining({
        settings: expect.objectContaining({ hidden_after: 5 }),
      }) })));
  });

  it("disables Save after retiring the selected packet and re-enables it on selection", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    const save = root.querySelector<HTMLButtonElement>('.training-options button[type="submit"]')!;
    expect(save.disabled).toBe(false);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    packetCards(root)[0]!.querySelector<HTMLButtonElement>(".danger-button")!.click();
    await vi.waitFor(() => expect(save.disabled).toBe(true));
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent)
      .toBe("No packet selected");
    save.click();
    expect(request.mock.calls.some(([path]) => path.endsWith("/lobby"))).toBe(false);
    root.querySelector<HTMLButtonElement>('button[data-select="two"]')!.click();
    expect(save.disabled).toBe(false);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/lobby", expect.objectContaining({ body: expect.objectContaining({ version_id: "two" }) })));
  });

  it("shows upload controls separately without loading the packet picker", () => {
    const { root, request } = create(true);
    expect(root.querySelector('input[type="file"]')).not.toBeNull();
    expect(root.querySelector("nav")).toBeNull();
    expect(root.querySelector("fieldset")).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it("does not report success for stale saves", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    request.mockRejectedValueOnce(new ApiError("stale_write", 409, false));
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(root.querySelector('[role="status"]')!.textContent).not.toBe(""));
    expect(root.textContent).not.toContain("Saved. Return");
  });

  it("localizes the redesigned training lobby in Russian", async () => {
    const { root } = create(false, "ru");
    expect(root.querySelector(".training-overview h2")!.textContent).toBe("Лобби тренировки");
    expect([...root.querySelectorAll(".training-overview .training-modifier-badge")].map((b) => b.textContent))
      .toEqual(["Обратный порядок слов", "Скрывать текст вопроса"]);
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    expect([...options.querySelectorAll(".descriptor-category")].map((n) => n.textContent))
      .toEqual(["Модификаторы", "Тайминг", "Чтение"]);
    expect(options.querySelector<HTMLSelectElement>(".training-modifier-picker select option")!.textContent)
      .toBe("Выберите модификатор для настройки");
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    expect(detailValue(packetCards(root)[0]!, "Темы")).toBe("12");
    expect(detailValue(packetCards(root)[0]!, "В библиотеке знаний")).toBe("Да");
  });
});
