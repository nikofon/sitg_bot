import type { ApiClient } from "../api/client";
import type { I18n } from "../i18n";
import { element } from "./dom";
import {
  renderKnowledge,
  type KnowledgeActions,
  type KnowledgeDissection,
  type KnowledgeDissectOutcome,
  type KnowledgeFactDetail,
  type KnowledgeFactsResource,
  type KnowledgeImportResult,
  type KnowledgePacketCard,
  type KnowledgeSharing,
  type KnowledgeTagPool,
  type KnowledgeTagPoolEntry,
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
    versionId: string, instructions: string, comment = "", confirmExposure = false,
  ): Promise<KnowledgeDissectOutcome> =>
    post("dissect", { version_id: versionId, instructions, comment, confirm_exposure: confirmExposure });
  const loadPacketPage = (
    query: URLSearchParams, requestSignal: AbortSignal,
  ): Promise<{ items: KnowledgePacketCard[]; next_cursor: string | null }> =>
    api.request(`/api/miniapp/training/packets?${query}`, { signal: requestSignal });
  const actions: KnowledgeActions = {
    signal,
    loadSharing: () => api.request<KnowledgeSharing>("/api/miniapp/knowledge/sharing", { signal }),
    loadFacts: (params) => {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
      return api.request<KnowledgeFactsResource>(`/api/miniapp/knowledge/facts?${query}`, { signal });
    },
    loadFact: (id) => api.request<KnowledgeFactDetail>(
      `/api/miniapp/knowledge/facts/${encodeURIComponent(id)}`, { signal }),
    save: (values, detail) => post<KnowledgeFactDetail>(detail ? "update" : "create", {
      ...values, ...(detail ? { fact_id: detail.id, expected_version: detail.content_version } : {}),
    }),
    remove: async (detail) => { await post("delete", { fact_id: detail.id, confirm_dissection_origin: true }); },
    merge: async (detail, targetId) => {
      const target = await actions.loadFact(targetId);
      await post("merge", { source_fact_id: detail.id, target_fact_id: target.id,
        expected_target_version: target.content_version, confirm_source_delete: true });
    },
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
  };
  const root = element("section", { className: "stack" },
    element("a", { href: "/training", className: "knowledge-back" }, i18n.t("training.title")),
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
