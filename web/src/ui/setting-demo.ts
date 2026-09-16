import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

/** SI settings that control how in-game messages appear over time. */
export const MESSAGE_FLOW_SETTINGS: ReadonlySet<string> = new Set([
  "ready_delay",
  "message_delay",
  "game_start_to_first_theme_delay",
  "theme_to_first_question_delay",
  "question_cost_announcement_delay",
  "question_token_delay",
  "question_token_target_chars",
  "buzz_timer_countdown_delay",
  "buzz_timeout",
  "answer_timeout",
  "between_questions_delay",
  "last_question_to_theme_complete_delay",
  "theme_complete_to_scoreboard_delay",
  "between_themes_delay",
]);

const DEFAULTS: Record<string, number> = {
  ready_delay: 3,
  message_delay: 3,
  game_start_to_first_theme_delay: 3,
  theme_to_first_question_delay: 3,
  question_cost_announcement_delay: 1,
  question_token_delay: 0.6,
  question_token_target_chars: 18,
  buzz_timer_countdown_delay: 0,
  buzz_timeout: 10,
  answer_timeout: 15,
  between_questions_delay: 3,
  last_question_to_theme_complete_delay: 3,
  theme_complete_to_scoreboard_delay: 3,
  between_themes_delay: 3,
};

/** Real-time cap per pause so extreme values cannot stall the preview. */
const CAP_SECONDS = 12;
const TICK_MS = 100;

/** Mirrors the backend announcement chunking used to reveal question text. */
export function announcementTokens(text: string, targetChars: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const chunks: string[] = [];
  let current: string[] = [];
  let currentLength = 0;
  for (const word of words) {
    const addedLength = word.length + (current.length ? 1 : 0);
    if (current.length && currentLength + addedLength > targetChars) {
      chunks.push(current.join(" "));
      current = [];
      currentLength = 0;
    }
    current.push(word);
    currentLength += word.length + (current.length > 1 ? 1 : 0);
    if (word.length >= targetChars) {
      chunks.push(current.join(" "));
      current = [];
      currentLength = 0;
    }
  }
  if (current.length) chunks.push(current.join(" "));
  return chunks;
}

type DemoStep =
  | { kind: "message"; text: string; tone?: "chunk" }
  | { kind: "wait"; seconds: number; focused: boolean }
  | { kind: "countdown"; seconds: number; label: string; focused: boolean };

type TimedStep = Extract<DemoStep, { kind: "wait" | "countdown" }>;

export interface SettingDemo {
  root: HTMLElement;
  restart(): void;
  stop(): void;
}

