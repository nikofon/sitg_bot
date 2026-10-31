import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { TrainingPacket, TrainingResult, TrainingSession } from "../api/types";
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
  const request = vi.fn(async (path: string): Promise<TrainingResult | { items: TrainingPacket[]; next_cursor: null }> => path.includes("packets")
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

  it.each(["buzz", "pause"])("ignores a pre-%s poll after the action response", async (command) => {
    vi.useFakeTimers();
    const { root, request, controller } = create({ ...state, phase: "reading" });
    let finishPoll!: (result: TrainingResult) => void;
    let finishAction!: (result: TrainingResult) => void;
    request.mockImplementation((path: string) => {
      if (path.endsWith("/current")) return new Promise((resolve) => { finishPoll = resolve; });
      return new Promise((resolve) => { finishAction = resolve; });
    });
    await vi.advanceTimersByTimeAsync(600);
    expect(finishPoll).toBeTypeOf("function");
    const label = command === "buzz" ? "Buzz" : "Pause";
    const button = Array.from(root.querySelectorAll<HTMLButtonElement>("button"))
      .find((value) => value.textContent === label)!;
    button.click();
    finishAction({ session: {
      ...state, version: 2, phase: command === "buzz" ? "answering" : "reading",
      status: command === "pause" ? "paused" : "active",
    } });
    await vi.advanceTimersByTimeAsync(0);
    const input = root.querySelector<HTMLInputElement>('input[aria-label="Submit answer"]');
    if (command === "buzz") { expect(input).not.toBeNull(); input!.value = "Typed answer"; }
    finishPoll({ session: { ...state, phase: "reading" } });
    await vi.advanceTimersByTimeAsync(0);
    if (command === "buzz") {
      expect(root.querySelector('input[aria-label="Submit answer"]')).toBe(input);
      expect(input!.value).toBe("Typed answer");
    } else {
      expect(root.textContent).toContain("Resume");
      const count = request.mock.calls.length;
      await vi.advanceTimersByTimeAsync(1200);
      expect(request.mock.calls.length).toBe(count);
    }
    controller.abort();
  });

  it("ignores a poll that arrives while the newer action is still pending", async () => {
    vi.useFakeTimers();
    const { root, request } = create({ ...state, phase: "answering" });
    let finishPoll!: (result: TrainingResult) => void;
    let finishAction!: (result: TrainingResult) => void;
    request.mockImplementation((path: string) => path.endsWith("/current")
      ? new Promise((resolve) => { finishPoll = resolve; })
      : new Promise((resolve) => { finishAction = resolve; }));
    const input = root.querySelector<HTMLInputElement>('input[aria-label="Submit answer"]')!;
    input.value = "Answer in flight";
    await vi.advanceTimersByTimeAsync(600);
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    finishPoll({ session: { ...state, phase: "reading" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector('input[aria-label="Submit answer"]')).toBe(input);
    expect(input.value).toBe("Answer in flight");
    finishAction({ session: { ...state, version: 2, phase: "revealed", answer: "Correct answer" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(root.querySelector('input[aria-label="Submit answer"]')).toBeNull();
    expect(root.textContent).toContain("Correct answer");
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
    bestBuzz.checked = false;
    bestBuzz.dispatchEvent(new Event("change", { bubbles: true }));
    picker.value = "reversed";
    picker.dispatchEvent(new Event("change", { bubbles: true }));
    expect(config().checked).toBe(false);
    enable();
    expect(config().checked).toBe(true);
    bestBuzz.checked = true;
    bestBuzz.dispatchEvent(new Event("change", { bubbles: true }));
    enable();
    expect(bestBuzz.checked).toBe(false);
    config().checked = false;
    config().dispatchEvent(new Event("change", { bubbles: true }));
    expect(bestBuzz.checked).toBe(false);
  });
});
