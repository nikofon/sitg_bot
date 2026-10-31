import { element } from "./dom";
import type { KnowledgeI18n, KnowledgeMessageKey, KnowledgeTagPoolEntry } from "./knowledge";

/**
 * Runtime helpers shared by the Knowledge library view and its dissection
 * composer / pending-import dialogs. Types come from knowledge.ts as
 * type-only imports so no runtime module cycle is created.
 */

export const DEFAULT_TAG_COLOUR = "#64748b";
export const TAG_COLOUR_PATTERN = /^#[0-9a-f]{6}$/;

/** Knowledge state shared across the library view and its dialogs. */
export interface KnowledgeState {
  tagPool: Map<string, KnowledgeTagPoolEntry>;
  tagPoolReady: Promise<void>;
}

export function tagBorderGradient(colours: string[]): string {
  const stops = colours.length === 1 ? [colours[0], colours[0]] : colours;
  return `linear-gradient(120deg, ${stops.join(", ")})`;
}

export function tagChip(tag: { name: string; label?: string; colour?: string }): HTMLElement {
  const colour = tag.colour && TAG_COLOUR_PATTERN.test(tag.colour) ? tag.colour : DEFAULT_TAG_COLOUR;
  const chip = element("span", { className: "knowledge-tag-chip" }, tag.label ?? tag.name);
  chip.style.color = colour;
  return chip;
}

export function openDialog(dialog: HTMLDialogElement): void {
  document.body.append(dialog);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

export function closeDialog(dialog: HTMLDialogElement): void {
  if (typeof dialog.close === "function") dialog.close();
  dialog.remove();
}

export function dialogFrame(
  title: string,
  i18n: KnowledgeI18n,
): { dialog: HTMLDialogElement; body: HTMLElement } {
  const dialog = element("dialog", { className: "tournament-dialog knowledge-dialog", "aria-label": title });
  const body = element("div", { className: "knowledge-dialog-body" });
  const close = element("button", {
    type: "button",
    className: "icon-button knowledge-dialog-close",
    "aria-label": i18n.t("common.close" as KnowledgeMessageKey),
    onclick: (() => { closeDialog(dialog); }) as EventListener,
  }, "×");
  dialog.append(element("div", { className: "dialog-heading" },
    element("h2", {}, title), close), body);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  return { dialog, body };
}

/** Localized short date-time for import cards and quota reset times. */
export function formatDateTime(value: string, locale?: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale ?? "en", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}
