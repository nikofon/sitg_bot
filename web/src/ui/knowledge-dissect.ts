import { element, replaceChildren } from "./dom";
import type {
  KnowledgeActions,
  KnowledgeDissectRequest,
  KnowledgeDissectionQuota,
  KnowledgeI18n,
  KnowledgePacketCard,
  KnowledgeThemeDetail,
  KnowledgeThemePreview,
  KnowledgeThemeSummary,
} from "./knowledge";
import {
  closeDialog,
  dialogFrame,
  formatDateTime,
  tagChip,
  type KnowledgeState,
} from "./knowledge-shared";

/**
 * Packet dissection composer: a closable window with General/Themes tabs.
 * General carries the selected-theme counter, commentary, the auto-assigned
 * tag pool selection and the start action; Themes lists the packet's themes,
 * collapsed with independent dissect checkboxes, expanding through the
 * explicit exposure-confirmed theme preview contract.
 */
export function dissectComposerDialog(
  packet: KnowledgePacketCard | { id: string; name: string },
  i18n: KnowledgeI18n,
  state: KnowledgeState,
  actions: KnowledgeActions,
  onStarted: () => void,
): HTMLDialogElement {
  const { dialog, body } = dialogFrame(i18n.t("knowledge.dissection.title"), i18n);

  const selectedThemes = new Set<string>();
  const selectedTags = new Set<string>();
  const themeDetails = new Map<string, KnowledgeThemeDetail>();
  const themeCheckboxes = new Map<string, HTMLInputElement>();
  let allThemes: KnowledgeThemeSummary[] = [];
  let themesLoaded = false;
  let submitting = false;
  let quota: KnowledgeDissectionQuota | undefined;

  // Tab navigation between General and Themes.
  const generalPanel = element("section", {
    className: "knowledge-panel knowledge-composer-panel", "data-composer-panel": "general",
  });
  const themesPanel = element("section", {
    className: "knowledge-panel knowledge-composer-panel", "data-composer-panel": "themes",
    hidden: true,
  });
  const generalTab = element("button", { type: "button", className: "primary-button knowledge-tab" },
    i18n.t("knowledge.dissection.tab.general"));
  const themesTab = element("button", { type: "button", className: "secondary-button knowledge-tab" },
    i18n.t("knowledge.dissection.tab.themes"));
  const setComposerTab = (tab: "general" | "themes"): void => {
    const general = tab === "general";
    generalPanel.hidden = !general;
    themesPanel.hidden = general;
    generalTab.className = general ? "primary-button knowledge-tab" : "secondary-button knowledge-tab";
    themesTab.className = general ? "secondary-button knowledge-tab" : "primary-button knowledge-tab";
    if (general) {
      generalTab.setAttribute("aria-current", "page");
      themesTab.removeAttribute("aria-current");
    } else {
      themesTab.setAttribute("aria-current", "page");
      generalTab.removeAttribute("aria-current");
    }
  };
  generalTab.addEventListener("click", () => setComposerTab("general"));
  themesTab.addEventListener("click", () => setComposerTab("themes"));
  const tabs = element("nav", { className: "settings-actions knowledge-tabs", role: "tablist",
    "aria-label": i18n.t("knowledge.dissection.title") }, generalTab, themesTab);

  // General tab.
  const selectedCount = element("p", {
    className: "knowledge-fact-meta", "data-dissect-selected-count": "",
  }, i18n.t("knowledge.dissection.selected_count", { count: 0 }));
  const instructions = element("textarea", {
    "aria-label": i18n.t("knowledge.dissection.instructions"),
  });
  const comment = element("textarea", { "aria-label": i18n.t("knowledge.dissection.comment") });
  const tagList = element("div", { className: "knowledge-tag-options", role: "group",
    "aria-label": i18n.t("knowledge.tags.selected") });
  const warning = element("p", { className: "knowledge-dissection-warning", hidden: true },
    i18n.t("knowledge.confirm_exposure"));
  const hint = element("p", { className: "knowledge-fact-meta", hidden: true },
    i18n.t("knowledge.dissection.themes_hint"));
  const error = element("p", { className: "error-text", role: "alert" });
  const submit = element("button", { type: "button", className: "primary-button",
    "data-confirm": "false", disabled: true }, i18n.t("knowledge.dissection.submit"));

  const syncSelectedCount = (): void => {
    selectedCount.textContent = i18n.t("knowledge.dissection.selected_count",
      { count: selectedThemes.size });
    // Never fall back to all themes while metadata is loading, failed or absent.
    submit.disabled = submitting || !themesLoaded || selectedThemes.size === 0
      || quota?.enabled === false;
    hint.hidden = !(themesLoaded && selectedThemes.size === 0);
  };

  const renderTagOptions = (): void => {
    replaceChildren(tagList);
    if (!state.tagPool.size) {
      tagList.append(element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.tags.field_empty")));
      return;
    }
    for (const tag of [...state.tagPool.values()].sort((a, b) =>
      (a.label ?? a.name).localeCompare(b.label ?? b.name))) {
      const input = element("input", { type: "checkbox", value: tag.tag_id });
      input.checked = selectedTags.has(tag.tag_id);
      input.addEventListener("change", () => {
        if (input.checked) selectedTags.add(tag.tag_id);
        else selectedTags.delete(tag.tag_id);
      });
      tagList.append(element("div", { className: "knowledge-tag-option" },
        element("label", { className: "checkbox-label" }, input, tagChip(tag))));
    }
  };
  renderTagOptions();
  void state.tagPoolReady.then(renderTagOptions).catch(() => { /* the pool reload surfaces failures */ });

  const run = (confirmExposure: boolean): void => {
    if (submit.disabled || !themesLoaded || selectedThemes.size === 0 || submitting) return;
    submitting = true;
    syncSelectedCount();
    const request: KnowledgeDissectRequest = {
      theme_revision_ids: [...selectedThemes],
      instructions: instructions.value || undefined,
      comment: comment.value || undefined,
      tag_ids: [...selectedTags],
      confirm_exposure: confirmExposure,
    };
    void actions.dissect(packet.id, request)
      .then((outcome) => {
        if (outcome.confirmation_required && !confirmExposure) {
          warning.hidden = false;
          submit.textContent = i18n.t("knowledge.dissection.confirm");
          submit.setAttribute("data-confirm", "true");
          submit.classList.add("warning-button");
          return;
        }
        // The job is durable server-side: the window closes freely.
        closeDialog(dialog);
        onStarted();
      })
      .catch(() => { error.textContent = i18n.t("knowledge.error"); })
      .finally(() => {
        submitting = false;
        if (document.contains(dialog)) syncSelectedCount();
      });
  };
  submit.addEventListener("click", () => {
    run(submit.getAttribute("data-confirm") === "true");
  });

  generalPanel.append(
    element("h3", {}, packet.name),
    "total_theme_count" in packet
      ? element("p", { className: "knowledge-fact-meta" }, i18n.t("knowledge.packets.fresh",
        { fresh: packet.fresh_theme_count, total: packet.total_theme_count }))
      : element("p", { className: "knowledge-fact-meta", hidden: true }),
    selectedCount,
    element("label", {}, i18n.t("knowledge.dissection.instructions"), instructions),
    element("label", {}, i18n.t("knowledge.dissection.comment"), comment),
    element("fieldset", { className: "knowledge-tag-select" },
      element("legend", {}, i18n.t("knowledge.tags.selected")), tagList),
    hint,
    warning,
    submit,
    error,
  );

  // Themes tab.
  const quotaLine = element("p", {
    className: "knowledge-fact-meta knowledge-dissection-quota", role: "status", hidden: true,
  });
  const themesError = element("p", { className: "error-text", role: "alert" });
  const selectAll = element("button", { type: "button", className: "secondary-button",
    "data-dissect-select-all": "" }, i18n.t("knowledge.dissection.select_all"));
  const themeList = element("ul", { className: "knowledge-theme-list" });

  const renderQuota = (): void => {
    if (!quota) { quotaLine.hidden = true; return; }
    let text: string;
    if (!quota.enabled) text = i18n.t("knowledge.ai_unavailable");
    else if (quota.remaining === null) text = i18n.t("knowledge.dissection.quota_unlimited");
    else {
      text = i18n.t("knowledge.dissection.quota", { count: quota.remaining });
      if (quota.reset_at !== null) {
        text += " " + i18n.t("knowledge.dissection.quota_reset",
          { time: formatDateTime(quota.reset_at, i18n.locale) });
      }
    }
    quotaLine.textContent = text;
    quotaLine.hidden = false;
  };

  const allSelected = (): boolean =>
    allThemes.length > 0 && selectedThemes.size === allThemes.length;
  const syncSelectAll = (): void => {
    selectAll.textContent = i18n.t(allSelected()
      ? "knowledge.dissection.deselect_all"
      : "knowledge.dissection.select_all");
  };
  selectAll.addEventListener("click", () => {
    if (allSelected()) {
      selectedThemes.clear();
      for (const checkbox of themeCheckboxes.values()) checkbox.checked = false;
    } else {
      for (const theme of allThemes) {
        selectedThemes.add(theme.theme_revision_id);
        themeCheckboxes.get(theme.theme_revision_id)!.checked = true;
      }
    }
    syncSelectedCount();
    syncSelectAll();
  });

  const renderThemeDetail = (detail: KnowledgeThemeDetail): HTMLElement =>
    element("div", { className: "knowledge-theme-detail" },
      element("p", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.dissection.theme_author", { author: detail.author })),
      detail.commentary
        ? element("p", { className: "knowledge-fact-description" }, detail.commentary)
        : null,
      element("ul", { className: "knowledge-theme-questions" },
        ...detail.questions.map((question) => element("li", { className: "knowledge-theme-question" },
          element("p", { className: "knowledge-fact-meta" },
            i18n.t("training_history.value", { value: question.value })),
          element("p", { className: "knowledge-theme-question-text" }, question.text),
          element("p", { className: "knowledge-fact-meta" },
            `${i18n.t("training_history.answer")}: ${question.answer}`),
          question.accepted_answers.length
            ? element("p", { className: "knowledge-fact-meta" },
              `${i18n.t("training_history.accepted")}: ${question.accepted_answers.join(", ")}`)
            : null,
          question.rejected_answers.length
            ? element("p", { className: "knowledge-fact-meta" },
              `${i18n.t("training_history.rejected")}: ${question.rejected_answers.join(", ")}`)
            : null,
          question.commentary
            ? element("p", { className: "knowledge-fact-meta" },
              `${i18n.t("training_history.commentary")}: ${question.commentary}`)
            : null,
        ))));

  const loadThemeDetail = (theme: KnowledgeThemeSummary, themeBody: HTMLElement): void => {
    const preview = actions.previewTheme;
    if (!preview) return;
    replaceChildren(themeBody, element("p", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.loading")));
    void preview(packet.id, theme.theme_revision_id, false)
      .then((outcome: KnowledgeThemePreview) => {
        if (outcome.confirmation_required) {
          // Explicit exposure confirmation must precede revealing content.
          const confirmButton = element("button", {
            type: "button", className: "warning-button knowledge-theme-confirm",
          }, i18n.t("knowledge.dissection.confirm_theme"));
          confirmButton.addEventListener("click", () => {
            replaceChildren(themeBody, element("p", { className: "knowledge-fact-meta", role: "status" },
              i18n.t("knowledge.loading")));
            void preview(packet.id, theme.theme_revision_id, true)
              .then((confirmed: KnowledgeThemePreview) => {
                if (confirmed.confirmation_required) return;
                themeDetails.set(theme.theme_revision_id, confirmed.theme);
                replaceChildren(themeBody, renderThemeDetail(confirmed.theme));
              })
              .catch(() => {
                replaceChildren(themeBody, element("p", { className: "error-text" },
                  i18n.t("knowledge.error")));
              });
          });
          replaceChildren(themeBody,
            element("p", { className: "knowledge-dissection-warning" },
              i18n.t("knowledge.dissection.exposure_units", { count: outcome.fresh_unit_count })),
            confirmButton);
          return;
        }
        themeDetails.set(theme.theme_revision_id, outcome.theme);
        replaceChildren(themeBody, renderThemeDetail(outcome.theme));
      })
      .catch(() => {
        replaceChildren(themeBody, element("p", { className: "error-text" },
          i18n.t("knowledge.error")));
      })
      .finally(() => { delete themeBody.dataset.loading; });
  };

  const renderThemes = (): void => {
    replaceChildren(themeList);
    themeCheckboxes.clear();
    if (!allThemes.length) {
      themeList.append(element("li", { className: "knowledge-fact-meta" },
        i18n.t("knowledge.dissection.themes_empty")));
      return;
    }
    for (const theme of allThemes) {
      const checkbox = element("input", {
        type: "checkbox", value: theme.theme_revision_id, "aria-label": theme.name,
      });
      checkbox.checked = selectedThemes.has(theme.theme_revision_id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedThemes.add(theme.theme_revision_id);
        else selectedThemes.delete(theme.theme_revision_id);
        syncSelectedCount();
        syncSelectAll();
      });
      themeCheckboxes.set(theme.theme_revision_id, checkbox);
      const toggle = element("button", {
        type: "button", className: "knowledge-theme-toggle", "aria-expanded": "false",
        "aria-controls": `knowledge-theme-body-${theme.theme_revision_id}`,
      }, element("span", { className: "knowledge-theme-name" }, theme.name),
        element("span", { className: "knowledge-fact-chevron", "aria-hidden": "true" }, "▾"));
      const themeBody = element("div", {
        className: "knowledge-theme-body", id: `knowledge-theme-body-${theme.theme_revision_id}`,
        hidden: true,
      });
      toggle.addEventListener("click", () => {
        const expanded = toggle.getAttribute("aria-expanded") === "true";
        if (expanded) {
          toggle.setAttribute("aria-expanded", "false");
          themeBody.hidden = true;
          return;
        }
        toggle.setAttribute("aria-expanded", "true");
        themeBody.hidden = false;
        const cached = themeDetails.get(theme.theme_revision_id);
        if (cached) replaceChildren(themeBody, renderThemeDetail(cached));
        else if (themeBody.dataset.loading !== "true" && actions.previewTheme) {
          themeBody.dataset.loading = "true";
          loadThemeDetail(theme, themeBody);
        }
      });
      themeList.append(element("li", {
        className: "knowledge-theme-row", "data-theme-id": theme.theme_revision_id,
      }, element("div", { className: "knowledge-theme-head" },
        element("label", { className: "checkbox-label" }, checkbox), toggle), themeBody));
    }
    syncSelectAll();
  };

  if (actions.loadThemes) {
    themeList.append(element("li", { className: "knowledge-fact-meta", role: "status" },
      i18n.t("knowledge.dissection.themes_loading")));
    void actions.loadThemes(packet.id)
      .then((resource) => {
        if (actions.signal?.aborted) return;
        allThemes = resource.themes;
        themesLoaded = true;
        quota = resource.quota;
        renderQuota();
        renderThemes();
        syncSelectedCount();
      })
      .catch(() => {
        themesError.textContent = i18n.t("knowledge.error");
      });
  } else {
    themesError.textContent = i18n.t("knowledge.error");
  }

  themesPanel.append(
    element("div", { className: "settings-actions knowledge-dissection-theme-controls" },
      selectAll),
    quotaLine,
    themesError,
    themeList,
  );

  setComposerTab("general");
  body.append(tabs, generalPanel, themesPanel);
  return dialog;
}
