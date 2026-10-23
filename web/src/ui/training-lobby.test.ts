import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { I18n } from "../i18n";
import { renderTraining, type TrainingFeedback } from "./training";

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

type LobbyHandler = (body: Record<string, unknown>) => Promise<unknown> | unknown;

interface LobbyFixture {
  version: number;
  version_id: string | null;
  packet_name: string | null;
  settings: Record<string, unknown>;
}

interface CreateOptions {
  upload?: boolean;
  locale?: "en" | "ru";
  initial?: LobbyFixture;
  current?: unknown;
  /** Handlers for POST /api/miniapp/training/lobby, consumed in order. */
  lobbyCalls?: LobbyHandler[];
}

function create(options: CreateOptions = {}) {
  const controller = new AbortController(); controllers.push(controller);
  const feedback: TrainingFeedback = { notifySuccess: vi.fn(), notifyError: vi.fn() };
  const initial: LobbyFixture = options.initial ?? lobby;
  const calls: LobbyHandler[] = [...(options.lobbyCalls ?? [])];
  // Stateful default: select_packet keeps settings, save_settings keeps the packet.
  let server: LobbyFixture = { ...initial };
  const defaultLobby = (body: Record<string, unknown>): unknown => {
    server = body.version_id
      ? { ...server, version: server.version + 1, version_id: body.version_id as string, packet_name: "Beta packet" }
      : { ...server, version: server.version + 1 };
    if (body.settings) server = { ...server, settings: body.settings as Record<string, unknown> };
    return { lobby: { ...server } };
  };
  const request = vi.fn(async (path: string, init: { body?: unknown } = {}) => {
    if (path.includes("/packets?")) return { items: packets.map((packet) => ({ ...packet })), next_cursor: null };
    if (path === "/api/miniapp/training/lobby") {
      const handler = calls.shift() ?? defaultLobby;
      return handler(init.body as Record<string, unknown>);
    }
    if (path === "/api/miniapp/training/current") return options.current ?? {};
    return {};
  });
  const root = renderTraining({ session: null, lobby: initial }, { request } as unknown as ApiClient,
    new I18n(options.locale ?? "en"), controller.signal, options.upload ?? false, feedback);
  return { root, request, controller, feedback, calls };
}

const tab = (root: HTMLElement, index: number): void => {
  root.querySelectorAll<HTMLButtonElement>("nav button")[index]!.click();
};

const packetCards = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll<HTMLElement>(".training-packet-card")];

const lobbyBodies = (request: ReturnType<typeof create>["request"]): Array<Record<string, unknown>> =>
  request.mock.calls
    .filter(([path]) => path === "/api/miniapp/training/lobby")
    .map((call) => (call[1] as { body: Record<string, unknown> }).body);

const status = (root: HTMLElement): string => root.querySelector('[role="status"]')!.textContent ?? "";

