import { ApiClient, ApiError } from "../api/client";
import type {
  TrainingHistoryCard,
  TrainingHistoryDetailResource,
  TrainingHistoryListResource,
  TrainingHistoryQuestionRow,
} from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element, replaceChildren } from "./dom";

type ModifierKey = "reversed" | "hidden" | "best_buzz";

const MODIFIER_LABEL_KEYS: Record<ModifierKey, MessageKey> = {
  reversed: "training.reversed",
  hidden: "training.hidden",
  best_buzz: "training.best_buzz",
};

const modifierLabel = (key: string): MessageKey =>
  MODIFIER_LABEL_KEYS[key as ModifierKey] ?? "training.modifiers";

const statusKey = (status: TrainingHistoryCard["status"]): MessageKey =>
  `training_history.status_${status}` as MessageKey;

const outcomeKey = (outcome: string): MessageKey =>
  `training_history.own_outcome_${outcome}` as MessageKey;

function formatDate(i18n: I18n, value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat(i18n.locale, {
        day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
      }).format(date);
}

function modifierBadges(card: TrainingHistoryCard, i18n: I18n): HTMLElement {
  return element("ul", { className: "training-modifier-badges training-history-modifiers",
    "aria-label": i18n.t("training.modifiers") },
    ...card.modifiers.map((key) =>
      element("li", { className: "training-modifier-badge" }, i18n.t(modifierLabel(key)))));
}

function packetNames(card: TrainingHistoryCard): string {
  return card.packets.map((packet) => packet.name ?? "—").join(", ") || "—";
}

function settingsDetails(card: TrainingHistoryCard, i18n: I18n): HTMLElement {
  const fields: Array<[string, MessageKey]> = [
    ["token_delay", "training.token_delay"], ["token_target_chars", "training.token_chars"],
    ["buzz_timeout", "training.buzz_seconds"], ["answer_timeout", "training.answer_seconds"],
    ["hidden_after", "training.hidden_seconds"],
  ];
  return element("details", { className: "training-history-settings" },
    element("summary", {}, i18n.t("training_history.settings")),
    element("dl", { className: "lobby-packet-details" },
      ...fields.filter(([name]) => typeof card.settings[name] === "number")
        .map(([name, key]) => element("div", { className: "lobby-packet-detail" },
          element("dt", {}, i18n.t(key)), element("dd", {},
            new Intl.NumberFormat(i18n.locale).format(Number(card.settings[name])))))));
}

function summaryDetails(card: TrainingHistoryCard, i18n: I18n): HTMLElement {
  const row = (key: MessageKey, value: string): HTMLElement =>
    element("div", { className: "lobby-packet-detail" },
      element("dt", {}, i18n.t(key)), element("dd", {}, value));
  return element("dl", { className: "lobby-packet-details training-history-summary" },
    row("training_history.played_at", formatDate(i18n, card.created_at)),
    row("training_history.packets", packetNames(card)),
    row("training_history.score", String(card.score)),
    row("training_history.questions", String(card.question_total)),
    row("training_history.status", i18n.t(statusKey(card.status))),
    row("training.modifiers", card.modifiers.length
      ? card.modifiers.map((key) => i18n.t(modifierLabel(key))).join(", ") : "—"));
}

function statusCard(kind: "empty" | "loading" | "error", text: string): HTMLElement {
  return element("p", { className: `training-history-${kind}`, role: "status",
    "aria-live": kind === "error" ? "assertive" : "polite" }, text);
}

function loadMoreButton(
  i18n: I18n,
  key: MessageKey,
  onLoad: (button: HTMLButtonElement) => void,
): HTMLButtonElement {
  const button = element("button", { type: "button", className: "secondary-button" },
    i18n.t(key));
  button.addEventListener("click", () => onLoad(button));
  return button;
}

