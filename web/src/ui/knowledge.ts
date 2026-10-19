import { element, replaceChildren } from "./dom";
import { PacketLoader } from "./packet-loading";

export type KnowledgeOrder = "title_asc" | "title_desc" | "updated_desc";

export const knowledgeOrders: readonly KnowledgeOrder[] = ["title_asc", "title_desc", "updated_desc"];

export type KnowledgePacketOrder = "id_asc" | "name_asc" | "name_desc" | "year_asc" | "year_desc";

export const knowledgePacketOrders: readonly KnowledgePacketOrder[] = [
  "id_asc", "name_asc", "name_desc", "year_asc", "year_desc",
];

export type KnowledgeTab = "library" | "packets";

export type KnowledgeStatisticStatus =
  | "none"
  | "pending"
  | "fresh"
  | "stale"
  | "unavailable"
  | "denied"
  | "failed"
  | "pending_calibration";

export interface KnowledgeMentions {
  count: number;
  complete: boolean;
}

export interface KnowledgeStatistics {
  status: KnowledgeStatisticStatus;
  mentions?: KnowledgeMentions | null;
  common_knowledge?: number | null;
  bingo?: { status: string; value: number | null } | null;
}

/** Tag as returned by the fact detail contract (id + name + colour). */
export interface KnowledgeTag {
  id: string;
  name: string;
  colour: string;
}

/** Tag as returned by the tag pool contract (tag_id + name + colour). */
export interface KnowledgeTagPoolEntry {
  tag_id: string;
  name: string;
  colour: string;
}

export interface KnowledgeTagPool {
  items: KnowledgeTagPoolEntry[];
}

export interface KnowledgeFactSummary {
  id: string;
  title: string;
  tag_names: string[];
  origin: "manual" | "dissection";
  statistics: KnowledgeStatistics;
  updated_at: string;
}

export interface KnowledgeFactsResource {
  state: "ready" | "empty";
  items: KnowledgeFactSummary[];
  total: number;
  next_cursor: string | null;
  order: KnowledgeOrder;
  search?: string;
}

export interface KnowledgeFactDetail {
  id: string;
  title: string;
  description: string;
  links: Array<{ url: string; title: string }>;
  tags: KnowledgeTag[];
  origin: "manual" | "dissection";
  content_version: number;
  editable: boolean;
  statistics: KnowledgeStatistics;
}

export interface ShareLink {
  id: string;
  token: string;
  url: string;
  created_at: string;
  deactivated_at: string | null;
  active: boolean;
}

export interface KnowledgeSharing {
  grants: Array<{ id: string; recipient_name: string; copied_count: number }>;
  shared?: Array<{ owner_id: string; owner_name: string; copied_count: number }>;
  links?: ShareLink[];
}

export interface KnowledgeImportResult {
  owner_id: string;
  recipient_id: string;
  imported_count: number;
  skipped_count: number;
}

export interface KnowledgePacketCard {
  id: string;
  packet_id: string;
  name: string;
  year: number | null;
  scope: "training" | "tournament";
  authors: Array<{ id: string; name: string }>;
  tournaments: Array<{ id: string; name: string }>;
  total_theme_count: number;
  fresh_theme_count: number;
  dissected_theme_count: number | null;
  dissected: boolean;
  best_buzz_calculated: boolean;
}

export interface KnowledgeDissection {
  next_cursor?: string | null;
  packets: Array<{ id: string; name: string }>;
  requests: Array<{
    id: string;
    request_id?: string;
    packet_id: string;
    status: string;
    base: boolean;
    applied_count: number;
    partial: boolean;
  }>;
}

export interface KnowledgeViewData {
  facts: KnowledgeFactsResource;
  sharing: KnowledgeSharing;
  dissection: KnowledgeDissection;
  import_token?: string;
}

export interface KnowledgeDissectOutcome {
  confirmation_required?: boolean;
}

export interface KnowledgeActions {
  signal?: AbortSignal;
  loadFacts: (params: {
    owner_id?: string;
    tag?: string;
    cursor?: string | null;
    search?: string;
    order?: KnowledgeOrder;
  }) => Promise<KnowledgeFactsResource>;
  loadFact: (factId: string) => Promise<KnowledgeFactDetail>;
  save?: (values: KnowledgeFactInput, detail?: KnowledgeFactDetail) => Promise<KnowledgeFactDetail>;
  remove?: (detail: KnowledgeFactDetail) => Promise<void>;
  merge?: (source: KnowledgeFactDetail, targetId: string) => Promise<void>;
  tag?: (command: "tag_rename" | "tag_delete", tagId: string, name?: string, colour?: string) => Promise<void>;
  loadTags?: () => Promise<KnowledgeTagPool>;
  createTag?: (name: string, colour: string) => Promise<KnowledgeTagPoolEntry>;
  loadSharing?: () => Promise<KnowledgeSharing>;
  grant?: (recipient: string) => Promise<void>;
  createLink?: () => Promise<ShareLink>;
  deactivateLink?: (linkId: string) => Promise<ShareLink | void>;
  importLibrary?: (token: string) => Promise<KnowledgeImportResult>;
  consumeImportToken?: () => void;
  resolveShareUrl?: (url: string) => string;
  loadPackets?: (query: URLSearchParams, signal: AbortSignal) => Promise<{
    items: KnowledgePacketCard[];
    next_cursor: string | null;
  }>;
  dissect: (packetId: string, instructions: string, comment?: string, confirmExposure?: boolean)
    => Promise<KnowledgeDissectOutcome>;
  loadDissections?: () => Promise<KnowledgeDissection["requests"]>;
  cancel?: (requestId: string) => Promise<void>;
}

export interface KnowledgeFactInput {
  title: string;
  description: string;
  tag_names: string[];
  links: Array<{ url: string; title?: string; position: number }>;
}

