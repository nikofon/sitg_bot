import type { I18n } from "../i18n";
import { element } from "./dom";

export function saveReminder(i18n: I18n): HTMLElement {
  return element("aside", { className: "save-reminder", role: "note" },
    i18n.t("common.save_reminder"));
}