export function renderTrainingHistory(
  initial: TrainingHistoryListResource,
  api: ApiClient,
  i18n: I18n,
  signal: AbortSignal,
  navigate: (path: string) => void,
): HTMLElement {
  const root = element("section", { className: "route-content training-history" });
  const items = [...initial.items];
  let nextCursor = initial.next_cursor;
  const notice = element("p", { role: "status", "aria-live": "polite" });
  const list = element("ul", { className: "stack training-history-list" });
  const pagination = element("div", { className: "training-history-pagination" });

  const failure = (error: unknown): void => {
    if (signal.aborted) return;
    const code = error instanceof ApiError ? error.code : "internal_error";
    notice.textContent = i18n.t(`error.${code}` as MessageKey);
  };

  const card = (item: TrainingHistoryCard): HTMLElement => {
    const details = element("button", {
      type: "button",
      className: "primary-button training-history-more",
      "aria-label": `${i18n.t("training_history.more_info")}: ${packetNames(item)} · ${formatDate(i18n, item.created_at)}`,
      onclick: (() => navigate(`/training/history/${item.id}`)) as EventListener,
    }, i18n.t("training_history.more_info"));
    return element("li", {},
      element("article", { className: "resource-card training-history-card" },
        element("h3", {}, formatDate(i18n, item.created_at)),
        element("p", { className: "training-history-packet" }, packetNames(item)),
        element("p", { className: "training-history-score" },
          `${i18n.t("training_history.score")}: ${item.score}`),
        modifierBadges(item, i18n),
        element("p", { className: "training-history-status" },
          i18n.t(statusKey(item.status))),
        settingsDetails(item, i18n), details));
  };

  const render = (): void => {
    replaceChildren(list, ...items.map(card));
    if (!items.length) {
      list.append(element("li", {}, statusCard("empty", i18n.t("training_history.empty"))));
    }
    replaceChildren(pagination, nextCursor ? more : null);
  };

  const more = loadMoreButton(i18n, "training_history.load_more", (button) => {
    if (!nextCursor || button.disabled || signal.aborted) return;
    button.disabled = true;
    notice.textContent = i18n.t("common.loading");
    const cursor = nextCursor;
    api.request<TrainingHistoryListResource>(
      `/api/miniapp/training/history?cursor=${encodeURIComponent(cursor)}&limit=25`,
      { signal },
    ).then((page) => {
      if (signal.aborted) return;
      items.push(...page.items);
      nextCursor = page.next_cursor;
      notice.textContent = "";
      render();
    }).catch(failure).finally(() => { button.disabled = false; });
  });

  render();
  root.append(notice, list, pagination);
  return root;
}

function questionRow(row: TrainingHistoryQuestionRow, i18n: I18n): HTMLElement {
  const detail = (key: MessageKey, value: string): HTMLElement =>
    element("div", { className: "lobby-packet-detail" },
      element("dt", {}, i18n.t(key)), element("dd", {}, value));
  const details: HTMLElement[] = [];
  if (row.protected) {
    details.push(detail("training_history.question", i18n.t(`training_history.protected_${row.protected}` as MessageKey)));
  } else {
    details.push(detail("training_history.question", row.text ?? "—"));
    details.push(detail("training_history.answer", row.answer ?? "—"));
    if (row.accepted_answers?.length) {
      details.push(detail("training_history.accepted", row.accepted_answers.join("; ")));
    }
    if (row.rejected_answers?.length) {
      details.push(detail("training_history.rejected", row.rejected_answers.join("; ")));
    }
    if (row.commentary) details.push(detail("training_history.commentary", row.commentary));
    if (row.source) details.push(detail("training_history.source", row.source));
  }
  const stats = element("dl", { className: "lobby-packet-details training-history-stats" },
    detail("training_history.presentations", String(row.stats.presentations)),
    detail("training_history.buzzes", row.stats.buzzes == null ? "—" : String(row.stats.buzzes)),
    detail("training_history.attempts", String(row.stats.attempts)),
    detail("training_history.answered", String(row.stats.answered)),
    detail("training_history.correct", String(row.stats.correct)),
    detail("training_history.incorrect", String(row.stats.incorrect)),
    detail("training_history.buzz_timings",
      row.stats.buzz_timings === null
        ? i18n.t("training_history.timings_unavailable")
        : row.stats.buzz_timings.length
          ? row.stats.buzz_timings.map((seconds) => `${seconds}s`).join(", ")
          : "—"));
  const own = element("dl", { className: "lobby-packet-details training-history-own" },
    ...(row.own
      ? [
          detail("training_history.own_answer", row.own.answer ?? "—"),
          detail("training_history.own_result",
            row.own.correct === null || row.own.correct === undefined
              ? i18n.t(outcomeKey(row.own.outcome))
              : `${i18n.t(row.own.correct ? "training_history.correct" : "training_history.incorrect")} · ${i18n.t(outcomeKey(row.own.outcome))}`),
          ...(row.own.original_correct !== undefined && row.own.original_correct !== null && row.own.original_correct !== row.own.correct
            ? [detail("training_history.own_original", i18n.t(row.own.original_correct ? "training_history.correct" : "training_history.incorrect"))]
            : []),
          ...(row.own.appeal_status
            ? [detail("training_history.appeal_status",
                `${i18n.t(`training_history.appeal_${row.own.appeal_status}` as MessageKey)}${row.own.appeal_resolved_at ? ` · ${formatDate(i18n, row.own.appeal_resolved_at)}` : ""}`)]
            : []),
        ]
      : [detail("training_history.own_result", "—")]));
  own.append(detail("training_history.buzz_time", row.buzz
    ? formatDate(i18n, row.buzz.buzzed_at)
    : row.own ? i18n.t("training_history.timings_unavailable") : "—"));
  if (row.buzz) {
    own.append(detail("training_history.buzz_delay", row.buzz.buzz_delay_seconds === null
      ? i18n.t("training_history.timings_unavailable")
      : `${row.buzz.buzz_delay_seconds}s`));
  }
  return element("article", { className: "resource-card training-history-question",
    "data-position": String(row.position) },
    element("h4", { className: "training-history-question-title" },
      `${row.position + 1}. ${row.theme ?? i18n.t("training_history.theme")} · ${row.value}`),
    element("dl", { className: "lobby-packet-details training-history-content" }, ...details),
    element("section", {}, element("h5", { className: "training-history-stats-title" },
      i18n.t("training_history.stats")), stats),
    element("section", {}, element("h5", { className: "training-history-stats-title" },
      i18n.t("training_history.own_performance")), own));
}