export const knowledgeMessageKeys = [
  "knowledge.sharing.permanent",
  "knowledge.sharing.received",
  "knowledge.sharing.count",
  "knowledge.create",
  "knowledge.save",
  "knowledge.delete",
  "knowledge.title_field",
  "knowledge.description",
  "knowledge.links_field",
  "knowledge.tags_field",
  "knowledge.confirm_delete",
  "knowledge.refresh",
  "knowledge.shared_collections",
  "knowledge.own",
  "knowledge.tag_filter",
  "knowledge.tag_filter_all",
  "knowledge.merge",
  "knowledge.merge_target",
  "knowledge.tag_rename",
  "knowledge.tag_delete",
  "knowledge.dissection.comment",
  "knowledge.dissection.status.queued",
  "knowledge.dissection.status.running",
  "knowledge.dissection.status.cancelled",
  "knowledge.cancel",
  "knowledge.ai_unavailable",
  "knowledge.capabilities",
  "knowledge.confirm_exposure",
  "knowledge.title",
  "knowledge.search",
  "knowledge.order.title_asc",
  "knowledge.order.title_desc",
  "knowledge.order.updated_desc",
  "knowledge.total",
  "knowledge.empty",
  "knowledge.loading",
  "knowledge.error",
  "knowledge.quota",
  "knowledge.load_more",
  "knowledge.fact.tags",
  "knowledge.fact.links",
  "knowledge.fact.origin.manual",
  "knowledge.fact.origin.dissection",
  "knowledge.fact.read_only",
  "knowledge.fact.version",
  "knowledge.fact.edit",
  "knowledge.statistic.none",
  "knowledge.statistic.pending",
  "knowledge.statistic.fresh",
  "knowledge.statistic.stale",
  "knowledge.statistic.unavailable",
  "knowledge.statistic.denied",
  "knowledge.statistic.failed",
  "knowledge.statistic.pending_calibration",
  "knowledge.statistic.mentions",
  "knowledge.statistic.mentions_partial",
  "knowledge.statistic.common",
  "knowledge.statistic.bingo",
  "knowledge.tab.library",
  "knowledge.tab.packets",
  "knowledge.share",
  "knowledge.sharing.title",
  "knowledge.sharing.empty",
  "knowledge.sharing.recipient",
  "knowledge.sharing.grant",
  "knowledge.sharing.revoked",
  "knowledge.sharing.link_hint",
  "knowledge.sharing.create_link",
  "knowledge.sharing.links",
  "knowledge.sharing.links_inactive",
  "knowledge.sharing.link_url",
  "knowledge.sharing.copy",
  "knowledge.sharing.copied",
  "knowledge.sharing.deactivate",
  "knowledge.sharing.deactivated",
  "knowledge.sharing.confirm_deactivate",
  "knowledge.sharing.direct",
  "knowledge.sharing.empty_links",
  "knowledge.tags.new",
  "knowledge.tags.create",
  "knowledge.tags.name",
  "knowledge.tags.colour",
  "knowledge.tags.selected",
  "knowledge.tags.field_empty",
  "knowledge.import.title",
  "knowledge.import.body",
  "knowledge.import.confirm",
  "knowledge.import.dismiss",
  "knowledge.import.done",
  "knowledge.packets.search",
  "knowledge.packets.reset",
  "knowledge.packets.empty",
  "knowledge.packets.no_matches",
  "knowledge.packets.loading",
  "knowledge.packets.fresh",
  "knowledge.packets.fresh_label",
  "knowledge.packets.dissected",
  "knowledge.packets.dissected_label",
  "knowledge.packets.dissected_unknown",
  "knowledge.packets.dissected_complete",
  "knowledge.packets.scope.training",
  "knowledge.packets.scope.tournament",
  "knowledge.packets.jobs",
  "knowledge.dissection.title",
  "knowledge.dissection.packet",
  "knowledge.dissection.instructions",
  "knowledge.dissection.submit",
  "knowledge.dissection.submit_short",
  "knowledge.dissection.confirm",
  "knowledge.dissection.empty",
  "knowledge.dissection.status.pending",
  "knowledge.dissection.status.succeeded",
  "knowledge.dissection.status.partially_succeeded",
  "knowledge.dissection.status.failed",
  "knowledge.dissection.partial",
  "knowledge.dissection.base",
  "common.close",
  "common.loading",
  "lobby.packet_sort",
  "lobby.packet_sort_default",
  "lobby.packet_sort_year_asc",
  "lobby.packet_sort_year_desc",
  "lobby.packet_authors",
  "lobby.packet_tournaments",
  "lobby.packet_from",
  "lobby.packet_to",
  "lobby.packet_year",
  "lobby.packet_publication_year",
  "filters.name_asc",
  "filters.name_desc",
] as const;

export type KnowledgeMessageKey = (typeof knowledgeMessageKeys)[number];

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

const orderKeys: Record<KnowledgeOrder, KnowledgeMessageKey> = {
  title_asc: "knowledge.order.title_asc",
  title_desc: "knowledge.order.title_desc",
  updated_desc: "knowledge.order.updated_desc",
};

const packetOrderKeys: Record<KnowledgePacketOrder, KnowledgeMessageKey> = {
  id_asc: "lobby.packet_sort_default",
  name_asc: "filters.name_asc",
  name_desc: "filters.name_desc",
  year_asc: "lobby.packet_sort_year_asc",
  year_desc: "lobby.packet_sort_year_desc",
};

const originKeys: Record<"manual" | "dissection", KnowledgeMessageKey> = {
  manual: "knowledge.fact.origin.manual",
  dissection: "knowledge.fact.origin.dissection",
};

const dissectionStatusKeys: Record<string, KnowledgeMessageKey> = {
  queued: "knowledge.dissection.status.queued",
  running: "knowledge.dissection.status.running",
  cancelled: "knowledge.dissection.status.cancelled",
  pending: "knowledge.dissection.status.pending",
  succeeded: "knowledge.dissection.status.succeeded",
  partially_succeeded: "knowledge.dissection.status.partially_succeeded",
  failed: "knowledge.dissection.status.failed",
};

const DEFAULT_TAG_COLOUR = "#64748b";
const TAG_COLOUR_PATTERN = /^#[0-9a-f]{6}$/;

export interface KnowledgeI18n {
  t(key: KnowledgeMessageKey, parameters?: Record<string, unknown>): string;
}

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

/** Combined tag outline: one colour per tag, cycling when a card has many tags. */
function tagBorderGradient(colours: string[]): string {
  const stops = colours.length <= 1 ? colours : colours.slice(0, 6);
  return `linear-gradient(120deg, ${stops.join(", ")})`;
}

