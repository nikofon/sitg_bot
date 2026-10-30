import { element, replaceChildren } from "./dom";
import type {
  KnowledgeActions,
  KnowledgeI18n,
  KnowledgeImportCandidate,
  KnowledgeImportCard,
  KnowledgeImportDetail,
} from "./knowledge";
import {
  closeDialog,
  dialogFrame,
  formatDateTime,
  openDialog,
  tagChip,
} from "./knowledge-shared";

export type KnowledgeImportSectionKey = "new" | "potential_duplicates" | "duplicates";

const sectionKeys: Record<KnowledgeImportSectionKey, "knowledge.imports.section.new"
  | "knowledge.imports.section.potential_duplicates"
  | "knowledge.imports.section.duplicates"> = {
  new: "knowledge.imports.section.new",
  potential_duplicates: "knowledge.imports.section.potential_duplicates",
  duplicates: "knowledge.imports.section.duplicates",
};

const statusKeys: Record<KnowledgeImportCard["status"], "knowledge.imports.status.working"
  | "knowledge.imports.status.pending"
  | "knowledge.imports.status.approved"
  | "knowledge.imports.status.rejected"
  | "knowledge.imports.status.failed"> = {
  working: "knowledge.imports.status.working",
  pending: "knowledge.imports.status.pending",
  approved: "knowledge.imports.status.approved",
  rejected: "knowledge.imports.status.rejected",
  failed: "knowledge.imports.status.failed",
};

export interface ImportsPanel {
  root: HTMLElement;
  refresh: () => void;
}

/**
 * Pending imports tab: dissections and incoming shared libraries awaiting
 * review. Nothing is applied automatically; facts enter the library only
 * through the explicit review decision.
 */
