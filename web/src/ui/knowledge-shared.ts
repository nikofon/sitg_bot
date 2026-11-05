import { element, replaceChildren } from "./dom";
import type {
  KnowledgeFactDetail,
  KnowledgeI18n,
  KnowledgeMessageKey,
  KnowledgeStatistics,
  KnowledgeStatisticStatus,
  KnowledgeTagPoolEntry,
} from "./knowledge";

/**
 * Runtime helpers shared by the Knowledge library view and its dissection
 * composer / pending-import / practice dialogs. Types come from knowledge.ts
 * as type-only imports so no runtime module cycle is created.
 */

export const DEFAULT_TAG_COLOUR = "#64748b";
export const TAG_COLOUR_PATTERN = /^#[0-9a-f]{6}$/;

/** Knowledge state shared across the library view and its dialogs. */
export interface KnowledgeState {
  tagPool: Map<string, KnowledgeTagPoolEntry>;
  tagPoolReady: Promise<void>;
  /** True when the last tag pool load failed (the pool may be stale/empty). */
  tagPoolFailed?: boolean;
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

const statisticStatusKeys: Record<KnowledgeStatisticStatus, KnowledgeMessageKey> = {
  none: "knowledge.statistic.none",
  pending: "knowledge.statistic.pending",
  fresh: "knowledge.statistic.fresh",
  stale: "knowledge.statistic.stale",
  unavailable: "knowledge.statistic.unavailable",
  denied: "knowledge.statistic.denied",
  failed: "knowledge.statistic.failed",
  pending_calibration: "knowledge.statistic.pending_calibration",
};

function statisticLines(
  statistics: KnowledgeStatistics,
  i18n: KnowledgeI18n,
): string[] {
  const lines = [i18n.t(statisticStatusKeys[statistics.status])];
  if (statistics.mentions) {
    lines.push(
      statistics.mentions.complete
        ? i18n.t("knowledge.statistic.mentions", { count: statistics.mentions.count })
        : i18n.t("knowledge.statistic.mentions_partial", { count: statistics.mentions.count }),
    );
  }
  if (typeof statistics.common_knowledge === "number") {
    lines.push(i18n.t("knowledge.statistic.common", { value: statistics.common_knowledge }));
  }
  if (statistics.bingo) {
    const value =
      statistics.bingo.status === "scored" && statistics.bingo.value !== null
        ? `: ${statistics.bingo.value}`
        : ` · ${i18n.t("knowledge.statistic.pending_calibration")}`;
    lines.push(`${i18n.t("knowledge.statistic.bingo")}${value}`);
  }
  return lines;
}

/**
 * The complete read-only fact view used by library card expansion and the
 * practice reveal: description, author, images, links, tags, statistics and
 * the read-only notice. Editing stays in the editor dialog; packet
 * provenance is never rendered.
 */
export function renderFactDetail(
  detail: KnowledgeFactDetail,
  i18n: KnowledgeI18n,
  container: HTMLElement,
  syncControls: () => void,
): void {
  const links = detail.links.length
    ? element(
      "ul",
      { className: "knowledge-fact-links" },
      ...detail.links.map((link) =>
        element("li", {}, element("a", { href: link.url, target: "_blank", rel: "noreferrer" }, link.title || link.url))),
    )
    : element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.fact.links"));
  const images = detail.images?.length
    ? element("div", { className: "knowledge-fact-images" },
      ...detail.images.map((image) =>
        element("img", { src: image.url, alt: detail.title, loading: "lazy" })))
    : null;
  replaceChildren(container,
    element("p", { className: "knowledge-fact-description" }, detail.description),
    detail.author
      ? element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.fact.author", { author: detail.author }))
      : null,
    images,
    links,
    detail.tags.length
      ? element("p", { className: "knowledge-fact-tags" },
        ...detail.tags.map((tag) => tagChip(tag)))
      : null,
    // Internal packet provenance stays hidden: exposing it would leak other
    // users' private packet names on imported fact libraries.
    element("p", { className: "knowledge-fact-meta" },
      statisticLines(detail.statistics, i18n).join(" · ")),
    detail.editable ? null : element("p", { className: "knowledge-readonly" }, i18n.t("knowledge.fact.read_only")),
  );
  syncControls();
}
