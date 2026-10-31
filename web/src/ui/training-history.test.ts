import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type {
  TrainingHistoryDetailResource,
  TrainingHistoryQuestionRow,
} from "../api/types";
import { I18n } from "../i18n";
import { renderTrainingHistory, renderTrainingHistoryDetail } from "./training-history";

const controllers: AbortController[] = [];
afterEach(() => controllers.forEach((controller) => controller.abort()));

const baseCard = {
  id: "11111111-1111-1111-1111-111111111111",
  created_at: "2026-10-01T12:00:00Z",
  ended_at: "2026-10-01T12:05:00Z",
  status: "completed" as const,
  packets: [{ id: "version", packet_id: "packet", name: "Packet <one>" }],
  score: 20,
  question_total: 5,
  settings: { token_delay: 0.6, buzz_timeout: 10 },
  modifiers: ["reversed"],
};

const row = (changes: Partial<TrainingHistoryQuestionRow> = {}): TrainingHistoryQuestionRow => ({
  position: 0,
  value: 10,
  theme: "Theme",
  text: "Question <text>",
  answer: "Paris",
  accepted_answers: ["City of Paris"],
  rejected_answers: [],
  commentary: "",
  source: "Source",
  protected: null,
  buzz: null,
  stats: {
    presentations: 2, buzzes: 1, attempts: 2, answered: 2, correct: 1, incorrect: 1,
    buzz_timings: null,
  },
  own: {
    answer: "Paris", correct: true, outcome: "answered", token_cursor: 3,
    created_at: "2026-10-01T12:01:00Z",
  },
  ...changes,
});

function historyApi(pages: Record<string, unknown>) {
  const controller = new AbortController();
  controllers.push(controller);
  const request = vi.fn(async (path: string) => {
    for (const [prefix, page] of Object.entries(pages)) {
      if (path.startsWith(prefix)) return page;
    }
    throw new Error(`unexpected request ${path}`);
  });
  return { api: { request } as unknown as ApiClient, request, controller };
}

describe("training history list", () => {
  it("renders cards with More info navigation and no pagination without a cursor", () => {
    const { api } = historyApi({});
    const navigate = vi.fn();
    const root = renderTrainingHistory(
      { kind: "training_history", state: "ready", items: [baseCard], next_cursor: null },
      api, new I18n("en"), new AbortController().signal, navigate,
    );
    const card = root.querySelector("article.training-history-card")!;
    expect(card.textContent).toContain("Packet <one>");
    expect(card.querySelector("question")).toBeNull();
    expect(card.textContent).toContain("20");
    const more = card.querySelector<HTMLButtonElement>("button.training-history-more")!;
    expect(more.getAttribute("aria-label")).toContain("Packet <one>");
    more.click();
    expect(navigate).toHaveBeenCalledWith("/training/history/11111111-1111-1111-1111-111111111111");
    expect(root.querySelector("button.training-history-pagination button")).toBeNull();
    expect(root.querySelector(".training-history-empty")).toBeNull();
  });

  it("shows an empty state and loads one more bounded page on demand", async () => {
    const second = { ...baseCard, id: "22222222-2222-2222-2222-222222222222" };
    const { api, request } = historyApi({
      "/api/miniapp/training/history?cursor=next": {
        kind: "training_history", state: "ready", items: [second], next_cursor: null,
      },
    });
    const root = renderTrainingHistory(
      { kind: "training_history", state: "empty", items: [], next_cursor: "next" },
      api, new I18n("en"), new AbortController().signal, vi.fn(),
    );
    expect(root.querySelector(".training-history-empty")?.textContent)
      .toBe("No training runs yet. Start training to build history.");
    const more = root.querySelector<HTMLButtonElement>(".training-history-pagination button")!;
    more.click();
    await vi.waitFor(() => {
      expect(root.querySelectorAll("article.training-history-card")).toHaveLength(1);
      expect(request).toHaveBeenCalledWith(
        "/api/miniapp/training/history?cursor=next&limit=25",
        { signal: expect.any(AbortSignal) },
      );
    });
    expect(root.querySelector(".training-history-pagination button")).toBeNull();
  });

  it("keeps a failed page retryable and cancels late updates on navigation", async () => {
    const controller = new AbortController(); controllers.push(controller);
    let finish!: (page: unknown) => void;
    const request = vi.fn().mockRejectedValueOnce(new Error("offline"))
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const root = renderTrainingHistory(
      { kind: "training_history", state: "ready", items: [baseCard], next_cursor: "next" },
      { request } as unknown as ApiClient, new I18n("en"), controller.signal, vi.fn(),
    );
    const more = root.querySelector<HTMLButtonElement>(".training-history-pagination button")!;
    more.click(); more.click();
    await vi.waitFor(() => expect(more.disabled).toBe(false));
    expect(request).toHaveBeenCalledTimes(1);
    expect(root.querySelector('[role="status"]')?.textContent).not.toBe("");
    more.click();
    expect(more.disabled).toBe(true);
    controller.abort();
    finish({ items: [{ ...baseCard, id: "late" }], next_cursor: null });
    await Promise.resolve();
    expect(root.querySelectorAll("article.training-history-card")).toHaveLength(1);
  });

  it("localizes to Russian", () => {
    const { api } = historyApi({});
    const root = renderTrainingHistory(
      { kind: "training_history", state: "ready", items: [baseCard], next_cursor: null },
      api, new I18n("ru"), new AbortController().signal, vi.fn(),
    );
    expect(root.querySelector("button.training-history-more")?.textContent).toBe("Подробнее");
  });
});