function tagChip(tag: { name: string; colour?: string }): HTMLElement {
  const colour = tag.colour && TAG_COLOUR_PATTERN.test(tag.colour) ? tag.colour : DEFAULT_TAG_COLOUR;
  const chip = element("span", { className: "knowledge-tag-chip" }, tag.name);
  chip.style.color = colour;
  return chip;
}

function openDialog(dialog: HTMLDialogElement): void {
  document.body.append(dialog);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog(dialog: HTMLDialogElement): void {
  if (typeof dialog.close === "function") dialog.close();
  dialog.remove();
}

function dialogFrame(title: string, i18n: KnowledgeI18n): { dialog: HTMLDialogElement; body: HTMLElement } {
  const dialog = element("dialog", { className: "tournament-dialog knowledge-dialog", "aria-label": title });
  const body = element("div", { className: "knowledge-dialog-body" });
  const close = element("button", {
    type: "button",
    className: "icon-button knowledge-dialog-close",
    "aria-label": i18n.t("common.close"),
    onclick: (() => { closeDialog(dialog); }) as EventListener,
  }, "×");
  dialog.append(element("div", { className: "dialog-heading" },
    element("h2", {}, title), close), body);
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  return { dialog, body };
}

interface KnowledgeState {
  tagPool: Map<string, KnowledgeTagPoolEntry>;
}

function tagColoursFor(names: string[], state: KnowledgeState, detailTags?: KnowledgeTag[]): string[] {
  const colours: string[] = [];
  for (const name of names) {
    const entry = detailTags?.find((tag) => tag.name === name);
    const colour = entry?.colour ?? state.tagPool.get(name)?.colour;
    if (colour && TAG_COLOUR_PATTERN.test(colour)) colours.push(colour);
  }
  return colours;
}

function factCard(
  fact: KnowledgeFactSummary,
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  onChanged: () => void,
  refreshTags: () => void,
): HTMLElement {
  const colours = tagColoursFor(fact.tag_names, state);
  const card = element("article", { className: "resource-card knowledge-fact", "data-fact-id": fact.id });
  if (colours.length) {
    card.style.setProperty("--knowledge-tag-border", tagBorderGradient(colours));
    card.classList.add("knowledge-fact-tagged");
  }
  const header = element("button", {
    type: "button",
    className: "knowledge-fact-header",
    "aria-expanded": "false",
    "aria-controls": `knowledge-fact-body-${fact.id}`,
  }, element("h3", { className: "knowledge-fact-title" }, fact.title),
    element("span", { className: "knowledge-fact-chevron", "aria-hidden": "true" }, "▾"));
  const body = element("div", {
    className: "knowledge-fact-body",
    id: `knowledge-fact-body-${fact.id}`,
    hidden: true,
  }, element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.loading")));
  header.addEventListener("click", () => {
    const expanded = header.getAttribute("aria-expanded") === "true";
    if (expanded) {
      header.setAttribute("aria-expanded", "false");
      body.hidden = true;
      return;
    }
    header.setAttribute("aria-expanded", "true");
    body.hidden = false;
    if (body.dataset.loaded !== "true") {
      void actions.loadFact(fact.id).then((detail) => {
        if (body.dataset.loaded === "true") return;
        body.dataset.loaded = "true";
        renderFactDetail(detail, i18n, state, actions, body, onChanged, fact.id, refreshTags);
      }).catch(() => {
        replaceChildren(body, element("p", { className: "error-text" }, i18n.t("knowledge.error")));
      });
    }
  });
  card.append(header, body);
  return card;
}

function renderFactDetail(
  detail: KnowledgeFactDetail,
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  container: HTMLElement,
  onChanged: () => void,
  factId: string,
  refreshTags: () => void,
): void {
  const links = detail.links.length
    ? element(
      "ul",
      { className: "knowledge-fact-links" },
      ...detail.links.map((link) =>
        element("li", {}, element("a", { href: link.url, target: "_blank", rel: "noreferrer" }, link.title || link.url))),
    )
    : element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.fact.links"));
  replaceChildren(container,
    element("p", { className: "knowledge-fact-description" }, detail.description),
    links,
    detail.tags.length
      ? element("p", { className: "knowledge-fact-tags" },
        ...detail.tags.map((tag) => tagChip(tag)))
      : null,
    element("p", { className: "knowledge-fact-meta" },
      `${i18n.t(originKeys[detail.origin])} · ${i18n.t("knowledge.fact.version", { version: detail.content_version })}`),
    element("p", { className: "knowledge-fact-meta" },
      statisticLines(detail.statistics, i18n).join(" · ")),
    detail.editable ? null : element("p", { className: "knowledge-readonly" }, i18n.t("knowledge.fact.read_only")),
  );
  if (detail.editable && actions.save) {
    container.append(element("div", { className: "settings-actions" },
      element("button", {
        type: "button",
        className: "primary-button",
        onclick: (() => {
          openFactDialog(i18n, state, actions, detail, () => {
            onChanged();
            void actions.loadFact(factId).then((fresh) => {
              renderFactDetail(fresh, i18n, state, actions, container, onChanged, factId, refreshTags);
            }).catch(() => { /* the changed-list reload surfaces failures */ });
          });
        }) as EventListener,
      }, i18n.t("knowledge.fact.edit"))));
    const tagTools = element("div", { className: "knowledge-tag-tools" });
    for (const tag of detail.tags) {
      tagTools.append(element("span", { className: "knowledge-tag-tool" },
        tagChip(tag),
        element("button", {
          type: "button",
          className: "secondary-button knowledge-tag-button",
          "aria-label": `${i18n.t("knowledge.tag_rename")}: ${tag.name}`,
          onclick: (() => {
            const name = window.prompt(i18n.t("knowledge.tag_rename"), tag.name);
            if (name) void actions.tag?.("tag_rename", tag.id, name)
              .then(() => { refreshTags(); onChanged(); })
              .catch(() => { /* list reload surfaces failures */ });
          }) as EventListener,
        }, "✎"),
        element("button", {
          type: "button",
          className: "secondary-button knowledge-tag-button",
          "aria-label": `${i18n.t("knowledge.tag_delete")}: ${tag.name}`,
          onclick: (() => {
            if (window.confirm(i18n.t("knowledge.tag_delete"))) {
              void actions.tag?.("tag_delete", tag.id)
                .then(() => { state.tagPool.delete(tag.name); onChanged(); })
                .catch(() => { /* list reload surfaces failures */ });
            }
          }) as EventListener,
        }, "✕")));
    }
    if (detail.tags.length) container.append(tagTools);
  }
}

function tagSelectionFieldset(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  selected: Set<string>,
  onChanged: () => void,
): HTMLElement {
  const fieldset = element("fieldset", { className: "knowledge-tag-select" });
  const list = element("div", { className: "knowledge-tag-options", role: "group",
    "aria-label": i18n.t("knowledge.tags.selected") });
  const sync = (): void => {
    replaceChildren(list);
    if (!state.tagPool.size) {
      list.append(element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.tags.field_empty")));
      return;
    }
    for (const tag of [...state.tagPool.values()].sort((a, b) => a.name.localeCompare(b.name))) {
      const input = element("input", { type: "checkbox", value: tag.name });
      input.checked = selected.has(tag.name);
      input.addEventListener("change", () => {
        if (input.checked) selected.add(tag.name);
        else selected.delete(tag.name);
      });
      list.append(element("label", { className: "checkbox-label knowledge-tag-option" }, input, tagChip(tag)));
    }
  };
  const newTagButton = element("button", {
    type: "button",
    className: "secondary-button knowledge-new-tag",
    onclick: (() => {
      openTagDialog(i18n, state, actions, (tag) => {
        selected.add(tag.name);
        sync();
        onChanged();
      });
    }) as EventListener,
  }, i18n.t("knowledge.tags.new"));
  fieldset.append(
    element("legend", {}, i18n.t("knowledge.tags.selected")),
    list,
    newTagButton,
  );
  sync();
  return fieldset;
}

function openTagDialog(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  created: (tag: KnowledgeTagPoolEntry) => void,
): void {
  if (!actions.createTag) return;
  const { dialog, body } = dialogFrame(i18n.t("knowledge.tags.new"), i18n);
  const name = element("input", { type: "text", required: true, maxlength: "60",
    "aria-label": i18n.t("knowledge.tags.name") });
  const colour = element("input", { type: "color", value: DEFAULT_TAG_COLOUR,
    "aria-label": i18n.t("knowledge.tags.colour") });
  const error = element("p", { className: "error-text", role: "alert" });
  const submit = element("button", { type: "submit", className: "primary-button" },
    i18n.t("knowledge.tags.create"));
  const form = element("form", { className: "settings-form" },
    element("label", {}, i18n.t("knowledge.tags.name"), name),
    element("label", {}, i18n.t("knowledge.tags.colour"), colour),
    submit, error);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const tagName = name.value.trim();
    if (!tagName || submit.disabled) return;
    submit.disabled = true;
    void actions.createTag?.(tagName, colour.value)
      .then((tag) => {
        state.tagPool.set(tag.name, tag);
        created(tag);
        closeDialog(dialog);
      })
      .catch(() => { error.textContent = i18n.t("knowledge.error"); })
      .finally(() => { submit.disabled = false; });
  });
  body.append(form);
  openDialog(dialog);
  name.focus();
}

function openFactDialog(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  detail: KnowledgeFactDetail | undefined,
  changed: () => void,
): void {
  const { dialog, body } = dialogFrame(
    detail ? i18n.t("knowledge.fact.edit") : i18n.t("knowledge.create"), i18n);
  body.append(factForm(i18n, state, actions, detail, changed, () => closeDialog(dialog)));
  openDialog(dialog);
}

function factForm(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  detail: KnowledgeFactDetail | undefined,
  changed: () => void,
  close: () => void,
): HTMLElement {
  const form = element("form", { className: "settings-form" });
  const title = element("input", { required: true, maxlength: "200", value: detail?.title ?? "" });
  const description = element("textarea", { required: true, maxlength: "4000" });
  description.value = detail?.description ?? "";
  const selectedTags = new Set(detail?.tags.map((tag) => tag.name) ?? []);
  const links = element("textarea");
  links.value = detail?.links.map((link) => link.url + (link.title ? ` | ${link.title}` : "")).join("\n") ?? "";
  const error = element("p", { role: "alert" });
  const save = element("button", { type: "submit", className: "primary-button" },
    i18n.t(detail ? "knowledge.save" : "knowledge.create"));
  form.append(
    element("label", {}, i18n.t("knowledge.title_field"), title),
    element("label", {}, i18n.t("knowledge.description"), description),
    tagSelectionFieldset(i18n, state, actions, selectedTags, changed),
    element("label", {}, i18n.t("knowledge.links_field"), links), save, error);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    void actions.save?.({
      title: title.value,
      description: description.value,
      tag_names: [...selectedTags],
      links: links.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
        .map((line, position) => {
          const [url = "", ...label] = line.split(" | ");
          return { url, title: label.join(" | "), position };
        }),
    }, detail).then(() => {
      close();
      changed();
    }).catch(() => { error.textContent = i18n.t("knowledge.error"); })
      .finally(() => { save.disabled = false; });
  });
  if (detail && actions.remove) {
    form.append(element("button", {
      type: "button",
      className: "danger-button",
      onclick: (() => {
        if (window.confirm(i18n.t("knowledge.confirm_delete"))) {
          void actions.remove?.(detail).then(() => { close(); changed(); })
            .catch(() => { error.textContent = i18n.t("knowledge.error"); });
        }
      }) as EventListener,
    }, i18n.t("knowledge.delete")));
  }
  if (detail && actions.merge) {
    const target = element("input", { "aria-label": i18n.t("knowledge.merge_target") });
    form.append(target, element("button", {
      type: "button",
      className: "secondary-button",
      onclick: (() => {
        if (target.value && window.confirm(i18n.t("knowledge.merge"))) {
          void actions.merge?.(detail, target.value).then(() => { close(); changed(); })
            .catch(() => { error.textContent = i18n.t("knowledge.error"); });
        }
      }) as EventListener,
    }, i18n.t("knowledge.merge")));
  }
  return form;
}