const settingInput = (root: HTMLElement, label: string): HTMLInputElement => {
  for (const node of root.querySelectorAll<HTMLLabelElement>(".settings-form label")) {
    if (node.textContent?.startsWith(label)) {
      return node.querySelector("input")!;
    }
  }
  throw new Error(`missing setting input: ${label}`);
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
    expect(options.querySelector(".training-selection-packet")!.textContent).toBe("Alpha packet");
    // Best buzz is its own section, separate from the other modifiers.
    const groups = [...options.querySelectorAll(".descriptor-category")].map((node) => node.textContent);
    expect(groups).toEqual(["Modifiers", "Best buzz", "Timing", "Reading"]);
    const bestBuzzGroup = options.querySelector<HTMLElement>(".training-best-buzz-group")!;
    expect(bestBuzzGroup.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
    // The picker configures only the non-best-buzz modifiers.
    const picker = options.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    expect([...picker.options].map((option) => option.value)).toEqual(["", "reversed", "hidden"]);
    const reversedBox = options.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    expect(reversedBox.checked).toBe(true);
    const numbers = [...options.querySelectorAll<HTMLInputElement>('input[type="number"]')]
      .map((input) => input.value);
    expect(numbers).toEqual(["8", "15", "0.6", "18"]);
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const hiddenBox = options.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    expect(hiddenBox.checked).toBe(true);
    expect(options.querySelector<HTMLInputElement>('.training-modifier-config input[type="number"]')!.value).toBe("2");
  });

  it("describes every modifier and groups Best buzz separately", () => {
    const { root } = create();
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    // Reversed (the currently viewed modifier) carries its description.
    expect(options.querySelector(".training-modifier-description")!.textContent)
      .toBe("The question is shown with its words in reverse order.");
    const picker = options.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(options.querySelector(".training-modifier-description")!.textContent)
      .toBe("The question appears all at once and its text disappears after the visible time; buzz before it vanishes.");
    const bestBuzzGroup = options.querySelector<HTMLElement>(".training-best-buzz-group")!;
    expect(bestBuzzGroup.querySelector(".training-modifier-description")!.textContent)
      .toBe("Reading pauses at the estimated best buzz point of the question; press Continue reading to reveal the rest.");
    expect(bestBuzzGroup.querySelector(".training-modifier-note")!.textContent)
      .toContain("Not combined with Reverse word order or Hide question text");
  });

  it("greys out the reading interval while Hidden overrides it, without wiping the stored value", () => {
    const { root } = create();
    tab(root, 2);
    const picker = root.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    const tokenDelay = settingInput(root, "Reading interval");
    const note = root.querySelector<HTMLElement>(".training-setting-override")!;
    // Saved lobby has hidden_after: 2, so the override is active on load.
    expect(tokenDelay.disabled).toBe(true);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe("Set to 0 seconds by the Hide question text modifier.");
    expect(tokenDelay.value).toBe("0.6"); // stored preference retained, never zeroed
    expect(settingInput(root, "Characters per reading chunk").disabled).toBe(false);

    const hiddenBox = root.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    hiddenBox.checked = false;
    hiddenBox.dispatchEvent(new Event("change", { bubbles: true }));
    expect(tokenDelay.disabled).toBe(false);
    expect(note.hidden).toBe(true);
    expect(tokenDelay.value).toBe("0.6"); // toggling the modifier off restores the stored value

    hiddenBox.checked = true;
    hiddenBox.dispatchEvent(new Event("change", { bubbles: true }));
    expect(tokenDelay.disabled).toBe(true);
    expect(note.textContent).toBe("Set to 0 seconds by the Hide question text modifier.");
    expect(tokenDelay.value).toBe("0.6");
  });

  it("requires explicit selection to re-enable displaced modifiers", () => {
    const { root } = create();
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    const picker = options.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    // Saved lobby starts with reversed enabled and best buzz off.
    const bestBuzzBox = options.querySelector<HTMLInputElement>(".training-best-buzz-group input[type=\"checkbox\"]")!;
    bestBuzzBox.checked = true;
    bestBuzzBox.dispatchEvent(new Event("change", { bubbles: true }));
    expect(root.querySelector<HTMLInputElement>(".training-modifier-config input[type=\"checkbox\"]")!.checked)
      .toBe(false); // reversed displaced by best buzz
    // Both incompatible modifiers were displaced, each with an explanation.
    expect(status(root)).toBe("Hide question text was turned off: it is incompatible with Best buzz.");
    expect([...root.querySelectorAll(".training-overview .training-modifier-badge")].map((b) => b.textContent))
      .toEqual(["Best buzz"]);

    bestBuzzBox.checked = false;
    bestBuzzBox.dispatchEvent(new Event("change", { bubbles: true }));
    const reversedBox = root.querySelector<HTMLInputElement>(".training-modifier-config input[type=\"checkbox\"]")!;
    expect(reversedBox.checked).toBe(false);
    expect(root.querySelectorAll(".training-overview .training-modifier-badge")).toHaveLength(0);
    reversedBox.checked = true;
    reversedBox.dispatchEvent(new Event("change", { bubbles: true }));
    expect([...root.querySelectorAll(".training-overview .training-modifier-badge")].map((b) => b.textContent))
      .toEqual(["Reverse word order"]);
    expect(settingInput(root, "Reading interval").value).toBe("0.6");
  });

  it("autosaves packet selection immediately without carrying settings", async () => {
    const { root, request, feedback } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ command: "select_packet", expected_version: 3, version_id: "two" });
    expect(lobbyBodies(request)[0]).not.toHaveProperty("settings");
    await vi.waitFor(() => expect(status(root)).toBe("Packet selection saved."));
    expect(feedback.notifySuccess).toHaveBeenCalledTimes(1);
    expect(feedback.notifyError).not.toHaveBeenCalled();
    // Selection summary follows the newly chosen packet everywhere.
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent).toBe("Beta packet");
    expect(root.querySelector(".training-overview .lobby-packet-selected")).toBeNull();
    expect(packetCards(root)[1]!.querySelector(".lobby-packet-selected")!.textContent).toBe("Selected");
    expect(packetCards(root)[0]!.querySelector(".lobby-packet-selected")).toBeNull();
  });

  it("preserves unsaved settings drafts across the packet autosave", async () => {
    const { root, request, feedback } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    tab(root, 2);
    const buzz = settingInput(root, "Buzz timer");
    buzz.value = "9";
    buzz.dispatchEvent(new Event("change", { bubbles: true }));
    expect(status(root)).toBe("You have unsaved changes. Save before returning to Telegram.");
    tab(root, 1);
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).not.toHaveProperty("settings");
    await vi.waitFor(() => expect(status(root)).toBe("You have unsaved changes. Save before returning to Telegram."));
    // The draft survives the authoritative lobby replacement.
    expect(settingInput(root, "Buzz timer").value).toBe("9");
    expect(feedback.notifySuccess).toHaveBeenCalledTimes(1);
    // Explicit save persists the draft against the post-selection version.
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(2));
    expect(lobbyBodies(request)[1]).toMatchObject({ command: "save_settings", expected_version: 4 });
    expect(lobbyBodies(request)[1]).not.toHaveProperty("version_id");
    expect(lobbyBodies(request)[1]).toMatchObject({ settings: { buzz_timeout: 9, reversed: true, hidden_after: 2 } });
    await vi.waitFor(() => expect(status(root)).toContain("Saved. Return to Telegram"));
    expect(settingInput(root, "Buzz timer").value).toBe("9");
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent).toBe("Beta packet");
  });

  it("keeps edits made during a settings save unsaved until explicitly saved", async () => {
    let resolveSave!: (value: unknown) => void;
    const { root, request } = create({ lobbyCalls: [
      () => new Promise((resolve) => { resolveSave = resolve; }),
    ] });
    tab(root, 2);
    const buzz = settingInput(root, "Buzz timer");
    buzz.value = "10";
    buzz.dispatchEvent(new Event("change", { bubbles: true }));
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request)).toHaveLength(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ settings: { buzz_timeout: 10 } });
    buzz.value = "20";
    buzz.dispatchEvent(new Event("input", { bubbles: true }));
    resolveSave({ lobby: { ...lobby, version: 4, settings: { ...lobby.settings, buzz_timeout: 10 } } });
    await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false));
    expect(status(root)).toBe("You have unsaved changes. Save before returning to Telegram.");
    expect(buzz.value).toBe("20");
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request)).toHaveLength(2));
    expect(lobbyBodies(request)[1]).toMatchObject({ expected_version: 4, settings: { buzz_timeout: 20 } });
    await vi.waitFor(() => expect(status(root)).toContain("Saved. Return to Telegram"));
  });

  it("snapshots settings at submit while waiting for packet autosave", async () => {
    let resolveSelect!: (value: unknown) => void;
    const { root, request } = create({ lobbyCalls: [
      () => new Promise((resolve) => { resolveSelect = resolve; }),
    ] });
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    const buzz = settingInput(root, "Buzz timer");
    buzz.value = "10";
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    buzz.value = "20";
    buzz.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(lobbyBodies(request)).toHaveLength(1));
    resolveSelect({ lobby: { ...lobby, version: 4, version_id: "two", packet_name: "Beta packet" } });
    await vi.waitFor(() => expect(lobbyBodies(request)).toHaveLength(2));
    expect(lobbyBodies(request)[1]).toMatchObject({ expected_version: 4, settings: { buzz_timeout: 10 } });
    await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false));
    expect(status(root)).toBe("You have unsaved changes. Save before returning to Telegram.");
    expect(buzz.value).toBe("20");
  });

  it("saves settings without a packet selected", async () => {
    const { root, request } = create({ initial: { version: 2, version_id: null, packet_name: null, settings: {} } });
    const save = root.querySelector<HTMLButtonElement>('.training-options button[type="submit"]')!;
    expect(save.disabled).toBe(false);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ command: "save_settings", expected_version: 2 });
    expect(lobbyBodies(request)[0]).not.toHaveProperty("version_id");
  });

  it("serializes an in-flight selection before a settings save and uses the newest version", async () => {
    let resolveSelect: ((value: unknown) => void) | undefined;
    const { root, request } = create({ lobbyCalls: [
      () => new Promise((resolve) => { resolveSelect = resolve; }),
      (body) => ({ lobby: { ...lobby, version: 5, version_id: body.version_id as string, settings: body.settings } }),
    ] });
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    expect(status(root)).toBe("Saving packet selection…");
    // While the selection is in flight the Save submit is queued, not sent.
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ command: "select_packet" });
    resolveSelect!({ lobby: { ...lobby, version: 4, version_id: "two", packet_name: "Beta packet", settings: lobby.settings } });
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(2));
    expect(lobbyBodies(request)[1]).toMatchObject({ command: "save_settings", expected_version: 4 });
  });

  it("reverts a rejected selection and reports the failure honestly", async () => {
    const { root, request, feedback } = create({ lobbyCalls: [
      () => { throw new ApiError("forbidden", 403, false); },
    ] });
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    await vi.waitFor(() => expect(status(root)).not.toBe("Saving packet selection…"));
    expect(status(root)).not.toBe("Packet selection saved.");
    expect(feedback.notifyError).toHaveBeenCalledTimes(1);
    expect(feedback.notifySuccess).not.toHaveBeenCalled();
    // The authoritative selection is untouched.
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent).toBe("Alpha packet");
    expect(packetCards(root)[0]!.querySelector(".lobby-packet-selected")).not.toBeNull();
    expect(packetCards(root)[1]!.querySelector(".lobby-packet-selected")).toBeNull();
    expect(lobbyBodies(request).length).toBe(1);
  });

  it("reloads the authoritative lobby on a stale selection without retrying silently", async () => {
    const server = { ...lobby, version: 9, version_id: "one", packet_name: "Alpha packet" };
    const { root, request, feedback } = create({
      lobbyCalls: [() => { throw new ApiError("stale_write", 409, false); }],
      current: { lobby: server },
    });
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    packetCards(root)[1]!.querySelector<HTMLButtonElement>("button[data-select]")!.click();
    await vi.waitFor(() => expect(status(root)).toBe("This item changed. The latest version has been loaded."));
    expect(feedback.notifyError).toHaveBeenCalledTimes(1);
    expect(feedback.notifySuccess).not.toHaveBeenCalled();
    // The server state was adopted, the selection reverted, no silent retry.
    expect(request.mock.calls.some(([path]) => path === "/api/miniapp/training/current")).toBe(true);
    expect(lobbyBodies(request).length).toBe(1);
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent).toBe("Alpha packet");
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
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ command: "save_settings" });
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
    expect(lobbyBodies(request).length).toBe(0);
    seconds.value = "5";
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ settings: expect.objectContaining({ hidden_after: 5 }) });
  });

  it("does not report success for stale saves", async () => {
    const { root, feedback } = create({ lobbyCalls: [
      () => { throw new ApiError("stale_write", 409, false); },
    ] });
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status(root)).not.toBe(""));
    expect(status(root)).not.toContain("Saved. Return");
    expect(feedback.notifyError).toHaveBeenCalledTimes(1);
    expect(feedback.notifySuccess).not.toHaveBeenCalled();
  });

  it("signals a haptic error for a rejected settings save", async () => {
    const { root, feedback } = create({ lobbyCalls: [
      () => { throw new ApiError("validation_failed", 422, false); },
    ] });
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(status(root)).not.toBe(""));
    expect(feedback.notifyError).toHaveBeenCalledTimes(1);
    expect(feedback.notifySuccess).not.toHaveBeenCalled();
    expect(status(root)).not.toContain("Saved. Return");
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
    expect(beta.querySelector("h3")!.textContent).toBe("Beta packet");
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

  it("filters the catalogue to packets uploaded by the player themself", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    const filters = root.querySelector<HTMLElement>(".training-packet-filters")!;
    const mine = filters.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(mine.closest("label")!.textContent).toBe("My uploads only");

    mine.checked = true;
    mine.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?owner_uploads_only=true&limit=25", expect.anything()));

    mine.checked = false;
    mine.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?limit=25", expect.anything()));

    mine.checked = true;
    mine.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?owner_uploads_only=true&limit=25", expect.anything()));
    // Reset clears the own-uploads filter too.
    filters.querySelector<HTMLButtonElement>("button")!.click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith(
      "/api/miniapp/training/packets?limit=25", expect.anything()));
    expect(mine.checked).toBe(false);
    // Browsing the filter bubble never marks the settings form dirty.
    expect(status(root)).toBe("");
  });

  it("prevents Enter in every packet-filter input from saving the lobby", async () => {
    const { root, request } = create();
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    tab(root, 1);
    const inputs = root.querySelectorAll<HTMLInputElement>(".training-packet-filters input");
    expect(inputs).toHaveLength(7);
    for (const input of inputs) {
      const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
      input.dispatchEvent(enter);
      expect(input.form).toBeNull();
      if (!enter.defaultPrevented) input.form?.requestSubmit();
      const letter = new KeyboardEvent("keydown", { key: "a", bubbles: true, cancelable: true });
      input.dispatchEvent(letter);
      expect(letter.defaultPrevented).toBe(false);
    }
    expect(lobbyBodies(request).length).toBe(0);
  });

  it("retains authoritative retired selection and unsaved settings without inventing deselection", async () => {
    const { root, request } = create({ current: { session: null, lobby: { ...lobby, version: 4 } } });
    await vi.waitFor(() => expect(packetCards(root)).toHaveLength(2));
    const save = root.querySelector<HTMLButtonElement>('.training-options button[type="submit"]')!;
    expect(save.disabled).toBe(false);
    const buzzTimer = settingInput(root, "Buzz timer (seconds)");
    buzzTimer.value = "12";
    buzzTimer.dispatchEvent(new Event("input", { bubbles: true }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    packetCards(root)[0]!.querySelector<HTMLButtonElement>(".danger-button")!.click();
    await vi.waitFor(() => expect(status(root)).toContain("saved packet was deleted"));
    expect(root.querySelector(".training-overview .training-selection-packet")!.textContent)
      .toBe("Alpha packet");
    expect(request).toHaveBeenCalledWith("/api/miniapp/training/current", expect.anything());
    // Settings remain independently savable using the authoritative version.
    expect(save.disabled).toBe(false);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(lobbyBodies(request).length).toBe(1));
    expect(lobbyBodies(request)[0]).toMatchObject({ command: "save_settings", expected_version: 4,
      settings: { buzz_timeout: 12 } });
  });

  it("shows upload controls separately without loading the packet picker", () => {
    const { root, request } = create({ upload: true });
    expect(root.querySelector('input[type="file"]')).not.toBeNull();
    expect(root.querySelector("nav")).toBeNull();
    expect(root.querySelector("fieldset")).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it("localizes the redesigned training lobby in Russian", async () => {
    const { root } = create({ locale: "ru" });
    expect(root.querySelector(".training-overview h2")!.textContent).toBe("Лобби тренировки");
    expect([...root.querySelectorAll(".training-overview .training-modifier-badge")].map((b) => b.textContent))
      .toEqual(["Обратный порядок слов", "Скрывать текст вопроса"]);
    tab(root, 2);
    const options = root.querySelector<HTMLElement>(".training-options")!;
    expect([...options.querySelectorAll(".descriptor-category")].map((n) => n.textContent))
      .toEqual(["Модификаторы", "Лучшее нажатие", "Тайминг", "Чтение"]);
    expect(options.querySelector<HTMLSelectElement>(".training-modifier-picker select option")!.textContent)
      .toBe("Выберите модификатор для настройки");
    const note = options.querySelector<HTMLElement>(".training-setting-override")!;
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe("Установлено модификатором «Скрывать текст вопроса»: 0 с.");
    expect(options.querySelector<HTMLElement>(".training-best-buzz-group .training-modifier-description")!.textContent)
      .toBe("Чтение приостанавливается в расчётной точке лучшего нажатия; нажмите «Продолжить чтение», чтобы показать остаток вопроса.");
    await vi.waitFor(() => expect(packetCards(root).length).toBe(2));
    expect(detailValue(packetCards(root)[0]!, "Темы")).toBe("12");
    expect(detailValue(packetCards(root)[0]!, "В библиотеке знаний")).toBe("Да");
    const mine = root.querySelector<HTMLElement>(".training-packet-filters")!
      .querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(mine.closest("label")!.textContent).toBe("Только мои загрузки");
  });
});

const detailValue = (card: HTMLElement, label: string): string => {
  for (const row of card.querySelectorAll(".lobby-packet-detail")) {
    if (row.querySelector("dt")!.textContent === label) return row.querySelector("dd")!.textContent ?? "";
  }
  throw new Error(`missing detail row: ${label}`);
};
