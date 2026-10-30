import type { ApiClient } from "../api/client";
import type { I18n } from "../i18n";
import { element } from "./dom";
import {
  renderKnowledge,
  type KnowledgeActions,
  type KnowledgeDissection,
  type KnowledgeDissectOutcome,
  type KnowledgeDissectRequest,
  type KnowledgeFactDetail,
  type KnowledgeFactImage,
  type KnowledgeFactsResource,
  type KnowledgeImportsResource,
  type KnowledgeImportDecision,
  type KnowledgeImportDetail,
  type KnowledgeImportResult,
  type KnowledgePacketCard,
  type KnowledgeSharing,
  type KnowledgeTagPool,
  type KnowledgeTagPoolEntry,
  type KnowledgeThemePreview,
  type KnowledgeThemesResource,
  type KnowledgeViewData,
  type ShareLink,
} from "./knowledge";

const IMPORT_FRAGMENT_KEY = "import";

function importTokenFromLocation(): string | undefined {
  const hash = window.location.hash;
  if (!hash.startsWith("#")) return undefined;
  const token = new URLSearchParams(hash.slice(1)).get(IMPORT_FRAGMENT_KEY);
  return token ? token : undefined;
}

function consumeImportToken(): void {
  if (importTokenFromLocation() === undefined) return;
  window.history.replaceState({}, "", `${window.location.pathname}${window.location.search}`);
}

export function renderKnowledgePage(
  initial: KnowledgeViewData, api: ApiClient, i18n: I18n, signal: AbortSignal,
): HTMLElement {
  const pendingImport = initial.import_token ?? importTokenFromLocation();
  const post = <T>(command: string, body: unknown): Promise<T> =>
    api.request<T>(`/api/miniapp/knowledge/${command}`, { method: "POST", body, signal });
  const dissect = async (
    versionId: string, request: KnowledgeDissectRequest,
  ): Promise<KnowledgeDissectOutcome> =>
    post("dissect", { version_id: versionId, ...request });
  const loadPacketPage = async (
    query: URLSearchParams, requestSignal: AbortSignal,
  ): Promise<{ items: KnowledgePacketCard[]; next_cursor: string | null }> => {
    const page = await api.request<{ items: KnowledgePacketCard[]; next_cursor: string | null }>(
      `/api/miniapp/training/packets?${query}`, { signal: requestSignal });
    return { ...page, items: page.items.map((packet) => ({ ...packet, packet_id: packet.id })) };
  };
  const loadFacts = (params: {
    owner_id?: string;
    tags?: string[];
    cursor?: string | null;
    search?: string;
    author?: string;
    order?: string;
  }): Promise<KnowledgeFactsResource> => {
    const query = new URLSearchParams();
    if (params.owner_id) query.set("owner_id", params.owner_id);
    if (params.cursor) query.set("cursor", params.cursor);
    if (params.search) query.set("search", params.search);
    if (params.author) query.set("author", params.author);
    if (params.order) query.set("order", params.order);
    // Repeated `tags` entries: every selected tag must match (AND semantics).
    for (const tag of params.tags ?? []) query.append("tags", tag);
    return api.request<KnowledgeFactsResource>(`/api/miniapp/knowledge/facts?${query}`, { signal });
  };
  const actions: KnowledgeActions = {
    signal,
    loadSharing: () => api.request<KnowledgeSharing>("/api/miniapp/knowledge/sharing", { signal }),
    loadFacts,
    loadFact: (id) => api.request<KnowledgeFactDetail>(
      `/api/miniapp/knowledge/facts/${encodeURIComponent(id)}`, { signal }),
    save: (values, detail) => post<KnowledgeFactDetail>(detail ? "update" : "create", {
      ...values, ...(detail ? { fact_id: detail.id, expected_version: detail.content_version } : {}),
    }),
    remove: async (detail) => { await post("delete", { fact_id: detail.id, confirm_dissection_origin: true }); },
    uploadImage: (sourceBase64) => post<KnowledgeFactImage>("image_upload", { source_base64: sourceBase64 }),
    tag: async (command, tagId, name, colour) => {
      await post(command, { tag_id: tagId, ...(name ? { name } : {}), ...(colour ? { colour } : {}) });
    },
    loadTags: () => api.request<KnowledgeTagPool>("/api/miniapp/knowledge/tags", { signal }),
    createTag: (name, colour) => post<KnowledgeTagPoolEntry>("tag_create", { name, colour }),
    grant: async (recipient) => { await post("grant", { recipient }); },
    createLink: () => post<ShareLink>("link_create", {}),
    deactivateLink: async (linkId) => { await post("link_deactivate", { link_id: linkId }); },
    importLibrary: (token) => post<KnowledgeImportResult>("import", { token }),
    consumeImportToken,
    loadPackets: loadPacketPage,
    dissect,
    loadDissections: async () => (await api.request<{ items: KnowledgeDissection["requests"] }>(
      "/api/miniapp/knowledge/dissections", { signal })).items,
    cancel: async (requestId) => { await post("cancel", { request_id: requestId }); },
    loadThemes: (versionId) => api.request<KnowledgeThemesResource>(
      `/api/miniapp/knowledge/themes?version_id=${encodeURIComponent(versionId)}`, { signal }),
    previewTheme: (versionId, themeRevisionId, confirmExposure) =>
      post<KnowledgeThemePreview>("theme_preview", {
        version_id: versionId,
        theme_revision_id: themeRevisionId,
        confirm_exposure: confirmExposure,
      }),
    loadImports: (cursor) => {
      const query = new URLSearchParams();
      if (cursor) query.set("cursor", cursor);
      return api.request<KnowledgeImportsResource>(
        `/api/miniapp/knowledge/imports?${query}`, { signal });
    },
    loadImportDetail: (importId) => api.request<KnowledgeImportDetail>(
      `/api/miniapp/knowledge/imports/${encodeURIComponent(importId)}`, { signal }),
    decideImport: (importId, decision, selectedCandidateIds) =>
      post<KnowledgeImportDecision>("import_decide", {
        import_id: importId,
        decision,
        ...(selectedCandidateIds ? { selected_candidate_ids: selectedCandidateIds } : {}),
      }),
  };
  const root = element("section", { className: "stack" },
    renderKnowledge({ ...initial, ...(pendingImport ? { import_token: pendingImport } : {}) },
      i18n, actions));
  const limits = element("p", { role: "status" });
  root.append(limits);
  void api.request<{ features: Record<string, { enabled: boolean; remaining: number | null; limit: number | null; unit: "themes" | "estimates"; reset_at: string | null }> }>(
    "/api/miniapp/training/entitlements", { signal }).then((data) => {
      if (signal.aborted) return;
      limits.textContent = i18n.t("knowledge.capabilities") + ": " + Object.entries(data.features)
        .map(([name, feature]) => {
          const label = name === "dissection" ? i18n.t("knowledge.dissection.title")
            : name === "best_buzz" ? i18n.t("training.best_buzz") : i18n.t("knowledge.common_estimates");
          const unit = i18n.t(feature.unit === "themes" ? "knowledge.unit.themes" : "knowledge.unit.estimates");
          const allowance = feature.enabled ? `${feature.remaining ?? "∞"}/${feature.limit ?? "∞"} ${unit}`
            : i18n.t("knowledge.ai_unavailable");
          return `${label}: ${allowance}`;
        })
        .join(" · ");
    }).catch(() => { /* Views remain available while entitlement loading is retried on next visit. */ });
  return root;
}