function shareLinkRow(
  link: ShareLink,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  active: boolean,
  reportError: (message: string) => void,
  refresh: () => void,
): HTMLElement {
  const resolved = actions.resolveShareUrl
    ? actions.resolveShareUrl(link.url)
    : new URL(link.url, window.location.href).toString();
  const url = element("input", {
    type: "text",
    readonly: true,
    className: "knowledge-link-url",
    value: resolved,
    "aria-label": i18n.t("knowledge.sharing.link_url"),
  });
  const copy = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => {
      void (navigator.clipboard?.writeText(resolved) ?? Promise.reject())
        .then(() => { copy.textContent = i18n.t("knowledge.sharing.copied"); })
        .catch(() => { url.select(); });
    }) as EventListener,
  }, i18n.t("knowledge.sharing.copy"));
  const row = element("li", { className: "knowledge-link-row resource-card" },
    element("div", { className: "knowledge-link-url-row" }, url, copy),
    active ? element("button", {
      type: "button",
      className: "danger-button",
      onclick: (() => {
        if (!window.confirm(i18n.t("knowledge.sharing.confirm_deactivate"))) return;
        void actions.deactivateLink?.(link.id)
          .then(refresh)
          .catch(() => reportError(i18n.t("knowledge.error")));
      }) as EventListener,
    }, i18n.t("knowledge.sharing.deactivate"))
      : element("span", { className: "knowledge-link-state" }, i18n.t("knowledge.sharing.deactivated")));
  return row;
}

