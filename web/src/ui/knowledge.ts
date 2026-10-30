import { element, replaceChildren } from "./dom";
import { icon } from "./icons";
import { dissectComposerDialog } from "./knowledge-dissect";
import { createImportsPanel } from "./knowledge-imports";
import { PacketLoader } from "./packet-loading";
import {
  DEFAULT_TAG_COLOUR,
  TAG_COLOUR_PATTERN,
  closeDialog,
  dialogFrame,
  openDialog,
  tagBorderGradient,
  tagChip,
  type KnowledgeState,
} from "./knowledge-shared";

export type { KnowledgeState };

export type KnowledgeOrder = "title_asc" | "title_desc" | "updated_desc";

export const knowledgeOrders: readonly KnowledgeOrder[] = ["title_asc", "title_desc", "updated_desc"];

export type KnowledgePacketOrder = "id_asc" | "name_asc" | "name_desc" | "year_asc" | "year_desc";

export const knowledgePacketOrders: readonly KnowledgePacketOrder[] = [
  "id_asc", "name_asc", "name_desc", "year_asc", "year_desc",
];

export type KnowledgeTab = "library" | "packets" | "imports";

export const knowledgeTabs: readonly KnowledgeTab[] = ["library", "packets", "imports"];

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

/** Tag as returned by the tag pool contract (tag_id + name + colour + display label). */
export interface KnowledgeTagPoolEntry {
  tag_id: string;
  name: string;
  /** Requested display capitalization; falls back to the canonical name. */
  label?: string;
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
  /** Fact author per the facts contract; empty/absent means unknown. */
  author?: string;
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

/** Image attached to a fact via the authenticated image upload contract. */
export interface KnowledgeFactImage {
  id: string;
  url: string;
  content_type: string;
}

export interface KnowledgeFactDetail {
  id: string;
  title: string;
  description: string;
  links: Array<{ url: string; title: string }>;
  tags: KnowledgeTag[];
  origin: "manual" | "dissection";
  /** Fact author per the facts contract; empty/absent means unknown. */
  author?: string;
  /** Internal packet provenance; never displayed on fact cards. */
  packet_name?: string | null;
  content_version: number;
  editable: boolean;
  images?: KnowledgeFactImage[];
  statistics: KnowledgeStatistics;
}

export interface ShareLink {
  id: string;
  token: string;
  /** Shareable Telegram bot /start URL; null when the launch identity is unavailable. */
  url: string | null;
  /** In-session fragment route only; never presented as an externally usable share link. */
  app_url: string;
  launch_available: boolean;
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
  /** Staged review identifier; present since imports became pending. */
  import_id?: string;
  /** "pending" when the import was staged for review instead of applied. */
  status?: string;
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

/** Dissection composer request per the knowledge LLM library contract. */
export interface KnowledgeDissectRequest {
  instructions?: string;
  comment?: string;
  theme_revision_ids?: string[];
  tag_ids?: string[];
  confirm_exposure?: boolean;
}

export interface KnowledgeDissectionQuota {
  enabled: boolean;
  remaining: number | null;
  reset_at: string | null;
}

export interface KnowledgeThemeSummary {
  theme_revision_id: string;
  name: string;
}

export interface KnowledgeThemesResource {
  version_id: string;
  packet_name: string;
  themes: KnowledgeThemeSummary[];
  quota: KnowledgeDissectionQuota;
}

export interface KnowledgeThemeQuestion {
  revision_id: string;
  value: number;
  text: string;
  answer: string;
  accepted_answers: string[];
  rejected_answers: string[];
  commentary: string | null;
}

export interface KnowledgeThemeDetail {
  theme_revision_id: string;
  name: string;
  author: string;
  commentary: string | null;
  questions: KnowledgeThemeQuestion[];
}

export type KnowledgeThemePreview =
  | { confirmation_required: true; fresh_unit_count: number }
  | { confirmation_required: false; theme: KnowledgeThemeDetail };

export interface KnowledgeImportCard {
  id: string;
  type: "dissection" | "collection";
  author: string;
  status: "working" | "pending" | "approved" | "rejected" | "failed";
  fact_count: number;
  packet_name: string | null;
  created_at: string;
  request_id: string | null;
}

export interface KnowledgeImportsResource {
  items: KnowledgeImportCard[];
  next_cursor: string | null;
}

export interface KnowledgeImportCandidate {
  id: string;
  title: string;
  description: string;
  author: string;
  links: Array<{ url: string; title: string }>;
  images: KnowledgeFactImage[];
  tag_names: string[];
  selected: boolean;
}

export interface KnowledgeImportDetail extends KnowledgeImportCard {
  sections: {
    new: KnowledgeImportCandidate[];
    potential_duplicates: KnowledgeImportCandidate[];
    duplicates: KnowledgeImportCandidate[];
  };
  approved_count: number;
}

export interface KnowledgeImportDecision {
  import_id: string;
  status: string;
  approved_count: number;
  skipped_count: number;
}

export interface KnowledgeActions {
  signal?: AbortSignal;
  loadFacts: (params: {
    owner_id?: string;
    tags?: string[];
    cursor?: string | null;
    search?: string;
    author?: string;
    order?: KnowledgeOrder;
  }) => Promise<KnowledgeFactsResource>;
  loadFact: (factId: string) => Promise<KnowledgeFactDetail>;
  save?: (values: KnowledgeFactInput, detail?: KnowledgeFactDetail) => Promise<KnowledgeFactDetail>;
  remove?: (detail: KnowledgeFactDetail) => Promise<void>;
  uploadImage?: (sourceBase64: string) => Promise<KnowledgeFactImage>;
  tag?: (command: "tag_rename" | "tag_delete", tagId: string, name?: string, colour?: string) => Promise<void>;
  loadTags?: () => Promise<KnowledgeTagPool>;
  createTag?: (name: string, colour: string) => Promise<KnowledgeTagPoolEntry>;
  loadSharing?: () => Promise<KnowledgeSharing>;
  grant?: (recipient: string) => Promise<void>;
  createLink?: () => Promise<ShareLink>;
  deactivateLink?: (linkId: string) => Promise<ShareLink | void>;
  importLibrary?: (token: string) => Promise<KnowledgeImportResult>;
  consumeImportToken?: () => void;
  loadPackets?: (query: URLSearchParams, signal: AbortSignal) => Promise<{
    items: KnowledgePacketCard[];
    next_cursor: string | null;
  }>;
  dissect: (versionId: string, request: KnowledgeDissectRequest)
    => Promise<KnowledgeDissectOutcome>;
  loadDissections?: () => Promise<KnowledgeDissection["requests"]>;
  cancel?: (requestId: string) => Promise<void>;
  /** Dissection composer: theme metadata (no content, no exposure). */
  loadThemes?: (versionId: string) => Promise<KnowledgeThemesResource>;
  /** Theme preview mutation: reveals one theme after explicit exposure confirmation. */
  previewTheme?: (
    versionId: string, themeRevisionId: string, confirmExposure: boolean,
  ) => Promise<KnowledgeThemePreview>;
  /** Pending imports listing (dissections and incoming collections). */
  loadImports?: (cursor?: string | null) => Promise<KnowledgeImportsResource>;
  loadImportDetail?: (importId: string) => Promise<KnowledgeImportDetail>;
  decideImport?: (
    importId: string,
    decision: "approve" | "reject",
    selectedCandidateIds?: string[],
  ) => Promise<KnowledgeImportDecision>;
}

export interface KnowledgeFactInput {
  title: string;
  description: string;
  tag_names: string[];
  links: Array<{ url: string; title?: string; position: number }>;
  image_ids?: string[];
  author?: string;
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
  "knowledge.fact.origin.packet",
  "knowledge.fact.read_only",
  "knowledge.fact.edit",
  "knowledge.fact.edit_label",
  "knowledge.fact.delete_label",
  "knowledge.images_field",
  "knowledge.images.remove",
  "knowledge.images.too_large",
  "knowledge.images.unsupported",
  "knowledge.images.too_many",
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
  "knowledge.tab.imports",
  "knowledge.author_search",
  "knowledge.fact.author",
  "knowledge.fact.author_field",
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
  "knowledge.sharing.launch_unavailable",
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
  "knowledge.packets.owner_only",
  "knowledge.packets.jobs",
  "knowledge.dissection.title",
  "knowledge.dissection.packet",
  "knowledge.dissection.instructions",
  "knowledge.dissection.submit",
  "knowledge.dissection.submit_short",
  "knowledge.dissection.confirm",
  "knowledge.dissection.confirm_theme",
  "knowledge.dissection.tab.general",
  "knowledge.dissection.tab.themes",
  "knowledge.dissection.selected_count",
  "knowledge.dissection.themes_hint",
  "knowledge.dissection.select_all",
  "knowledge.dissection.deselect_all",
  "knowledge.dissection.quota",
  "knowledge.dissection.quota_unlimited",
  "knowledge.dissection.quota_reset",
  "knowledge.dissection.themes_empty",
  "knowledge.dissection.themes_loading",
  "knowledge.dissection.theme_author",
  "knowledge.dissection.exposure_units",
  "knowledge.imports.empty",
  "knowledge.imports.loading",
  "knowledge.imports.type.dissection",
  "knowledge.imports.type.collection",
  "knowledge.imports.status.working",
  "knowledge.imports.status.pending",
  "knowledge.imports.status.approved",
  "knowledge.imports.status.rejected",
  "knowledge.imports.status.failed",
  "knowledge.imports.author",
  "knowledge.imports.fact_count",
  "knowledge.imports.source_packet",
  "knowledge.imports.created",
  "knowledge.imports.review",
  "knowledge.imports.review_title",
  "knowledge.imports.section.new",
  "knowledge.imports.section.potential_duplicates",
  "knowledge.imports.section.duplicates",
  "knowledge.imports.select_all",
  "knowledge.imports.deselect_all",
  "knowledge.imports.add_selected",
  "knowledge.imports.reject",
  "knowledge.imports.reject_confirm",
  "knowledge.imports.approved_note",
  "knowledge.imports.rejected_note",
  "knowledge.import.staged",
  "training_history.value",
  "training_history.answer",
  "training_history.accepted",
  "training_history.rejected",
  "training_history.commentary",
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

const dissectionStatusKeys: Record<string, KnowledgeMessageKey> = {
  queued: "knowledge.dissection.status.queued",
  running: "knowledge.dissection.status.running",
  cancelled: "knowledge.dissection.status.cancelled",
  pending: "knowledge.dissection.status.pending",
  succeeded: "knowledge.dissection.status.succeeded",
  partially_succeeded: "knowledge.dissection.status.partially_succeeded",
  failed: "knowledge.dissection.status.failed",
};

/** Fact image attachment limits per the image upload contract. */
const MAX_FACT_IMAGES = 8;
const IMAGE_MAX_BYTES = 2 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

/** Raw bytes of the picked file via FileReader (works in browsers and jsdom). */
function fileBytes(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => { resolve(reader.result as ArrayBuffer); };
    reader.onerror = () => { reject(reader.error ?? new Error("read failed")); };
    reader.readAsArrayBuffer(file);
  });
}

/** RFC 4648 base64 of the raw bytes, as required by image_upload. */
async function fileBase64(file: File): Promise<string> {
  const buffer = await fileBytes(file);
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

export interface KnowledgeI18n {
  t(key: KnowledgeMessageKey, parameters?: Record<string, unknown>): string;
  /** Locale for date formatting; optional in test harnesses. */
  readonly locale?: "en" | "ru" | string;
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
  refreshTags: (propagateError?: boolean) => Promise<void>,
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
  let loaded: KnowledgeFactDetail | undefined;
  // Compact controls at the card's bottom right; visible once an editable detail is loaded.
  const editButton = element("button", {
    type: "button",
    className: "icon-button knowledge-fact-edit",
    "aria-label": i18n.t("knowledge.fact.edit_label", { title: fact.title }),
    title: i18n.t("knowledge.fact.edit"),
    hidden: true,
  }, "✎");
  const deleteButton = element("button", {
    type: "button",
    className: "icon-button knowledge-fact-delete",
    "aria-label": i18n.t("knowledge.fact.delete_label", { title: fact.title }),
    title: i18n.t("knowledge.delete"),
    hidden: true,
  }, icon("trash"));
  const deleteError = element("p", { className: "error-text knowledge-fact-delete-error", role: "alert", hidden: true });
  const controls = element("div", { className: "knowledge-fact-actions", hidden: true },
    deleteError, deleteButton, editButton);
  const syncControls = (): void => {
    editButton.hidden = !(loaded?.editable && actions.save !== undefined);
    deleteButton.hidden = !(loaded?.editable && actions.remove !== undefined);
    controls.hidden = editButton.hidden && deleteButton.hidden;
  };
  const renderDetail = (detail: KnowledgeFactDetail): void => {
    loaded = detail;
    renderFactDetail(detail, i18n, body, syncControls);
  };
  editButton.addEventListener("click", () => {
    if (!loaded) return;
    openFactDialog(i18n, state, actions, loaded, () => {
      onChanged();
      void actions.loadFact(fact.id).then(renderDetail)
        .catch(() => { /* the changed-list reload surfaces failures */ });
    }, refreshTags);
  });
  // Deletion from the card reuses the dialog's authorized remove contract:
  // one confirmation, busy lock while in flight, honest failure reporting.
  deleteButton.addEventListener("click", () => {
    if (!loaded || deleteButton.disabled) return;
    if (!window.confirm(i18n.t("knowledge.confirm_delete"))) return;
    deleteButton.disabled = true;
    editButton.disabled = true;
    deleteError.hidden = true;
    void actions.remove?.(loaded).then(() => {
      onChanged();
    }).catch(() => {
      deleteButton.disabled = false;
      editButton.disabled = false;
      deleteError.hidden = false;
      deleteError.textContent = i18n.t("knowledge.error");
    });
  });
  header.addEventListener("click", () => {
    const expanded = header.getAttribute("aria-expanded") === "true";
    if (expanded) {
      header.setAttribute("aria-expanded", "false");
      body.hidden = true;
      return;
    }
    header.setAttribute("aria-expanded", "true");
    body.hidden = false;
    if (body.dataset.loaded !== "true" && body.dataset.loading !== "true") {
      body.dataset.loading = "true";
      void actions.loadFact(fact.id).then((detail) => {
        if (body.dataset.loaded === "true") return;
        body.dataset.loaded = "true";
        renderDetail(detail);
      }).catch(() => {
        replaceChildren(body, element("p", { className: "error-text" }, i18n.t("knowledge.error")));
      }).finally(() => { delete body.dataset.loading; });
    }
  });
  card.append(element("div", { className: "knowledge-fact-head-row" }, header), body, controls);
  return card;
}

function renderFactDetail(
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
  // Tag and fact editing happens in the editor dialog, never inline on the card.
  syncControls();
}

function tagSelectionFieldset(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  selected: Set<string>,
  onChanged: () => void,
  refreshTags: (propagateError?: boolean) => Promise<void>,
): { root: HTMLElement; ready: () => Promise<void> } {
  const fieldset = element("fieldset", { className: "knowledge-tag-select" });
  let pendingTags = state.tagPoolReady;
  const mutateTag = actions.tag;
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
      // Tag pool maintenance lives here in the editor, never on the fact card.
      const option = element("div", { className: "knowledge-tag-option" },
        element("label", { className: "checkbox-label" }, input, tagChip(tag)));
      if (mutateTag && actions.loadTags) {
        option.append(
          element("button", {
            type: "button",
            className: "secondary-button knowledge-tag-button",
            "aria-label": `${i18n.t("knowledge.tag_rename")}: ${tag.name}`,
            onclick: (() => {
              const name = window.prompt(i18n.t("knowledge.tag_rename"), tag.name);
              if (!name) return;
              fieldset.disabled = true;
              pendingTags = mutateTag("tag_rename", tag.tag_id, name)
                .then(async () => {
                  await refreshTags(true);
                  const canonical = [...state.tagPool.values()].find((entry) => entry.tag_id === tag.tag_id);
                  if (selected.delete(tag.name) && canonical) selected.add(canonical.name);
                  onChanged();
                  sync();
                })
                .finally(() => { fieldset.disabled = false; });
              void pendingTags.catch(() => { onChanged(); });
            }) as EventListener,
          }, "✎"),
          element("button", {
            type: "button",
            className: "secondary-button knowledge-tag-button",
            "aria-label": `${i18n.t("knowledge.tag_delete")}: ${tag.name}`,
            onclick: (() => {
              if (!window.confirm(i18n.t("knowledge.tag_delete"))) return;
              fieldset.disabled = true;
              pendingTags = mutateTag("tag_delete", tag.tag_id)
                .then(async () => {
                  await refreshTags(true);
                  selected.delete(tag.name);
                  onChanged();
                  sync();
                })
                .finally(() => { fieldset.disabled = false; });
              void pendingTags.catch(() => { onChanged(); });
            }) as EventListener,
          }, "✕"));
      }
      list.append(option);
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
  pendingTags = pendingTags.then(sync);
  void pendingTags.catch(() => { onChanged(); });
  return { root: fieldset, ready: () => pendingTags };
}

function openTagDialog(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  created: (tag: KnowledgeTagPoolEntry) => void,
): void {
  const createTag = actions.createTag;
  if (!createTag) return;
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
    void state.tagPoolReady.then(() => createTag(tagName, colour.value))
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
  refreshTags: (propagateError?: boolean) => Promise<void>,
): void {
  const { dialog, body } = dialogFrame(
    detail ? i18n.t("knowledge.fact.edit") : i18n.t("knowledge.create"), i18n);
  body.append(factForm(i18n, state, actions, detail, changed, () => closeDialog(dialog), refreshTags));
  openDialog(dialog);
}

function factForm(
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  detail: KnowledgeFactDetail | undefined,
  changed: () => void,
  close: () => void,
  refreshTags: (propagateError?: boolean) => Promise<void>,
): HTMLElement {
  const form = element("form", { className: "settings-form" });
  const title = element("input", { required: true, maxlength: "200", value: detail?.title ?? "" });
  const description = element("textarea", { required: true, maxlength: "4000" });
  description.value = detail?.description ?? "";
  // Author is snapshotted and independently editable per the facts contract;
  // leaving it empty on create falls back to the creator's nickname.
  const author = element("input", { maxlength: "300", value: detail?.author ?? "",
    "aria-label": i18n.t("knowledge.fact.author_field") });
  const selectedTags = new Set(detail?.tags.map((tag) => tag.name) ?? []);
  const links = element("textarea");
  links.value = detail?.links.map((link) => link.url + (link.title ? ` | ${link.title}` : "")).join("\n") ?? "";
  const error = element("p", { role: "alert" });
  const save = element("button", { type: "submit", className: "primary-button" },
    i18n.t(detail ? "knowledge.save" : "knowledge.create"));
  const imagesField = factImagesField(i18n, actions, detail, error);
  const tagsField = tagSelectionFieldset(i18n, state, actions, selectedTags, changed, refreshTags);
  const fields = element("fieldset", { className: "knowledge-fact-fields" });
  form.append(fields);
  fields.append(
    element("label", {}, i18n.t("knowledge.title_field"), title),
    element("label", {}, i18n.t("knowledge.description"), description),
    element("label", {}, i18n.t("knowledge.fact.author_field"), author),
    tagsField.root,
    element("label", {}, i18n.t("knowledge.links_field"), links),
    imagesField.root, save, error);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (save.disabled) return;
    save.disabled = true;
    fields.disabled = true;
    void Promise.all([imagesField.ready(), tagsField.ready()]).then(() => actions.save?.({
      title: title.value,
      description: description.value,
      // An empty author clears on update; creation omits it entirely so the
      // server can apply the creator's public nickname.
      author: author.value.trim() || (detail ? "" : undefined),
      tag_names: [...selectedTags],
      links: links.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
        .map((line, position) => {
          const [url = "", ...label] = line.split(" | ");
          return { url, title: label.join(" | "), position };
        }),
      // The complete desired attachment set: kept existing ids plus new uploads.
      image_ids: imagesField.imageIds(),
    }, detail)).then(() => {
      close();
      changed();
    }).catch(() => { error.textContent = i18n.t("knowledge.error"); })
      .finally(() => { save.disabled = false; fields.disabled = false; });
  });
  if (detail && actions.remove) {
    fields.append(element("button", {
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
  return form;
}

/**
 * The Images field of the fact editor: authenticated bounded uploads plus
 * kept existing attachments. The fact save always carries the complete id set.
 */
function factImagesField(
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  detail: KnowledgeFactDetail | undefined,
  error: HTMLElement,
): { root: HTMLElement; imageIds: () => string[]; ready: () => Promise<void> } {
  const kept: KnowledgeFactImage[] = [...(detail?.images ?? [])];
  let queuedUploads = 0;
  let pendingUploads = Promise.resolve();
  const list = element("div", { className: "knowledge-images" });
  const upload = actions.uploadImage;
  const picker = element("input", {
    type: "file",
    multiple: "",
    accept: "image/png,image/jpeg,image/webp",
    "aria-label": i18n.t("knowledge.images_field"),
  });
  if (!upload) picker.disabled = true;
  const report = (key: KnowledgeMessageKey): void => {
    error.textContent = i18n.t(key);
  };
  const render = (): void => {
    replaceChildren(list);
    for (const [index, image] of kept.entries()) {
      const remove = element("button", {
        type: "button",
        className: "secondary-button knowledge-image-remove",
        "aria-label": `${i18n.t("knowledge.images.remove")} ${index + 1}`,
        onclick: (() => {
          kept.splice(index, 1);
          render();
        }) as EventListener,
      }, "✕");
      list.append(element("div", { className: "knowledge-image" },
        element("img", { src: image.url, alt: i18n.t("knowledge.images_field") }),
        remove));
    }
    if (upload) picker.disabled = queuedUploads > 0 || kept.length >= MAX_FACT_IMAGES;
  };
  picker.addEventListener("change", () => {
    const files = [...(picker.files ?? [])].filter((file) => {
      if (!IMAGE_TYPES.has(file.type)) {
        report("knowledge.images.unsupported");
        return false;
      }
      if (file.size > IMAGE_MAX_BYTES) {
        report("knowledge.images.too_large");
        return false;
      }
      return true;
    });
    picker.value = "";
    if (!upload || !files.length) return;
    ++queuedUploads;
    render();
    pendingUploads = pendingUploads.catch(() => {}).then(async (): Promise<void> => {
      for (const file of files) {
        if (kept.length >= MAX_FACT_IMAGES) {
          report("knowledge.images.too_many");
          break;
        }
        try {
          const image = await upload(await fileBase64(file));
          kept.push(image);
          render();
        } catch (failure) {
          report("knowledge.error");
          throw failure;
        }
      }
    }).finally(() => {
      --queuedUploads;
      render();
    });
    void pendingUploads.catch(() => {});
  });
  const root = element("div", { className: "knowledge-images-field" },
    element("span", { className: "knowledge-images-label" }, i18n.t("knowledge.images_field")),
    picker, list);
  render();
  return { root, imageIds: () => kept.map((image) => image.id), ready: () => pendingUploads };
}

function shareLinkRow(
  link: ShareLink,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  active: boolean,
  reportError: (message: string) => void,
  refresh: () => void,
): HTMLElement {
  if (!active) {
    return element("li", { className: "knowledge-link-row resource-card knowledge-link-row-inactive" },
      element("span", { className: "knowledge-link-state" }, i18n.t("knowledge.sharing.deactivated")));
  }
  if (!link.launch_available || link.url === null) {
    return element("li", { className: "knowledge-link-row resource-card" },
      element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.sharing.launch_unavailable")));
  }
  // Share the actual returned Telegram bot URL unchanged; never substitute app_url.
  const shared = link.url;
  const url = element("input", {
    type: "text",
    readonly: true,
    className: "knowledge-link-url",
    value: shared,
    "aria-label": i18n.t("knowledge.sharing.link_url"),
  });
  const copy = element("button", {
    type: "button",
    className: "secondary-button",
    onclick: (() => {
      void (navigator.clipboard?.writeText(shared) ?? Promise.reject())
        .then(() => { copy.textContent = i18n.t("knowledge.sharing.copied"); })
        .catch(() => { url.select(); });
    }) as EventListener,
  }, i18n.t("knowledge.sharing.copy"));
  return element("li", { className: "knowledge-link-row resource-card" },
    element("div", { className: "knowledge-link-url-row" }, url, copy),
    element("button", {
      type: "button",
      className: "danger-button",
      onclick: (() => {
        if (!window.confirm(i18n.t("knowledge.sharing.confirm_deactivate"))) return;
        void actions.deactivateLink?.(link.id)
          .then(refresh)
          .catch(() => reportError(i18n.t("knowledge.error")));
      }) as EventListener,
    }, i18n.t("knowledge.sharing.deactivate")));
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
  imported: () => Promise<void>,
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
        .then(async (result) => {
          actions.consumeImportToken?.();
          await imported();
          // Imports now stage a pending review instead of applying directly.
          const message = result.status === "pending"
            ? i18n.t("knowledge.import.staged")
            : i18n.t("knowledge.import.done", {
              imported: result.imported_count,
              skipped: result.skipped_count,
            });
          replaceChildren(body, element("p", {}, message), element("button", {
            type: "button",
            className: "secondary-button",
            onclick: (() => { closeDialog(dialog); }) as EventListener,
          }, i18n.t("common.close")));
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

function packetCard(
  packet: KnowledgePacketCard,
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  onDissectStarted: () => void,
): HTMLElement {
  const dissect = element("button", {
    type: "button",
    className: "primary-button",
    onclick: (() => {
      // The composer window stays closable: the dissection job is durable
      // server-side and closing never cancels it.
      openDialog(dissectComposerDialog(packet, i18n, state, actions, onDissectStarted));
    }) as EventListener,
  }, i18n.t("knowledge.dissection.submit_short"));
  const detail = (key: KnowledgeMessageKey, value: string): HTMLElement => element(
    "div", { className: "lobby-packet-detail" },
    element("dt", {}, i18n.t(key)), element("dd", {}, value));
  const dissected = packet.dissected_theme_count === null
    ? i18n.t("knowledge.packets.dissected_unknown")
    : i18n.t("knowledge.packets.dissected", { count: packet.dissected_theme_count });
  // Provenance badges carry the actual tournament names, never a generic label.
  const badges = packet.tournaments.length
    ? element("div", { className: "knowledge-tournament-badges" },
      ...packet.tournaments.map((tournament) =>
        element("span", { className: "knowledge-scope-badge knowledge-tournament-badge" },
          tournament.name)))
    : element("div", { className: "knowledge-tournament-badges" },
      element("span", { className: "knowledge-scope-badge" },
        i18n.t(`knowledge.packets.scope.${packet.scope}` as KnowledgeMessageKey)));
  return element("article", {
    className: "resource-card lobby-packet-card knowledge-packet-card",
    "data-packet-id": packet.packet_id,
  },
    element("h3", {}, packet.name),
    badges,
    element("dl", { className: "lobby-packet-details" },
      detail("lobby.packet_year", packet.year?.toString() ?? "—"),
      detail("lobby.packet_authors", packet.authors.map((author) => author.name).join(", ") || "—"),
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
  const state: KnowledgeState = { tagPool: new Map(), tagPoolReady: Promise.resolve() };

  const reloadTagPool = (propagateError = false): Promise<void> => {
    if (!actions.loadTags) return Promise.resolve();
    state.tagPoolReady = actions.loadTags().then((pool) => {
      if (actions.signal?.aborted) return;
      state.tagPool = new Map(pool.items.map((tag) => [tag.name, tag]));
      refreshTagFilterOptions();
      applyTagOutlines();
    }).catch((error: unknown) => {
      if (propagateError) throw error;
      reportError(i18n.t("knowledge.error"));
    });
    return state.tagPoolReady;
  };

  let search = data.facts.search ?? "";
  // Dedicated author filter: a literal case-insensitive substring on the
  // fact author, AND-combined with the search and tag filters.
  let authorSearch = "";
  // Additive multi-tag filter state: every selected tag joins the query (AND).
  const selectedTags = new Set<string>();
  let order = data.facts.order;
  let queryRevision = 0;

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
  const importsTab = tabButton("imports", i18n.t("knowledge.tab.imports"));
  const tabs = element("nav", { className: "settings-actions knowledge-tabs", role: "tablist",
    "aria-label": i18n.t("knowledge.title") }, libraryTab, packetsTab, importsTab);

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
    const revision = queryRevision;
    void actions.loadFacts({ cursor: data.facts.next_cursor, search, order,
      ...(authorSearch ? { author: authorSearch } : {}),
      ...(selectedTags.size ? { tags: [...selectedTags] } : {}) })
      .then((page) => {
        if (revision !== queryRevision || actions.signal?.aborted) return;
        data.facts.items.push(...page.items);
        appendItems(page.items);
        data.facts.next_cursor = page.next_cursor;
        if (page.next_cursor === null) loadMore.remove();
      })
      .catch(() => {
        if (revision !== queryRevision || actions.signal?.aborted) return;
        reportError(i18n.t("knowledge.error"));
      })
      .finally(() => {
        if (revision !== queryRevision || actions.signal?.aborted) return;
        loadMore.disabled = false;
      });
  });

  const reload = (propagateError = false): Promise<void> => {
    const revision = ++queryRevision;
    data.facts.next_cursor = null;
    loadMore.hidden = true;
    loadMore.disabled = true;
    return actions.loadFacts({ search, order,
      ...(authorSearch ? { author: authorSearch } : {}),
      ...(selectedTags.size ? { tags: [...selectedTags] } : {}) })
      .then((page) => {
        if (revision !== queryRevision || actions.signal?.aborted) return;
        data.facts.items = [...page.items];
        data.facts.next_cursor = page.next_cursor;
        data.facts.total = page.total;
        renderList();
        total.textContent = i18n.t("knowledge.total", { total: page.total });
        loadMore.hidden = page.next_cursor === null;
        loadMore.disabled = false;
        if (page.next_cursor !== null && !loadMore.isConnected) pagination.append(loadMore);
      })
      .catch((error: unknown) => {
        if (revision !== queryRevision || actions.signal?.aborted) return;
        reportError(
          (error as { quota?: boolean })?.quota ? i18n.t("knowledge.quota") : i18n.t("knowledge.error"),
        );
        if (propagateError) throw error;
      });
  };

  const searchInput = element("input", {
    type: "search",
    value: search,
    "aria-label": i18n.t("knowledge.search"),
  });
  const authorInput = element("input", {
    type: "search",
    value: authorSearch,
    "aria-label": i18n.t("knowledge.author_search"),
  });
  const orderSelect = element("select", { "aria-label": i18n.t("knowledge.order.title_asc") },
    ...knowledgeOrders.map((value) =>
      element("option", { value }, i18n.t(orderKeys[value]))));
  orderSelect.value = order;
  const tagFilter = element("div", {
    className: "knowledge-tag-filter",
    role: "group",
    "aria-label": i18n.t("knowledge.tag_filter"),
  });
  const refreshTagFilterOptions = (): void => {
    // A tag deleted from the pool can no longer filter anything.
    for (const name of selectedTags) if (!state.tagPool.has(name)) selectedTags.delete(name);
    replaceChildren(tagFilter);
    for (const tag of [...state.tagPool.values()].sort((a, b) =>
      (a.label ?? a.name).localeCompare(b.label ?? b.name))) {
      const pressed = selectedTags.has(tag.name);
      const colour = tag.colour && TAG_COLOUR_PATTERN.test(tag.colour) ? tag.colour : DEFAULT_TAG_COLOUR;
      const chip = element("button", {
        type: "button",
        className: "knowledge-tag-filter-chip",
        "aria-pressed": String(pressed),
      }, tag.label ?? tag.name);
      chip.style.color = colour;
      chip.addEventListener("click", () => {
        const active = selectedTags.has(tag.name);
        if (active) selectedTags.delete(tag.name);
        else selectedTags.add(tag.name);
        chip.setAttribute("aria-pressed", String(!active));
        reload();
      });
      tagFilter.append(chip);
    }
  };

  searchInput.addEventListener("input", () => { search = searchInput.value; });
  searchInput.addEventListener("change", () => { void reload(); });
  authorInput.addEventListener("input", () => { authorSearch = authorInput.value; });
  authorInput.addEventListener("change", () => { void reload(); });
  orderSelect.addEventListener("change", () => {
    order = orderSelect.value as KnowledgeOrder;
    reload();
  });
  root.addEventListener("knowledge:changed", () => { void reload(); });

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
      onclick: (() => { openFactDialog(i18n, state, actions, undefined, reload, reloadTagPool); }) as EventListener,
    }, i18n.t("knowledge.create"))
    : null;

  libraryPanel.append(
    element("div", { className: "knowledge-controls", role: "search" },
      searchInput, authorInput, orderSelect, tagFilter,
      element("button", {
        type: "button", className: "secondary-button",
        onclick: (() => { void reload(); }) as EventListener,
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
      for (const field of packetsControls.querySelectorAll("input")) {
        if (field instanceof HTMLInputElement && field.type === "checkbox") field.checked = false;
        else field.value = "";
      }
      for (const key of Object.keys(packetFilters)) delete packetFilters[key];
      orderSelectPackets.value = "id_asc";
      if (packetsActive && loader) loader.start({}, true);
    }) as EventListener,
  }, i18n.t("knowledge.packets.reset"));
  // Own-uploads-only training packets: the server filters by uploader identity.
  const ownerOnlyFilter = element("input", { type: "checkbox" });
  ownerOnlyFilter.addEventListener("change", () => {
    if (ownerOnlyFilter.checked) packetFilters.owner_uploads_only = "true";
    else delete packetFilters.owner_uploads_only;
    if (packetsActive && loader) loader.start({ ...packetFilters }, true);
  });
  const packetsControls = element("div", { className: "knowledge-controls", role: "search" },
    packetInput("name", "knowledge.packets.search"),
    packetInput("author", "lobby.packet_authors"),
    orderSelectPackets,
    element("label", { className: "knowledge-owner-filter" }, ownerOnlyFilter,
      i18n.t("knowledge.packets.owner_only")),
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
  renderJobs(data.dissection.requests);

  let jobsTimer: number | undefined;
  const refreshJobs = async (): Promise<void> => {
    if (!actions.loadDissections || !packetsActive) return;
    try {
      const items = await actions.loadDissections() ?? [];
      if (!packetsActive || actions.signal?.aborted) return;
      data.dissection.requests = items;
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
    renderJobs(data.dissection.requests);
    replaceChildren(packetList, ...items.map((packet) =>
      packetCard(packet, i18n, state, actions, onDissectStarted)));
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
  // A started dissection refreshes the durable jobs projection and the
  // pending imports list; facts only change after an approved import.
  const onDissectStarted = (): void => {
    void refreshJobs();
    imports.refresh();
  };
  const imports = createImportsPanel(i18n, actions, async () => {
    await Promise.all([reload(), reloadTagPool()]);
  });
  const panels: Record<KnowledgeTab, HTMLElement> = {
    library: libraryPanel, packets: packetsPanel, imports: imports.root,
  };
  const tabButtons: Record<KnowledgeTab, HTMLButtonElement> = {
    library: libraryTab, packets: packetsTab, imports: importsTab,
  };
  const setTab = (tab: KnowledgeTab): void => {
    for (const key of knowledgeTabs) {
      const active = key === tab;
      panels[key].hidden = !active;
      tabButtons[key].className = active
        ? "primary-button knowledge-tab"
        : "secondary-button knowledge-tab";
      if (active) tabButtons[key].setAttribute("aria-current", "page");
      else tabButtons[key].removeAttribute("aria-current");
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
    if (tab === "imports") imports.refresh();
  };
  // Apply the very first render through the same path as a click so the
  // active tab is highlighted immediately, not only after re-pressing it.
  setTab("library");

  renderList();
  reloadTagPool();
  if (data.import_token && actions.importLibrary) {
    openImportDialog(data.import_token, i18n, actions, async () => {
      await Promise.all([reload(true), reloadTagPool(true)]);
      imports.refresh();
    });
  }

  root.append(
    element("h2", { className: "knowledge-title" }, i18n.t("knowledge.title")),
    tabs,
    errorText,
    libraryPanel,
    packetsPanel,
    imports.root,
  );
  return root;
}