export function createImportsPanel(
  i18n: KnowledgeI18n,
  actions: KnowledgeActions,
  /** Reloads the recipient's fact library after an approved import. */
  libraryChanged: () => Promise<void>,
): ImportsPanel {
  const root = element("section", {
    className: "knowledge-panel", "data-panel": "imports", hidden: true,
  });
  const list = element("ul", { className: "resource-list knowledge-import-list" });
  const loading = element("p", {
    className: "knowledge-fact-meta", role: "status", hidden: true,
  }, i18n.t("knowledge.imports.loading"));
  let revision = 0;

  const refresh = (): void => {
    if (!actions.loadImports) return;
    const current = ++revision;
    loading.hidden = false;
    void actions.loadImports()
      .then((resource) => {
        if (current !== revision || actions.signal?.aborted) return;
        renderCards(resource.items);
      })
      .catch(() => {
        if (current !== revision || actions.signal?.aborted) return;
        replaceChildren(list, element("li", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.error")));
      })
      .finally(() => { loading.hidden = true; });
  };

  const renderCards = (items: KnowledgeImportCard[]): void => {
    replaceChildren(list);
    if (!items.length) {
      list.append(element("li", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.imports.empty")));
      return;
    }
    for (const item of items) {
      list.append(element("li", {}, importCard(item)));
    }
  };

  const importCard = (item: KnowledgeImportCard): HTMLElement => {
    const review = item.status !== "working" && actions.loadImportDetail
      ? element("button", {
        type: "button", className: "secondary-button knowledge-import-review",
        "aria-label": `${i18n.t("knowledge.imports.review")}: ${item.author}`,
        onclick: (() => { openImportReviewDialog(item); }) as EventListener,
      }, i18n.t("knowledge.imports.review"))
      : null;
    return element("article", {
      className: "resource-card knowledge-import-card", "data-import-id": item.id,
    },
      element("span", { className: `knowledge-import-status`, "data-import-status": item.status },
        i18n.t(statusKeys[item.status])),
      element("h3", { className: "knowledge-import-type" },
        i18n.t(item.type === "dissection"
          ? "knowledge.imports.type.dissection"
          : "knowledge.imports.type.collection")),
      element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.imports.author", { author: item.author })),
      element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.imports.fact_count", { count: item.fact_count })),
      // Own authorized dissections carry their source packet; collection
      // imports never expose the sender's private packet provenance.
      item.packet_name !== null
        ? element("p", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.imports.source_packet", { name: item.packet_name }))
        : null,
      element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.imports.created", { date: formatDateTime(item.created_at, i18n.locale) })),
      review ? element("div", { className: "settings-actions" }, review) : null);
  };

  root.append(
    element("div", { className: "settings-actions knowledge-imports-actions" },
      element("button", {
        type: "button", className: "secondary-button",
        onclick: (() => { refresh(); }) as EventListener,
      }, i18n.t("knowledge.refresh"))),
    loading,
    list,
  );
  return { root, refresh };

  function openImportReviewDialog(card: KnowledgeImportCard): void {
    const { dialog, body } = dialogFrame(i18n.t("knowledge.imports.review_title"), i18n);
    const error = element("p", { className: "error-text", role: "alert" });
    const status = element("p", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.loading"));
    body.append(status);
    openDialog(dialog);

    const selectedIds = new Set<string>();
    const decideButtons: HTMLButtonElement[] = [];

    const decide = (decision: "approve" | "reject"): void => {
      if (!actions.decideImport || decideButtons.some((button) => button.disabled)) return;
      if (decision === "reject" && !window.confirm(i18n.t("knowledge.imports.reject_confirm"))) return;
      for (const button of decideButtons) button.disabled = true;
      void actions.decideImport(card.id, decision,
        decision === "approve" ? [...selectedIds] : undefined)
        .then(async () => {
          closeDialog(dialog);
          await libraryChanged();
          refresh();
        })
        .catch(() => {
          // Conflicting or stale decisions keep the review open and honest.
          error.textContent = i18n.t("knowledge.error");
          for (const button of decideButtons) button.disabled = false;
        });
    };

    void actions.loadImportDetail?.(card.id)
      .then((loaded) => {
        if (!document.contains(dialog)) return;
        renderReview(loaded);
      })
      .catch(() => {
        if (!document.contains(dialog)) return;
        replaceChildren(body, element("p", { className: "error-text" },
          i18n.t("knowledge.error")));
      });

    const renderReview = (loaded: KnowledgeImportDetail): void => {
      const terminal = loaded.status === "approved" || loaded.status === "rejected";
      const sections = element("div", { className: "knowledge-import-sections" },
        importSection("new", loaded.sections.new, terminal),
        importSection("potential_duplicates", loaded.sections.potential_duplicates, terminal),
        importSection("duplicates", loaded.sections.duplicates, terminal));
      const actionBar = (): HTMLElement => {
        if (terminal) return element("p", { className: "knowledge-fact-meta" },
          i18n.t(loaded.status === "approved"
            ? "knowledge.imports.approved_note"
            : "knowledge.imports.rejected_note"));
        const add = element("button", {
          type: "button", className: "primary-button knowledge-import-approve",
          "data-import-approve": "",
        }, i18n.t("knowledge.imports.add_selected"));
        add.addEventListener("click", () => decide("approve"));
        const reject = element("button", {
          type: "button", className: "danger-button knowledge-import-reject",
        }, i18n.t("knowledge.imports.reject"));
        reject.addEventListener("click", () => decide("reject"));
        decideButtons.push(add, reject);
        return element("div", { className: "settings-actions" }, add, reject);
      };
      replaceChildren(body,
        element("h3", {}, i18n.t(loaded.type === "dissection"
          ? "knowledge.imports.type.dissection"
          : "knowledge.imports.type.collection")),
        element("p", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.imports.author", { author: loaded.author })),
        element("span", { className: "knowledge-import-status", "data-import-status": loaded.status },
          i18n.t(statusKeys[loaded.status])),
        element("p", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.imports.fact_count", { count: loaded.fact_count })),
        loaded.packet_name !== null
          ? element("p", { className: "knowledge-fact-meta" },
            i18n.t("knowledge.imports.source_packet", { name: loaded.packet_name }))
          : null,
        element("p", { className: "knowledge-fact-meta" },
          i18n.t("knowledge.imports.created",
            { date: formatDateTime(loaded.created_at, i18n.locale) })),
        actionBar(),
        sections,
        actionBar(),
        error);
    };

    const importSection = (
      key: KnowledgeImportSectionKey,
      candidates: KnowledgeImportCandidate[],
      terminal: boolean,
    ): HTMLElement => {
      const rows = element("ul", { className: "knowledge-candidate-list" });
      const checkboxes: HTMLInputElement[] = [];
      for (const candidate of candidates) {
        if (candidate.selected) selectedIds.add(candidate.id);
        rows.append(candidateRow(candidate, terminal, checkboxes));
      }
      const toggleAll = element("button", {
        type: "button", className: "secondary-button",
        "data-import-section-toggle": key,
      }, i18n.t("knowledge.imports.select_all"));
      if (!terminal && candidates.length) {
        const syncToggle = (): void => {
          const all = checkboxes.length > 0 && checkboxes.every((checkbox) => checkbox.checked);
          toggleAll.textContent = i18n.t(all
            ? "knowledge.imports.deselect_all"
            : "knowledge.imports.select_all");
        };
        toggleAll.addEventListener("click", () => {
          const all = checkboxes.every((checkbox) => checkbox.checked);
          for (const checkbox of checkboxes) {
            checkbox.checked = !all;
            if (!all) selectedIds.add(checkbox.value);
            else selectedIds.delete(checkbox.value);
          }
          syncToggle();
        });
        syncToggle();
      } else {
        toggleAll.hidden = true;
      }
      return element("section", { className: "knowledge-import-section",
        "data-import-section": key },
        element("h4", {}, `${i18n.t(sectionKeys[key])} (${candidates.length})`),
        toggleAll,
        rows);
    };

    const candidateRow = (
      candidate: KnowledgeImportCandidate,
      terminal: boolean,
      checkboxes: HTMLInputElement[],
    ): HTMLElement => {
      const detailBody = element("div", {
        className: "knowledge-candidate-body",
        id: `knowledge-candidate-body-${candidate.id}`,
        hidden: true,
      },
        element("p", { className: "knowledge-fact-description" }, candidate.description),
        candidate.author
          ? element("p", { className: "knowledge-fact-meta" },
            i18n.t("knowledge.fact.author", { author: candidate.author }))
          : null,
        candidate.links.length
          ? element("ul", { className: "knowledge-fact-links" },
            ...candidate.links.map((link) => element("li", {},
              element("a", { href: link.url, target: "_blank", rel: "noreferrer" },
                link.title || link.url))))
          : null,
        candidate.tag_names.length
          ? element("p", { className: "knowledge-fact-tags" },
            ...candidate.tag_names.map((name) => tagChip({ name })))
          : null);
      const header = element("button", {
        type: "button", className: "knowledge-theme-toggle", "aria-expanded": "false",
        "aria-controls": `knowledge-candidate-body-${candidate.id}`,
      }, element("span", { className: "knowledge-theme-name" }, candidate.title),
        element("span", { className: "knowledge-fact-chevron", "aria-hidden": "true" }, "▾"));
      header.addEventListener("click", () => {
        const expanded = header.getAttribute("aria-expanded") === "true";
        header.setAttribute("aria-expanded", String(!expanded));
        detailBody.hidden = expanded;
      });
      const head = element("div", { className: "knowledge-theme-head" }, header);
      if (!terminal) {
        // Selection is independent of the row's collapsed/expanded state.
        const checkbox = element("input", {
          type: "checkbox", value: candidate.id, "aria-label": candidate.title,
        });
        checkbox.checked = candidate.selected;
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) selectedIds.add(candidate.id);
          else selectedIds.delete(candidate.id);
        });
        checkboxes.push(checkbox);
        head.prepend(element("label", { className: "checkbox-label" }, checkbox));
      }
      return element("li", {
        className: "knowledge-candidate-row", "data-candidate-id": candidate.id,
      }, head, detailBody);
    };
  }
}