describe("training history detail", () => {
  const detail = (rows: TrainingHistoryQuestionRow[]): TrainingHistoryDetailResource => ({
    ...baseCard, kind: "training_history_detail", state: "ready",
    protected_source: false, rows, next_cursor: null,
  });

  it("groups consecutive rows by theme and escapes source content", () => {
    const { api } = historyApi({});
    const root = renderTrainingHistoryDetail(
      detail([row(), row({ position: 1, theme: "Other", own: null })]),
      api, new I18n("en"), new AbortController().signal, vi.fn(),
    );
    const themes = [...root.querySelectorAll(".training-history-theme-title")]
      .map((node) => node.textContent);
    expect(themes).toEqual(["Theme", "Other"]);
    const first = root.querySelector("article.training-history-question")!;
    expect(first.querySelector("question")).toBeNull();
    expect(first.textContent).toContain("Question <text>");
    expect(first.textContent).toContain("Timings were not recorded for this run.");
    expect(first.textContent).toContain("Your answer");
    expect(root.querySelector("button.training-history-pagination button")).toBeNull();
  });

  it("withholds protected content instead of fabricating it", () => {
    const { api } = historyApi({});
    const root = renderTrainingHistoryDetail(
      detail([
        row({ protected: "pending", text: null, answer: null, theme: "Theme", own: null }),
        row({ protected: "revoked", text: null, answer: null, theme: null, own: null }),
      ]),
      api, new I18n("en"), new AbortController().signal, vi.fn(),
    );
    const questions = [...root.querySelectorAll("article.training-history-question")];
    expect(questions[0]!.textContent).toContain("Not played in this run yet");
    expect(questions[0]!.textContent).not.toContain("Paris");
    expect(questions[1]!.textContent).toContain("Packet access was revoked; content is unavailable");
  });

  it("loads one more bounded page of rows and keeps ordering", async () => {
    const { api, request } = historyApi({
      "/api/miniapp/training/history/11111111-1111-1111-1111-111111111111?cursor=page2": {
        ...baseCard, kind: "training_history_detail", state: "ready",
        protected_source: false, rows: [row({ position: 2 })], next_cursor: null,
      },
    });
    const root = renderTrainingHistoryDetail(
      { ...detail([row()]), next_cursor: "page2" },
      api, new I18n("en"), new AbortController().signal, vi.fn(),
    );
    const more = root.querySelector<HTMLButtonElement>(".training-history-pagination button")!;
    more.click();
    await vi.waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        "/api/miniapp/training/history/11111111-1111-1111-1111-111111111111?cursor=page2&limit=25",
        { signal: expect.any(AbortSignal) },
      );
      expect(root.querySelectorAll("article.training-history-question")).toHaveLength(2);
    });
    const positions = [...root.querySelectorAll("article.training-history-question")]
      .map((node) => node.getAttribute("data-position"));
    expect(positions).toEqual(["0", "2"]);
  });

  it("navigates back to all runs and surfaces recorded buzz data", () => {
    const { api } = historyApi({});
    const navigate = vi.fn();
    const root = renderTrainingHistoryDetail(
      detail([row({ buzz: { buzzed_at: "2026-10-01T12:00:30Z", buzz_delay_seconds: 2.5, buzz_token_cursor: 4 } })]),
      api, new I18n("en"), new AbortController().signal, navigate,
    );
    root.querySelector<HTMLButtonElement>("button.secondary-button")!.click();
    expect(navigate).toHaveBeenCalledWith("/training/history");
    expect(root.textContent).toContain("2.5s");
  });
});
