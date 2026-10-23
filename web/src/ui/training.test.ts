import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { TrainingPacket, TrainingSession } from "../api/types";
import { I18n } from "../i18n";
import { renderTraining } from "./training";

const state: TrainingSession = {
  id: "session", version: 1, status: "active", phase: "waiting", packet_version_id: "packet",
  question_revision_id: "question", question_index: 0, theme: "Theme", value: 10,
  text: "<question>", hidden: false, token_cursor: 2, deadline: null, hidden_deadline: null, settings: {},
};

const controllers: AbortController[] = [];
afterEach(() => { controllers.forEach((controller) => controller.abort()); vi.useRealTimers(); });
function create(session: TrainingSession | null, packets: TrainingPacket[] = []) {
  const controller = new AbortController(); controllers.push(controller);
  const request = vi.fn(async (path: string) => path.includes("packets")
    ? { items: packets, next_cursor: null } : { session });
  const root = renderTraining({ session }, { request } as unknown as ApiClient, new I18n("en"), controller.signal);
  return { root, request, controller };
}

describe("training", () => {
  it("keeps separately authorized versions of one logical packet", async () => {
    const packet: TrainingPacket = { id: "one", packet_id: "logical", name: "One", year: null, scope: "tournament" };
    const { root } = create(null, [packet, { ...packet, id: "two", name: "Two" }]);
    await vi.waitFor(() => expect(root.querySelectorAll("article")).toHaveLength(2));
  });
  it("escapes source text and hides expired text immediately on reconnect", () => {
    const { root } = create({ ...state, hidden_deadline: "2000-01-01T00:00:00Z" });
    expect(root.querySelector(".training-question")?.textContent).toBe("Question text is hidden.");
    expect(root.querySelector("question")).toBeNull();
  });

  it("hides text on its persisted deadline without waiting for a response", async () => {
    vi.useFakeTimers();
    const { root, controller } = create({ ...state, hidden_deadline: new Date(Date.now() + 1500).toISOString() });
    expect(root.querySelector(".training-question")?.textContent).toBe("<question>");
    await vi.advanceTimersByTimeAsync(1600);
    expect(root.querySelector(".training-question")?.textContent).toBe("Question text is hidden.");
    controller.abort();
  });

  it("keeps answer input while polling the same version and cancels on navigation", async () => {
    vi.useFakeTimers();
    const { root, request, controller } = create({ ...state, phase: "answering" });
    const answer = root.querySelector<HTMLInputElement>('input[aria-label="Submit answer"]')!;
    answer.value = "Partially typed";
    await vi.advanceTimersByTimeAsync(1300);
    expect(root.querySelector('input[aria-label="Submit answer"]')).toBe(answer);
    expect(answer.value).toBe("Partially typed");
    controller.abort();
    const count = request.mock.calls.length;
    await vi.advanceTimersByTimeAsync(2000);
    expect(request.mock.calls.length).toBe(count);
  });

  it("preserves focused answers across presentation updates and resets for a new question", async () => {
    vi.useFakeTimers();
    const current = { ...state, phase: "answering" as const, presentation_version: 1 };
    const { root, request } = create(current);
    document.body.append(root);
    const answer = root.querySelector<HTMLInputElement>('input[aria-label="Submit answer"]')!;
    answer.focus();
    answer.value = "Partially typed";
    answer.setSelectionRange(3, 7);
    request.mockImplementation(async () => ({ session: {
      ...current, presentation_version: 2, hidden: true,
    } }));
    await vi.advanceTimersByTimeAsync(650);
    expect(root.querySelector('input[aria-label="Submit answer"]')).toBe(answer);
    expect(document.activeElement).toBe(answer);
    expect(answer.value).toBe("Partially typed");
    expect(answer.selectionStart).toBe(3);
    expect(root.querySelector(".training-question")?.textContent).toBe("Question text is hidden.");
    request.mockImplementation(async () => ({ session: {
      ...current, version: 2, question_revision_id: "next-question",
    } }));
    await vi.advanceTimersByTimeAsync(650);
    const next = root.querySelector<HTMLInputElement>('input[aria-label="Submit answer"]')!;
    expect(next).not.toBe(answer);
    expect(next.value).toBe("");
    root.remove();
  });

  it("allows hidden plus reversed and displaces Best buzz", () => {
    const { root } = create(null);
    root.querySelectorAll<HTMLButtonElement>("nav button")[2]!.click(); // Options
    const options = root.querySelector<HTMLElement>(".training-options")!;
    const picker = options.querySelector<HTMLSelectElement>(".training-modifier-picker select")!;
    const config = () => options.querySelector<HTMLInputElement>('.training-modifier-config input[type="checkbox"]')!;
    const enable = (): void => {
      config().checked = true;
      config().dispatchEvent(new Event("change", { bubbles: true }));
    };
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    enable();
    expect(config().checked).toBe(true);
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    enable();
    expect(config().disabled).toBe(false);
    const seconds = options.querySelector<HTMLInputElement>('.training-modifier-config input[type="number"]')!;
    expect(seconds.disabled).toBe(false);
    // Best buzz is configured in its own separate section.
    const bestBuzz = options.querySelector<HTMLInputElement>(".training-best-buzz-group input[type=\"checkbox\"]")!;
    bestBuzz.checked = true;
    bestBuzz.dispatchEvent(new Event("change", { bubbles: true }));
    // Checking Best buzz displaces the incompatible reversed and hidden modifiers.
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(config().checked).toBe(false);
    picker.value = "hidden";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(config().checked).toBe(false);
    // Unchecking Best buzz restores the displaced preferences.
    bestBuzz.checked = false;
    bestBuzz.dispatchEvent(new Event("change", { bubbles: true }));
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(config().checked).toBe(true);
  });
});