export function renderTrainingHistoryDetail(
  initial: TrainingHistoryDetailResource,
  api: ApiClient,
  i18n: I18n,
  signal: AbortSignal,
  navigate: (path: string) => void,
): HTMLElement {
  const root = element("section", { className: "route-content training-history training-history-detail" });
  const rows = [...initial.rows];
  let nextCursor = initial.next_cursor;
  const notice = element("p", { role: "status", "aria-live": "polite" });
  const back = element("button", { type: "button", className: "secondary-button",
    onclick: (() => navigate("/training/history")) as EventListener },
    i18n.t("training_history.back"));
  const summary = element("article", { className: "resource-card training-history-summary-card" },
    element("h3", {}, i18n.t("training_history.run_summary")),
    element("p", { className: "training-history-packet" }, packetNames(initial)),
    summaryDetails(initial, i18n), settingsDetails(initial, i18n));
  if (initial.protected_source) {
    summary.append(statusCard("error", i18n.t("training_history.protected_revoked")));
  }
  const list = element("ol", { className: "stack training-history-questions" });
  const pagination = element("div", { className: "training-history-pagination" });

  const failure = (error: unknown): void => {
    if (signal.aborted) return;
    const code = error instanceof ApiError ? error.code : "internal_error";
    notice.textContent = i18n.t(`error.${code}` as MessageKey);
  };

  const render = (): void => {
    replaceChildren(list);
    let currentTheme: string | null | undefined;
    let group: HTMLElement | null = null;
    for (const row of rows) {
      if (row.theme !== currentTheme) {
        currentTheme = row.theme;
        group = element("li", {},
          element("section", { className: "training-history-theme" },
            element("h3", { className: "training-history-theme-title" },
              row.theme ?? i18n.t("training_history.theme"))));
        list.append(group);
      }
      (group?.firstChild as HTMLElement)?.append(questionRow(row, i18n));
    }
    if (!rows.length) {
      list.append(element("li", {}, statusCard("empty", i18n.t("route.training_history_detail.empty"))));
    }
    replaceChildren(pagination, nextCursor ? more : null);
  };

  const more = loadMoreButton(i18n, "training_history.load_more_questions", (button) => {
    if (!nextCursor || button.disabled || signal.aborted) return;
    button.disabled = true;
    notice.textContent = i18n.t("common.loading");
    const cursor = nextCursor;
    api.request<TrainingHistoryDetailResource>(
      `/api/miniapp/training/history/${encodeURIComponent(initial.id)}`
        + `?cursor=${encodeURIComponent(cursor)}&limit=25`,
      { signal },
    ).then((page) => {
      if (signal.aborted) return;
      if (page.protected_source) {
        // Later requests may observe source retirement/revocation. Replace old
        // protected content rather than keeping stale text on the screen.
        for (let index = 0; index < rows.length; index += 1) {
          rows[index] = { ...rows[index]!, protected: "revoked", theme: null,
            text: null, answer: null, accepted_answers: null, rejected_answers: null,
            commentary: null, source: null };
        }
      }
      rows.push(...page.rows);
      nextCursor = page.next_cursor;
      notice.textContent = "";
      render();
    }).catch(failure).finally(() => { button.disabled = false; });
  });

  render();
  root.append(notice, back, summary, list, pagination);
  return root;
}