function openShareDialog(
  sharing: KnowledgeSharing,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
): void {
  const { dialog, body } = dialogFrame(i18n.t("knowledge.share"), i18n);
  const error = element("p", { className: "error-text", role: "alert" });
  const report = (message: string): void => { error.textContent = message; };
  const activeLinks = element("ul", { className: "resource-list knowledge-link-list" });
  const inactiveLinks = element("ul", { className: "resource-list knowledge-link-list knowledge-link-list-inactive" });
  const inactiveHeading = element("h3", { hidden: true }, i18n.t("knowledge.sharing.links_inactive"));
  const renderLinks = (next: KnowledgeSharing): void => {
    const links = next.links ?? [];
    replaceChildren(activeLinks);
    replaceChildren(inactiveLinks);
    const active = links.filter((link) => link.active);
    const inactive = links.filter((link) => !link.active);
    if (!active.length) activeLinks.append(element("li", { className: "knowledge-fact-meta" },
      i18n.t("knowledge.sharing.empty_links")));
    for (const link of active) activeLinks.append(shareLinkRow(link, i18n, actions, true, report,
      () => { void refresh(); }));
    inactiveHeading.hidden = inactive.length === 0;
    for (const link of inactive) inactiveLinks.append(shareLinkRow(link, i18n, actions, false, report, () => {}));
  };
  const createButton = element("button", {
    type: "button",
    className: "primary-button",
    onclick: (() => {
      if (!actions.createLink || createButton.disabled) return;
      createButton.disabled = true;
      void actions.createLink()
        .then(() => refresh())
        .catch(() => report(i18n.t("knowledge.error")))
        .finally(() => { createButton.disabled = false; });
    }) as EventListener,
  }, i18n.t("knowledge.sharing.create_link"));
  const refresh = async (): Promise<void> => {
    if (!actions.loadSharing) return;
    try {
      const next = await actions.loadSharing();
      renderLinks(next);
      renderGrants(next);
    } catch { report(i18n.t("knowledge.error")); }
  };
  const grants = element("ul", { className: "resource-list" });
  const recipient = element("input", { "aria-label": i18n.t("knowledge.sharing.recipient") });
  const grantButton = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => {
      const name = recipient.value.trim();
      if (!name || !actions.grant || grantButton.disabled) return;
      grantButton.disabled = true;
      void actions.grant(name)
        .then(refresh)
        .catch(() => report(i18n.t("knowledge.error")))
        .finally(() => { grantButton.disabled = false; });
    }) as EventListener,
  }, i18n.t("knowledge.sharing.grant"));
  const received = element("ul", { className: "resource-list" });
  const renderGrants = (next: KnowledgeSharing): void => {
    replaceChildren(grants);
    replaceChildren(received);
    if (!next.grants.length) grants.append(element("li", { className: "knowledge-fact-meta" },
      i18n.t("knowledge.sharing.empty")));
    for (const grant of next.grants) {
      grants.append(element("li", { className: "resource-card knowledge-grant-row" },
        element("span", {}, grant.recipient_name),
        element("span", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.sharing.count", { count: grant.copied_count }))));
    }
    for (const item of next.shared ?? []) {
      received.append(element("li", { className: "resource-card knowledge-grant-row" },
        `${item.owner_name} · ${i18n.t("knowledge.sharing.count", { count: item.copied_count })}`));
    }
  };
  renderLinks(sharing);
  renderGrants(sharing);
  const legacy = element("details", { className: "knowledge-share-legacy" },
    element("summary", {}, i18n.t("knowledge.sharing.direct")),
    element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.sharing.permanent")),
    grants,
    element("div", { className: "knowledge-controls" },
      element("label", {}, i18n.t("knowledge.sharing.recipient"), recipient), grantButton),
    (sharing.shared ?? []).length ? element("h4", {}, i18n.t("knowledge.sharing.received")) : null,
    received);
  body.append(
    element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.sharing.link_hint")),
    element("h3", {}, i18n.t("knowledge.sharing.links")),
    activeLinks,
    createButton,
    inactiveHeading,
    inactiveLinks,
    legacy,
    error,
  );
  openDialog(dialog);
}

function openImportDialog(
  token: string,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
): void {
  const { dialog, body } = dialogFrame(i18n.t("knowledge.import.title"), i18n);
  const error = element("p", { className: "error-text", role: "alert" });
  const confirm = element("button", {
    type: "button",
    className: "primary-button",
    disabled: !actions.importLibrary,
    onclick: (() => {
      if (!actions.importLibrary || confirm.disabled) return;
      confirm.disabled = true;
      void actions.importLibrary(token)
        .then((result) => {
          replaceChildren(body, element("p", {}, i18n.t("knowledge.import.done", {
            imported: result.imported_count,
            skipped: result.skipped_count,
          })), element("button", {
            type: "button",
            className: "secondary-button",
            onclick: (() => { closeDialog(dialog); }) as EventListener,
          }, i18n.t("common.close")));
          actions.consumeImportToken?.();
        })
        .catch(() => {
          error.textContent = i18n.t("knowledge.error");
          confirm.disabled = false;
        });
    }) as EventListener,
  }, i18n.t("knowledge.import.confirm"));
  const dismiss = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => {
      actions.consumeImportToken?.();
      closeDialog(dialog);
    }) as EventListener,
  }, i18n.t("knowledge.import.dismiss"));
  body.append(element("p", {}, i18n.t("knowledge.import.body")), confirm, dismiss, error);
  openDialog(dialog);
}

