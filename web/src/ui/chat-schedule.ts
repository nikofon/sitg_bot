import type { TournamentChatResource } from "../api/types";
import type { I18n } from "../i18n";
import { element } from "./dom";
import { saveReminder } from "./save-reminder";

export interface ChatScheduleHandlers {
  set: (plannedAt: string | null) => void;
}

function roundLabel(resource: TournamentChatResource, i18n: I18n): string {
  if (resource.multiple_matches) {
    return (
      `${i18n.t("tournament_chat.round")} ${resource.round_number}` +
      ` · ${i18n.t("tournament_chat.game")} ${resource.match_number}`
    );
  }
  return `${i18n.t("tournament_chat.round")} ${resource.round_number}`;
}

function toLocalInputValue(plannedAt?: string | null): string {
  if (!plannedAt) return "";
  const date = new Date(plannedAt);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number): string => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

export function renderChatSchedule(
  resource: TournamentChatResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: ChatScheduleHandlers,
): HTMLElement {
  const participants = resource.participants.map((item) => item.nickname).join(", ");
  const current = resource.planned_at
    ? formatDate(resource.planned_at)
    : i18n.t("chat_schedule.no_time");
  const status = element("p", { className: "field-help", role: "status" });
  const input = element("input", {
    type: "datetime-local",
    step: "60",
    value: toLocalInputValue(resource.planned_at),
    "aria-label": i18n.t("chat_schedule.datetime"),
  });
  const form = element(
    "form",
    { className: "settings-form" },
    element("label", {}, i18n.t("chat_schedule.datetime"), input),
    element(
      "div",
      { className: "settings-actions" },
      element("button", { type: "submit", className: "primary-button" }, i18n.t("chat_schedule.save")),
      resource.planned_at
        ? element(
            "button",
            {
              type: "button",
              className: "danger-button",
              onclick: (() => handlers.set(null)) as EventListener,
            },
            i18n.t("chat_schedule.remove"),
          )
        : null,
    ),
    status,
  );
  form.addEventListener("submit", (event: Event) => {
    event.preventDefault();
    const value = input.value;
    const parsed = value ? new Date(value) : null;
    if (parsed === null || Number.isNaN(parsed.getTime())) {
      status.textContent = i18n.t("chat_schedule.invalid");
      return;
    }
    handlers.set(parsed.toISOString());
  });
  return element(
    "section",
    { className: "route-content chat-schedule" },
    saveReminder(i18n),
    element(
      "header",
      { className: "tournament-heading" },
      element("h2", { className: "tournament-name" }, roundLabel(resource, i18n)),
    ),
    element(
      "ul",
      { className: "detail-list" },
      element("li", {}, `${i18n.t("chat_schedule.tournament")}: ${resource.tournament_name}`),
      element("li", {}, `${i18n.t("chat_schedule.participants")}: ${participants}`),
      element("li", {}, `${i18n.t("chat_schedule.current")}: ${current}`),
    ),
    element("p", { className: "resource-summary" }, i18n.t("chat_schedule.advisory")),
    form,
  );
}