export function createSettingDemo(
  name: string,
  readSettings: () => Record<string, unknown>,
  i18n: I18n,
): SettingDemo {
  const root = element("div", { className: "setting-demo" });
  const caption = element("p", { className: "setting-demo-caption" }, i18n.t("setting_demo.title"));
  const replay = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => run()) as EventListener,
  }, i18n.t("setting_demo.replay"));
  const log = element("ol", { className: "setting-demo-log", "aria-live": "polite" });
  const status = element("p", { className: "setting-demo-status" });
  root.append(element("div", { className: "setting-demo-toolbar" }, caption, replay), log, status);

  const number = (setting: string): number => {
    const raw = readSettings()[setting];
    const parsed = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : DEFAULTS[setting] ?? 0;
  };
  const message = (key: MessageKey): DemoStep => ({ kind: "message", text: i18n.t(key) });
  const wait = (setting: string, focused: boolean): DemoStep => ({
    kind: "wait", seconds: number(setting), focused,
  });
  const countdown = (setting: string, labelKey: MessageKey): DemoStep => ({
    kind: "countdown", seconds: number(setting), label: i18n.t(labelKey), focused: true,
  });
  const questionChunks = (): string[] => announcementTokens(
    i18n.t("setting_demo.question_text"),
    Math.max(1, Math.round(number("question_token_target_chars"))),
  );

  const buildSteps = (): DemoStep[] => {
    switch (name) {
      case "ready_delay":
        return [wait(name, true), message("setting_demo.msg.ready")];
      case "message_delay":
        return [
          message("setting_demo.msg.plain_a"),
          wait(name, true),
          message("setting_demo.msg.plain_b"),
        ];
      case "game_start_to_first_theme_delay":
        return [
          message("setting_demo.msg.game_start"),
          wait(name, true),
          message("setting_demo.msg.theme1"),
        ];
      case "theme_to_first_question_delay":
        return [
          message("setting_demo.msg.theme1"),
          wait(name, true),
          message("setting_demo.msg.question1"),
        ];
      case "question_cost_announcement_delay":
        return [
          message("setting_demo.msg.question1"),
          wait(name, true),
          message("setting_demo.msg.cost1"),
        ];
      case "question_token_delay":
      case "question_token_target_chars": {
        const focused = name === "question_token_delay";
        const chunks = questionChunks().flatMap((chunk): DemoStep[] => [
          wait("question_token_delay", focused),
          { kind: "message", text: chunk, tone: "chunk" },
        ]);
        return [message("setting_demo.msg.cost1"), ...chunks];
      }
      case "buzz_timer_countdown_delay":
        return [
          message("setting_demo.msg.cost1"),
          wait(name, true),
          message("setting_demo.msg.buzz_open"),
        ];
      case "buzz_timeout":
        return [
          message("setting_demo.msg.buzz_open"),
          countdown(name, "setting_demo.buzz_label"),
          message("setting_demo.msg.no_buzz"),
        ];
      case "answer_timeout":
        return [
          message("setting_demo.msg.buzzed"),
          countdown(name, "setting_demo.answer_label"),
          message("setting_demo.msg.answer_out"),
        ];
      case "between_questions_delay":
        return [
          message("setting_demo.msg.answer_ok"),
          wait(name, true),
          message("setting_demo.msg.cost2"),
        ];
      case "last_question_to_theme_complete_delay":
        return [
          message("setting_demo.msg.answer_ok"),
          wait(name, true),
          message("setting_demo.msg.theme_complete"),
        ];
      case "theme_complete_to_scoreboard_delay":
        return [
          message("setting_demo.msg.theme_complete"),
          wait(name, true),
          message("setting_demo.msg.scoreboard"),
        ];
      case "between_themes_delay":
        return [
          message("setting_demo.msg.scoreboard"),
          wait(name, true),
          message("setting_demo.msg.theme2"),
        ];
      default:
        return [];
    }
  };

  let timer: number | undefined;
  let steps: DemoStep[] = [];
  let index = 0;
  let configuredSeconds = 0;
  let spanMs = 0;
  let elapsedMs = 0;

  const stopTimer = (): void => {
    if (timer !== undefined) {
      window.clearInterval(timer);
      timer = undefined;
    }
  };

  const finish = (): void => {
    stopTimer();
    status.replaceChildren(i18n.t("setting_demo.done"));
  };

  const appendMessage = (step: Extract<DemoStep, { kind: "message" }>): void => {
    log.append(element("li", {
      className: step.tone === "chunk"
        ? "setting-demo-message setting-demo-message--chunk"
        : "setting-demo-message",
    }, step.text));
    log.scrollTop = log.scrollHeight;
  };

  const statusSuffix = (step: TimedStep): string =>
    (step.focused ? ` · ${i18n.t("setting_demo.this_delay")}` : "")
    + (configuredSeconds > CAP_SECONDS ? ` · ${i18n.t("setting_demo.truncated")}` : "");

  const formatStatus = (step: TimedStep, shown: number): string =>
    (step.kind === "wait" ? i18n.t("setting_demo.waiting") : step.label)
    + ` ${shown.toFixed(1)} s${statusSuffix(step)}`;

  const advance = (): void => {
    status.replaceChildren();
    while (index < steps.length && steps[index]!.kind === "message") {
      appendMessage(steps[index] as Extract<DemoStep, { kind: "message" }>);
      index += 1;
    }
    if (index >= steps.length) {
      finish();
      return;
    }
    const step = steps[index] as TimedStep;
    index += 1;
    configuredSeconds = step.seconds;
    spanMs = Math.min(step.seconds, CAP_SECONDS) * 1000;
    elapsedMs = 0;
    status.textContent = formatStatus(step, step.seconds);
  };

  const tick = (): void => {
    if (!root.isConnected) {
      stopTimer();
      return;
    }
    const step = steps[index - 1];
    if (!step || step.kind === "message") {
      stopTimer();
      return;
    }
    elapsedMs += TICK_MS;
    const fraction = spanMs > 0 ? Math.min(1, elapsedMs / spanMs) : 1;
    status.textContent = formatStatus(step, Math.max(0, configuredSeconds * (1 - fraction)));
    if (elapsedMs >= spanMs) advance();
  };

  const run = (): void => {
    stopTimer();
    log.replaceChildren();
    status.replaceChildren();
    steps = buildSteps();
    index = 0;
    timer = window.setInterval(tick, TICK_MS);
    advance();
  };

  return {
    root,
    restart: run,
    stop: (): void => {
      stopTimer();
      status.replaceChildren();
    },
  };
}