function dissectDialog(
  packet: KnowledgePacketCard | { id: string; name: string },
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
): HTMLDialogElement {
  const { dialog, body } = dialogFrame(i18n.t("knowledge.dissection.title"), i18n);
  const error = element("p", { className: "error-text", role: "alert" });
  const instructions = element("textarea", {
    "aria-label": i18n.t("knowledge.dissection.instructions"),
  });
  const comment = element("textarea", { "aria-label": i18n.t("knowledge.dissection.comment") });
  const warning = element("p", { className: "knowledge-dissection-warning", hidden: true },
    i18n.t("knowledge.confirm_exposure"));
  const submit = element("button", { type: "button", className: "primary-button",
    "data-confirm": "false" }, i18n.t("knowledge.dissection.submit"));
  const run = (confirmExposure: boolean): void => {
    if (submit.disabled) return;
    submit.disabled = true;
    void actions.dissect(packet.id, instructions.value, comment.value || undefined, confirmExposure)
      .then((outcome) => {
        if (outcome.confirmation_required && !confirmExposure) {
          warning.hidden = false;
          submit.textContent = i18n.t("knowledge.dissection.confirm");
          submit.setAttribute("data-confirm", "true");
          submit.classList.add("warning-button");
          submit.disabled = false;
          return;
        }
        closeDialog(dialog);
      })
      .catch(() => { error.textContent = i18n.t("knowledge.error"); })
      .finally(() => { submit.disabled = false; });
  };
  submit.addEventListener("click", () => {
    run(submit.getAttribute("data-confirm") === "true");
  });
  body.append(
    element("h3", {}, packet.name),
    element("label", {}, i18n.t("knowledge.dissection.instructions"), instructions),
    element("label", {}, i18n.t("knowledge.dissection.comment"), comment),
    warning,
    submit,
    error,
  );
  if ("total_theme_count" in packet) {
    body.append(element("p", { className: "knowledge-fact-meta" },
      i18n.t("knowledge.packets.fresh", { fresh: packet.fresh_theme_count, total: packet.total_theme_count })));
    body.insertBefore(body.lastElementChild!, body.children[1]!);
  }
  return dialog;
}

function packetCard(
  packet: KnowledgePacketCard,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
): HTMLElement {
  const dissect = element("button", {
    type: "button",
    className: "primary-button",
    onclick: (() => {
      openDialog(dissectDialog(packet, i18n, actions));
    }) as EventListener,
  }, i18n.t("knowledge.dissection.submit_short"));
  const detail = (key: KnowledgeMessageKey, value: string): HTMLElement => element(
    "div", { className: "lobby-packet-detail" },
    element("dt", {}, i18n.t(key)), element("dd", {}, value));
  const dissected = packet.dissected_theme_count === null
    ? i18n.t("knowledge.packets.dissected_unknown")
    : i18n.t("knowledge.packets.dissected", { count: packet.dissected_theme_count });
  return element("article", {
    className: "resource-card lobby-packet-card knowledge-packet-card",
    "data-packet-id": packet.packet_id,
  },
    element("h3", {}, packet.name),
    element("span", { className: "knowledge-scope-badge" }, i18n.t(`knowledge.packets.scope.${packet.scope}` as KnowledgeMessageKey)),
    element("dl", { className: "lobby-packet-details" },
      detail("lobby.packet_year", packet.year?.toString() ?? "—"),
      detail("lobby.packet_authors", packet.authors.map((author) => author.name).join(", ") || "—"),
      detail("lobby.packet_tournaments", packet.tournaments.map((tournament) => tournament.name).join(", ") || "—"),
      detail("knowledge.packets.fresh_label",
        `${packet.fresh_theme_count} / ${packet.total_theme_count}`),
      detail("knowledge.packets.dissected_label", dissected)),
    packet.dissected ? element("span", { className: "knowledge-dissected-badge" },
      i18n.t("knowledge.packets.dissected_complete")) : null,
    element("div", { className: "settings-actions" }, dissect));
}

