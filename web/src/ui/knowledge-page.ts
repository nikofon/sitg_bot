import type { ApiClient } from "../api/client";
import type { I18n } from "../i18n";
import { element } from "./dom";
import { renderKnowledge, type KnowledgeActions, type KnowledgeFactDetail,
  type KnowledgeFactsResource, type KnowledgeViewData, type KnowledgeDissection } from "./knowledge";

export function renderKnowledgePage(
  initial: KnowledgeViewData, api: ApiClient, i18n: I18n, signal: AbortSignal,
): HTMLElement {
  let packetCursor: string | null | undefined = initial.dissection.next_cursor;
  const post = <T>(command: string, body: unknown): Promise<T> =>
    api.request<T>(`/api/miniapp/knowledge/${command}`, { method: "POST", body, signal });
  const analysis = async (versionId: string, instructions: string, comment = ""): Promise<void> => {
    const body = { version_id: versionId, instructions, comment, confirm_exposure: false };
    let result = await post<{ confirmation_required?: boolean }>("dissect", body);
    if (result.confirmation_required && window.confirm(i18n.t("knowledge.confirm_exposure"))) {
      result = await post("dissect", { ...body, confirm_exposure: true });
    }
  };
  const actions: KnowledgeActions = {
    loadSharing: () => api.request("/api/miniapp/knowledge/sharing", { signal }),
    signal,
    loadFacts: (params) => {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
      return api.request<KnowledgeFactsResource>(`/api/miniapp/knowledge/facts?${query}`, { signal });
    },
    loadFact: (id) => api.request<KnowledgeFactDetail>(`/api/miniapp/knowledge/facts/${encodeURIComponent(id)}`, { signal }),
    save: (values, detail) => post<KnowledgeFactDetail>(detail ? "update" : "create", {
      ...values, ...(detail ? { fact_id: detail.id, expected_version: detail.content_version } : {}),
    }),
    remove: async (detail) => { await post("delete", { fact_id: detail.id, confirm_dissection_origin: true }); },
    merge: async (detail, targetId) => {
      const target = await actions.loadFact(targetId);
      await post("merge", { source_fact_id: detail.id, target_fact_id: target.id,
        expected_target_version: target.content_version, confirm_source_delete: true });
    },
    tag: async (command, tagId, name) => { await post(command, { tag_id: tagId, ...(name ? { name } : {}) }); },
    grant: async (recipient) => { await post("grant", { recipient }); },
    dissect: analysis,
    loadDissections: async () => (await api.request<{ items: KnowledgeDissection["requests"] }>(
      "/api/miniapp/knowledge/dissections", { signal })).items,
    cancel: async (requestId) => { await post("cancel", { request_id: requestId }); },
    loadPackets: async () => {
      if (!packetCursor) return [];
      const page = await api.request<{ items: Array<{ id: string; name: string }>; next_cursor: string | null }>(
        `/api/miniapp/training/packets?cursor=${encodeURIComponent(packetCursor)}`, { signal });
      packetCursor = page.next_cursor;
      return page.items;
    },
  };
  const root = element("section", { className: "stack" },
    element("a", { href: "/training" }, i18n.t("training.title")),
    renderKnowledge(initial, i18n, actions));
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
