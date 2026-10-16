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

  it("allows hidden plus reversed and excludes Best buzz", () => {
    const { root } = create(null);
    const boxes = root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    boxes[0]!.checked = true; boxes[0]!.dispatchEvent(new Event("change"));
    boxes[1]!.checked = true; boxes[1]!.dispatchEvent(new Event("change"));
    expect(boxes[0]!.disabled).toBe(false);
    expect(boxes[1]!.disabled).toBe(false);
    expect(boxes[2]!.disabled).toBe(true);
    expect(root.querySelector<HTMLInputElement>('input[type="number"]')?.disabled).toBe(false);
  });
});
