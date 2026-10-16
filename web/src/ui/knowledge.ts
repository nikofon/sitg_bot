import { element, replaceChildren } from "./dom";

export type KnowledgeOrder = "title_asc" | "title_desc" | "updated_desc";

export const knowledgeOrders: readonly KnowledgeOrder[] = ["title_asc", "title_desc", "updated_desc"];

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
  tags: Array<{ id: string; name: string }>;
  origin: "manual" | "dissection";
  content_version: number;
  editable: boolean;
  statistics: KnowledgeStatistics;
}

export interface KnowledgeSharing {
  grants: Array<{ id: string; recipient_name: string; revoked: boolean }>;
}

export interface KnowledgeDissection {
  packets: Array<{ id: string; name: string }>;
  requests: Array<{
    id: string;
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
}

export interface KnowledgeActions {
  loadFacts: (params: {
    cursor?: string | null;
    search?: string;
    order?: KnowledgeOrder;
  }) => Promise<KnowledgeFactsResource>;
  loadFact: (factId: string) => Promise<KnowledgeFactDetail>;
  grant: (recipient: string) => Promise<void>;
  revoke: (grantId: string) => Promise<void>;
  dissect: (packetId: string, instructions: string) => Promise<void>;
}

export const knowledgeMessageKeys = [
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
  "knowledge.fact.open",
  "knowledge.fact.tags",
  "knowledge.fact.links",
  "knowledge.fact.origin.manual",
  "knowledge.fact.origin.dissection",
  "knowledge.fact.read_only",
  "knowledge.fact.version",
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
  "knowledge.sharing.title",
  "knowledge.sharing.empty",
  "knowledge.sharing.recipient",
  "knowledge.sharing.grant",
  "knowledge.sharing.revoke",
  "knowledge.sharing.revoked",
  "knowledge.dissection.title",
  "knowledge.dissection.packet",
  "knowledge.dissection.instructions",
  "knowledge.dissection.submit",
  "knowledge.dissection.empty",
  "knowledge.dissection.status.pending",
  "knowledge.dissection.status.succeeded",
  "knowledge.dissection.status.partially_succeeded",
  "knowledge.dissection.status.failed",
  "knowledge.dissection.partial",
  "knowledge.dissection.base",
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

const originKeys: Record<"manual" | "dissection", KnowledgeMessageKey> = {
  manual: "knowledge.fact.origin.manual",
  dissection: "knowledge.fact.origin.dissection",
};

const dissectionStatusKeys: Record<string, KnowledgeMessageKey> = {
  pending: "knowledge.dissection.status.pending",
  succeeded: "knowledge.dissection.status.succeeded",
  partially_succeeded: "knowledge.dissection.status.partially_succeeded",
  failed: "knowledge.dissection.status.failed",
};

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

function factCard(
  fact: KnowledgeFactSummary,
  i18n: KnowledgeI18n,
  details: HTMLElement,
  loadFact: (factId: string) => Promise<KnowledgeFactDetail>,
): HTMLElement {
  const body = element("div", { className: "resource-card knowledge-fact" });
  const open = element("button", {
    type: "button",
    className: "secondary-button knowledge-fact-open",
    onclick: (() => {
      void (async () => {
        replaceChildren(details, element("p", {}, i18n.t("knowledge.loading")));
        try {
          const detail = await loadFact(fact.id);
          renderFactDetail(detail, i18n, details);
        } catch {
          replaceChildren(details, element("p", { className: "error-text" }, i18n.t("knowledge.error")));
        }
      })();
    }) as EventListener,
  }, fact.title);
  body.append(open);
  if (fact.tag_names.length) {
    body.append(
      element("p", { className: "knowledge-fact-tags" }, i18n.t("knowledge.fact.tags", {
        tags: fact.tag_names.join(", "),
      })),
    );
  }
  body.append(...statisticLines(fact.statistics, i18n).map((line) =>
    element("p", { className: "knowledge-statistic" }, line)));
  return body;
}

function renderFactDetail(
  detail: KnowledgeFactDetail,
  i18n: KnowledgeI18n,
  container: HTMLElement,
): void {
  const links = detail.links.length
    ? element(
      "ul",
      { className: "knowledge-fact-links" },
      ...detail.links.map((link) =>
        element("li", {}, element("a", { href: link.url, target: "_blank", rel: "noreferrer" }, link.title || link.url))),
    )
    : element("p", {}, i18n.t("knowledge.fact.links"));
  replaceChildren(
    container,
    element("article", { className: "resource-card knowledge-fact-detail" },
      element("h3", {}, detail.title),
      element("p", {}, detail.description),
      element("p", { className: "knowledge-fact-meta" },
        `${i18n.t(originKeys[detail.origin])} · ${i18n.t("knowledge.fact.version", { version: detail.content_version })}`),
      links,
      element("p", { className: "knowledge-fact-meta" },
        statisticLines(detail.statistics, i18n).join(" · ")),
      detail.editable ? null : element("p", { className: "knowledge-readonly" }, i18n.t("knowledge.fact.read_only")),
    ),
  );
}

function sharingSection(
  sharing: KnowledgeSharing,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  reportError: (message: string) => void,
): HTMLElement {
  const section = element("section", { className: "knowledge-sharing" });
  section.append(element("h3", {}, i18n.t("knowledge.sharing.title")));
  const list = element("ul", { className: "resource-list" });
  if (!sharing.grants.length) {
    list.append(element("li", {}, i18n.t("knowledge.sharing.empty")));
  }
  for (const grant of sharing.grants) {
    const row = element("li", { className: "resource-card" }, element("span", {}, grant.recipient_name));
    if (grant.revoked) {
      row.append(element("span", { className: "knowledge-revoked" }, i18n.t("knowledge.sharing.revoked")));
    } else {
      const revoke = element("button", {
        type: "button",
        className: "secondary-button",
        onclick: (() => {
          void actions.revoke(grant.id).catch(() => reportError(i18n.t("knowledge.error")));
        }) as EventListener,
      }, i18n.t("knowledge.sharing.revoke"));
      row.append(revoke);
    }
    list.append(row);
  }
  const recipient = element("input", { "aria-label": i18n.t("knowledge.sharing.recipient") });
  const grantButton = element("button", {
    type: "button",
    className: "primary-button",
    onclick: (() => {
      const name = recipient.value.trim();
      if (!name || grantButton.disabled) return;
      grantButton.disabled = true;
      void actions.grant(name)
        .catch(() => reportError(i18n.t("knowledge.error")))
        .finally(() => { grantButton.disabled = false; });
    }) as EventListener,
  }, i18n.t("knowledge.sharing.grant"));
  section.append(
    list,
    element("div", { className: "settings-form" },
      element("label", {}, i18n.t("knowledge.sharing.recipient"), recipient), grantButton),
  );
  return section;
}

function dissectionSection(
  dissection: KnowledgeDissection,
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  reportError: (message: string) => void,
): HTMLElement {
  const section = element("section", { className: "knowledge-dissection" });
  section.append(element("h3", {}, i18n.t("knowledge.dissection.title")));
  const requests = element("ul", { className: "resource-list" });
  if (!dissection.requests.length) {
    requests.append(element("li", {}, i18n.t("knowledge.dissection.empty")));
  }
  for (const request of dissection.requests) {
    const packet = dissection.packets.find((item) => item.id === request.packet_id);
    requests.append(element("li", { className: "resource-card" },
      element("span", {}, packet?.name ?? request.packet_id),
      element("span", { className: "knowledge-dissection-status" },
        i18n.t(dissectionStatusKeys[request.status] ?? "knowledge.dissection.status.pending")),
      request.base ? element("span", {}, i18n.t("knowledge.dissection.base")) : null,
      element("span", {}, `${request.applied_count}`),
      request.partial ? element("span", { className: "error-text" }, i18n.t("knowledge.dissection.partial")) : null,
    ));
  }
  const packets = element("select", { "aria-label": i18n.t("knowledge.dissection.packet") },
    ...dissection.packets.map((packet) => element("option", { value: packet.id }, packet.name)));
  const instructions = element("textarea", {
    "aria-label": i18n.t("knowledge.dissection.instructions"),
  });
  const submit = element("button", {
    type: "button",
    className: "primary-button",
    disabled: !dissection.packets.length,
    onclick: (() => {
      const packetId = packets.value;
      if (!packetId || submit.disabled) return;
      submit.disabled = true;
      void actions.dissect(packetId, instructions.value)
        .catch(() => reportError(i18n.t("knowledge.error")))
        .finally(() => { submit.disabled = false; });
    }) as EventListener,
  }, i18n.t("knowledge.dissection.submit"));
  section.append(
    requests,
    element("div", { className: "settings-form" },
      element("label", {}, i18n.t("knowledge.dissection.packet"), packets),
      element("label", {}, i18n.t("knowledge.dissection.instructions"), instructions),
      submit),
  );
  return section;
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
  let search = data.facts.search ?? "";
  let order = data.facts.order;
  const list = element("ul", { className: "resource-list knowledge-list" });
  const details = element("div", { className: "knowledge-details" });
  const total = element("p", { className: "knowledge-total" },
    i18n.t("knowledge.total", { total: data.facts.total }));

  const appendItems = (items: KnowledgeFactSummary[]): void => {
    for (const fact of items) {
      list.append(element("li", {}, factCard(fact, i18n, details, actions.loadFact)));
    }
  };

  const loadMore = element("button", {
    type: "button",
    className: "secondary-button knowledge-load-more",
  }, i18n.t("knowledge.load_more"));
  loadMore.addEventListener("click", () => {
    if (!data.facts.next_cursor || loadMore.disabled) return;
    loadMore.disabled = true;
    void actions.loadFacts({ cursor: data.facts.next_cursor, search, order })
      .then((page) => {
        appendItems(page.items);
        data.facts.next_cursor = page.next_cursor;
        if (page.next_cursor === null) loadMore.remove();
      })
      .catch(() => reportError(i18n.t("knowledge.error")))
      .finally(() => { loadMore.disabled = false; });
  });

  const searchInput = element("input", {
    type: "search",
    value: search,
    "aria-label": i18n.t("knowledge.search"),
  });
  const orderSelect = element("select", { "aria-label": i18n.t("knowledge.order.title_asc") },
    ...knowledgeOrders.map((value) =>
      element("option", { value }, i18n.t(orderKeys[value]))));
  orderSelect.value = order;

  const reload = (): void => {
    if (searchInput.disabled || orderSelect.disabled) return;
    searchInput.disabled = true;
    orderSelect.disabled = true;
    void actions.loadFacts({ search, order })
      .then((page) => {
        replaceChildren(list);
        appendItems(page.items);
        if (!page.items.length) {
          list.append(element("li", { className: "knowledge-empty" }, i18n.t("knowledge.empty")));
        }
        data.facts.next_cursor = page.next_cursor;
        total.textContent = i18n.t("knowledge.total", { total: page.total });
        if (page.next_cursor === null) loadMore.remove();
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

  searchInput.addEventListener("input", () => { search = searchInput.value; });
  searchInput.addEventListener("change", reload);
  orderSelect.addEventListener("change", () => {
    order = orderSelect.value as KnowledgeOrder;
    reload();
  });

  appendItems(data.facts.items);
  if (!data.facts.items.length) {
    list.append(element("li", { className: "knowledge-empty" }, i18n.t("knowledge.empty")));
  }
  const pagination = element("div", { className: "knowledge-pagination" });
  if (data.facts.next_cursor !== null) pagination.append(loadMore);

  root.append(
    element("h2", {}, i18n.t("knowledge.title")),
    errorText,
    element("div", { className: "knowledge-controls" }, searchInput, orderSelect),
    total,
    list,
    pagination,
    details,
    sharingSection(data.sharing, i18n, actions, reportError),
    dissectionSection(data.dissection, i18n, actions, reportError),
  );
  return root;
}