export function renderKnowledge(
  data: KnowledgeViewData,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
): HTMLElement {
  const root = element("div", { className: "knowledge-library" });
  const errorText = element("p", { className: "error-text", role: "alert" });
  const reportError = (message: string): void => {
    errorText.textContent = message;
  };
  const state: KnowledgeState = { tagPool: new Map() };

  const reloadTagPool = (): void => {
    if (!actions.loadTags) return;
    void actions.loadTags().then((pool) => {
      if (actions.signal?.aborted) return;
      state.tagPool = new Map(pool.items.map((tag) => [tag.name, tag]));
      refreshTagFilterOptions();
      applyTagOutlines();
    }).catch(() => { /* the pool is retried on the next page visit */ });
  };

  let search = data.facts.search ?? "";
  let tagFilter = "";
  let order = data.facts.order;

  // Tabs
  const libraryPanel = element("section", { className: "knowledge-panel", "data-panel": "library" });
  const packetsPanel = element("section", { className: "knowledge-panel", "data-panel": "packets", hidden: true });
  const tabButton = (tab: KnowledgeTab, label: string): HTMLButtonElement => {
    const button = element("button", { type: "button", className: "secondary-button knowledge-tab" },
      label);
    button.addEventListener("click", () => setTab(tab));
    return button;
  };
  const libraryTab = tabButton("library", i18n.t("knowledge.tab.library"));
  const packetsTab = tabButton("packets", i18n.t("knowledge.tab.packets"));
  const tabs = element("nav", { className: "settings-actions knowledge-tabs", role: "tablist",
    "aria-label": i18n.t("knowledge.title") }, libraryTab, packetsTab);

  // Library list
  const list = element("ul", { className: "resource-list knowledge-list" });
  const total = element("p", { className: "knowledge-total" },
    i18n.t("knowledge.total", { total: data.facts.total }));
  const renderList = (): void => {
    replaceChildren(list);
    for (const fact of data.facts.items) {
      list.append(element("li", {}, factCard(fact, i18n, state, actions, reload, reloadTagPool)));
    }
    if (!data.facts.items.length) {
      list.append(element("li", { className: "knowledge-empty" }, i18n.t("knowledge.empty")));
    }
    applyTagOutlines();
  };
  /** Restyles existing cards in place so expansions survive the tag-pool read. */
  const applyTagOutlines = (): void => {
    const cards = list.querySelectorAll<HTMLElement>("[data-fact-id]");
    for (const card of cards) {
      const fact = data.facts.items.find((item) => item.id === card.getAttribute("data-fact-id"));
      if (!fact) continue;
      const colours = tagColoursFor(fact.tag_names, state);
      if (colours.length) {
        card.style.setProperty("--knowledge-tag-border", tagBorderGradient(colours));
        card.classList.add("knowledge-fact-tagged");
      } else {
        card.style.removeProperty("--knowledge-tag-border");
        card.classList.remove("knowledge-fact-tagged");
      }
    }
  };
  const appendItems = (items: KnowledgeFactSummary[]): void => {
    for (const fact of items) {
      list.append(element("li", {}, factCard(fact, i18n, state, actions, reload, reloadTagPool)));
    }
  };

  const loadMore = element("button", {
    type: "button",
    className: "secondary-button knowledge-load-more",
  }, i18n.t("knowledge.load_more"));
  loadMore.addEventListener("click", () => {
    if (!data.facts.next_cursor || loadMore.disabled) return;
    loadMore.disabled = true;
    void actions.loadFacts({ cursor: data.facts.next_cursor, search, order,
      ...(tagFilter ? { tag: tagFilter } : {}) })
      .then((page) => {
        appendItems(page.items);
        data.facts.next_cursor = page.next_cursor;
        if (page.next_cursor === null) loadMore.remove();
      })
      .catch(() => reportError(i18n.t("knowledge.error")))
      .finally(() => { loadMore.disabled = false; });
  });

  const reload = (): void => {
    if (searchInput.disabled || orderSelect.disabled) return;
    searchInput.disabled = true;
    orderSelect.disabled = true;
    void actions.loadFacts({ search, order,
      ...(tagFilter ? { tag: tagFilter } : {}) })
      .then((page) => {
        data.facts.items = page.items;
        data.facts.next_cursor = page.next_cursor;
        data.facts.total = page.total;
        renderList();
        total.textContent = i18n.t("knowledge.total", { total: page.total });
        loadMore.hidden = page.next_cursor === null;
        if (page.next_cursor !== null && !loadMore.isConnected) pagination.append(loadMore);
      })
      .catch((error: unknown) => {
        reportError(
          (error as { quota?: boolean })?.quota ? i18n.t("knowledge.quota") : i18n.t("knowledge.error"),
        );
      })
      .finally(() => {
        searchInput.disabled = false;
        orderSelect.disabled = false;
      });
  };

  const searchInput = element("input", {
    type: "search",
    value: search,
    "aria-label": i18n.t("knowledge.search"),
  });
  const orderSelect = element("select", { "aria-label": i18n.t("knowledge.order.title_asc") },
    ...knowledgeOrders.map((value) =>
      element("option", { value }, i18n.t(orderKeys[value]))));
  orderSelect.value = order;
  const tagFilterSelect = element("select", { "aria-label": i18n.t("knowledge.tag_filter") },
    element("option", { value: "" }, i18n.t("knowledge.tag_filter_all")));
  const refreshTagFilterOptions = (): void => {
    const current = tagFilterSelect.value;
    while (tagFilterSelect.options.length > 1) tagFilterSelect.options.remove(1);
    for (const tag of [...state.tagPool.values()].sort((a, b) => a.name.localeCompare(b.name))) {
      tagFilterSelect.append(element("option", { value: tag.name }, tag.name));
    }
    tagFilterSelect.value = current;
  };
  tagFilterSelect.addEventListener("change", () => {
    tagFilter = tagFilterSelect.value;
    reload();
  });

  searchInput.addEventListener("input", () => { search = searchInput.value; });
  searchInput.addEventListener("change", reload);
  orderSelect.addEventListener("change", () => {
    order = orderSelect.value as KnowledgeOrder;
    reload();
  });
  root.addEventListener("knowledge:changed", reload);

  const pagination = element("div", { className: "knowledge-pagination" });
  if (data.facts.next_cursor !== null) pagination.append(loadMore);

  const shareButton = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => {
      if (actions.loadSharing) {
        void actions.loadSharing().then((sharing) => {
          openShareDialog(sharing, i18n, actions);
        }).catch(() => reportError(i18n.t("knowledge.error")));
      } else {
        openShareDialog(data.sharing, i18n, actions);
      }
    }) as EventListener,
  }, i18n.t("knowledge.share"));

  const addButton = actions.save
    ? element("button", {
      type: "button",
      className: "primary-button",
      onclick: (() => { openFactDialog(i18n, state, actions, undefined, reload); }) as EventListener,
    }, i18n.t("knowledge.create"))
    : null;

  libraryPanel.append(
    element("div", { className: "knowledge-controls", role: "search" },
      searchInput, orderSelect, tagFilterSelect,
      element("button", {
        type: "button", className: "secondary-button",
        onclick: reload as EventListener,
      }, i18n.t("knowledge.refresh"))),
    element("div", { className: "settings-actions knowledge-library-actions" },
      shareButton, addButton),
    total,
    list,
    pagination,
  );

  // Packets tab
  const packetList = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const packetLoading = element("p", { className: "knowledge-fact-meta", role: "status", hidden: true },
    i18n.t("knowledge.packets.loading"));
  const packetFilters: Record<string, string> = {};
  const packetInput = (name: string, label: KnowledgeMessageKey, numeric = false): HTMLElement =>
    element("label", {}, i18n.t(label), element("input", {
      name, type: numeric ? "number" : "search", value: "",
      "aria-label": i18n.t(label),
      maxlength: numeric ? undefined : "200",
      min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
      oninput: ((event: Event) => {
        packetFilters[name] = (event.currentTarget as HTMLInputElement).value;
        if (packetsActive && loader) loader.start({ ...packetFilters }, true);
      }) as EventListener,
    }));
  const orderSelectPackets = element("select", { "aria-label": i18n.t("lobby.packet_sort") },
    ...knowledgePacketOrders.map((value) =>
      element("option", { value }, i18n.t(packetOrderKeys[value]))));
  orderSelectPackets.value = "id_asc";
  orderSelectPackets.addEventListener("change", () => {
    packetFilters.order = orderSelectPackets.value;
    if (packetsActive && loader) loader.start({ ...packetFilters }, true);
  });
  const resetButton = element("button", {
    type: "button", className: "secondary-button",
    onclick: (() => {
      for (const field of packetsControls.querySelectorAll("input")) field.value = "";
      for (const key of Object.keys(packetFilters)) delete packetFilters[key];
      orderSelectPackets.value = "id_asc";
      if (packetsActive && loader) loader.start({}, true);
    }) as EventListener,
  }, i18n.t("knowledge.packets.reset"));
  const packetsControls = element("div", { className: "knowledge-controls", role: "search" },
    packetInput("name", "knowledge.packets.search"),
    packetInput("author", "lobby.packet_authors"),
    orderSelectPackets,
    element("fieldset", { className: "lobby-packet-year-range" },
      element("legend", {}, i18n.t("lobby.packet_year")),
      packetInput("year_from", "lobby.packet_from", true),
      packetInput("year_to", "lobby.packet_to", true)),
    element("fieldset", { className: "lobby-packet-year-range" },
      element("legend", {}, i18n.t("lobby.packet_publication_year")),
      packetInput("publication_from", "lobby.packet_from", true),
      packetInput("publication_to", "lobby.packet_to", true)),
    resetButton);

  const jobs = element("ul", { className: "resource-list knowledge-jobs" });
  const renderJobs = (requests: KnowledgeDissection["requests"]): void => {
    replaceChildren(jobs);
    if (!requests.length) {
      jobs.append(element("li", { className: "knowledge-fact-meta" }, i18n.t("knowledge.dissection.empty")));
      return;
    }
    for (const request of requests) {
      const packetName = packetNames.get(request.packet_id) ?? request.packet_id;
      jobs.append(element("li", { className: "resource-card knowledge-job" },
        element("span", {}, packetName),
        element("span", { className: "knowledge-dissection-status" },
          i18n.t(dissectionStatusKeys[request.status] ?? "knowledge.dissection.status.pending")),
        request.base ? element("span", {}, i18n.t("knowledge.dissection.base")) : null,
        element("span", {}, `${request.applied_count}`),
        request.partial ? element("span", { className: "error-text" },
          i18n.t("knowledge.dissection.partial")) : null,
        ["queued", "running", "pending"].includes(request.status) && request.request_id && actions.cancel
          ? element("button", {
            type: "button", className: "secondary-button",
            onclick: (() => {
              void actions.cancel?.(request.request_id ?? "")
                .catch(() => reportError(i18n.t("knowledge.error")));
            }) as EventListener,
          }, i18n.t("knowledge.cancel"))
          : null));
    }
  };
  const packetNames = new Map<string, string>();
  for (const packet of data.dissection.packets) packetNames.set(packet.id, packet.name);
  renderJobs(data.dissection.requests);

  let jobsTimer: number | undefined;
  const refreshJobs = async (): Promise<void> => {
    if (!actions.loadDissections || !packetsActive) return;
    try {
      const items = await actions.loadDissections() ?? [];
      if (!packetsActive || actions.signal?.aborted) return;
      renderJobs(items);
    } catch { /* Keep the current job projection visible until the next read. */ }
  };
  const startJobs = (): void => {
    if (jobsTimer !== undefined || !actions.loadDissections) return;
    jobsTimer = window.setTimeout(function tick(): void {
      jobsTimer = undefined;
      void refreshJobs().finally(() => {
        if (packetsActive) jobsTimer = window.setTimeout(tick, 3000);
      });
    }, 3000);
  };
  const stopJobs = (): void => {
    if (jobsTimer !== undefined) window.clearTimeout(jobsTimer);
    jobsTimer = undefined;
  };
  actions.signal?.addEventListener("abort", () => { stopJobs(); loader?.stop(); }, { once: true });

  const renderPackets = (items: KnowledgePacketCard[], loading: boolean): void => {
    packetLoading.hidden = !loading;
    packetList.setAttribute("aria-busy", String(loading));
    for (const packet of items) packetNames.set(packet.id, packet.name);
    replaceChildren(packetList, ...items.map((packet) => packetCard(packet, i18n, actions)));
    if (!items.length && !loading) {
      packetList.append(element("p", {}, i18n.t(
        Object.values(packetFilters).some((value) => value) ? "knowledge.packets.no_matches" : "knowledge.packets.empty",
      )));
    }
  };

  const loader = actions.loadPackets && actions.signal
    ? new PacketLoader<KnowledgePacketCard>(
      actions.signal,
      (query, signal) => actions.loadPackets!(query, signal),
      renderPackets,
      () => {
        reportError(i18n.t("knowledge.error"));
        return true;
      },
    )
    : undefined;

  packetsPanel.append(
    packetsControls,
    packetLoading,
    packetList,
    element("section", { className: "knowledge-jobs-section" },
      element("h3", {}, i18n.t("knowledge.packets.jobs")),
      jobs),
  );

  let packetsActive = false;
  const setTab = (tab: KnowledgeTab): void => {
    const target = tab === "library";
    libraryPanel.hidden = !target;
    packetsPanel.hidden = target;
    libraryTab.className = target ? "primary-button knowledge-tab" : "secondary-button knowledge-tab";
    packetsTab.className = target ? "secondary-button knowledge-tab" : "primary-button knowledge-tab";
    if (target) {
      libraryTab.setAttribute("aria-current", "page");
      packetsTab.removeAttribute("aria-current");
    } else {
      packetsTab.setAttribute("aria-current", "page");
      libraryTab.removeAttribute("aria-current");
    }
    const nowPacketsActive = tab === "packets";
    if (nowPacketsActive && !packetsActive) {
      packetsActive = true;
      loader?.start({ ...packetFilters });
      startJobs();
    } else if (!nowPacketsActive && packetsActive) {
      packetsActive = false;
      loader?.stop();
      stopJobs();
    }
  };
  libraryTab.setAttribute("aria-current", "page");

  renderList();
  reloadTagPool();
  if (data.import_token && actions.importLibrary) openImportDialog(data.import_token, i18n, actions);

  root.append(
    element("h2", { className: "knowledge-title" }, i18n.t("knowledge.title")),
    tabs,
    errorText,
    libraryPanel,
    packetsPanel,
  );
  return root;
}
