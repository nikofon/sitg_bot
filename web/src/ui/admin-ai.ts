import { ApiError } from "../api/client";
import type {
  AiAdminPlayers,
  AiAdminPlayer,
  AiAdminProvider,
  AiAdminSettings,
  AiAdminTier,
} from "../api/types";
import type { I18n } from "../i18n";
import { element, replaceChildren } from "./dom";
import { renderAdminManagementTabs } from "./admin-management";
import { formatDateTime } from "./knowledge-shared";
import { saveReminder } from "./save-reminder";

const SETTINGS_PATH = "/api/miniapp/admin/ai/settings";
const PROVIDER_PATH = "/api/miniapp/admin/ai/provider";
const PLAYERS_PATH = "/api/miniapp/admin/ai/players";
const TIER_PATH = "/api/miniapp/admin/ai/tier";
const RESET_PATH = "/api/miniapp/admin/ai/reset";
const SEARCH_DEBOUNCE_MS = 250;
const SEARCH_LIMIT = 20;

/** App-level services the AI tab reuses instead of duplicating. */
export interface AdminAiActions {
  reload(): void;
  confirm(message: string): boolean;
  message(title: string, lines: string[]): void;
}

interface QuotaField {
  apply(value: number | null): void;
  read(): number | null;
  node: HTMLElement;
}

type AiQuotaKey =
  | "free_dissection_quota"
  | "free_best_buzz_quota"
  | "premium_dissection_quota"
  | "premium_best_buzz_quota";

/**
 * The "AI functionality" tab of the admin Management window. All data comes
 * from the stable AI administration API; the GETs are cached observations and
 * never trigger a real provider request.
 */
export function renderAdminAi(
  api: { request<T>(path: string, options?: { method?: "GET" | "POST"; body?: unknown; signal?: AbortSignal }): Promise<T> },
  i18n: I18n, signal: AbortSignal, navigate: (path: string) => void, actions: AdminAiActions,
): HTMLElement {
  const title = i18n.t("admin_management.title");
  const writeError = (error: unknown, onStale: () => void): void => {
    if (signal.aborted) return;
    if (error instanceof ApiError && error.code === "stale_write") {
      onStale();
      return;
    }
    actions.message(title, [i18n.t(`error.${error instanceof ApiError ? error.code : "internal_error"}`)]);
  };

  // Provider status bar: refreshed only from server-supplied observations.
  const providerState = element("span", { className: "badge" }, i18n.t("admin_ai.state.unknown"));
  const providerTarget = element("strong", {}, "—");
  const providerTimes = element("p", { className: "field-help" });
  const applyProvider = (data: AiAdminProvider): void => {
    providerTarget.textContent = `${data.provider} · ${data.model}`;
    const classes = data.state === "responding" ? "badge badge-accent"
      : data.state === "stale" || data.state === "unconfigured"
        ? "badge badge-warning" : "badge";
    providerState.className = classes;
    providerState.textContent = i18n.t(`admin_ai.state.${data.state}`);
    const times: string[] = [];
    if (data.verified_at) times.push(i18n.t("admin_ai.provider.verified_at",
      { time: formatDateTime(data.verified_at, i18n.locale) }));
    providerTimes.textContent = times.join(" · ");
  };
  const providerCard = element("article", { className: "resource-card admin-ai-provider" },
    element("h2", {}, i18n.t("admin_ai.provider")),
    element("p", { className: "admin-ai-provider-summary" }, providerTarget, " ", providerState),
    providerTimes,
    element("p", { className: "field-help" }, i18n.t("admin_ai.provider.policy_note")));

  // Settings form: rendered from the server, saved only on explicit submit.
  const quotaField = (labelKey: AiQuotaKey): QuotaField => {
    const number = element("input", {
      type: "number", min: "0", max: "2147483647", step: "1", value: "0",
      "aria-label": i18n.t(`admin_ai.${labelKey}`),
    });
    const unlimited = element("input", { type: "checkbox" });
    unlimited.addEventListener("change", () => { number.disabled = unlimited.checked; });
    return {
      apply(value: number | null): void {
        unlimited.checked = value === null;
        number.disabled = value === null;
        number.value = value === null ? "0" : String(value);
      },
      read(): number | null { return unlimited.checked ? null : Number(number.value); },
      node: element("div", { className: "admin-ai-quota" },
        element("label", {}, i18n.t(`admin_ai.${labelKey}`), number),
        element("label", { className: "checkbox-label" }, unlimited, i18n.t("subscriptions.unlimited"))),
    };
  };
  const dissection = element("input", { type: "checkbox", "aria-label": i18n.t("admin_ai.dissection_enabled") });
  const bestBuzz = element("input", { type: "checkbox", "aria-label": i18n.t("admin_ai.best_buzz_enabled") });
  const freeDissection = quotaField("free_dissection_quota");
  const freeBestBuzz = quotaField("free_best_buzz_quota");
  const premiumDissection = quotaField("premium_dissection_quota");
  const premiumBestBuzz = quotaField("premium_best_buzz_quota");
  const period = element("select", { "aria-label": i18n.t("admin_ai.reset_period") },
    ...(["hour", "day", "week"] as const).map((value) =>
      element("option", { value }, i18n.t(`admin_ai.period.${value}`))));
  const settingsInfo = element("p", { className: "field-help", role: "status" });
  let version = 0;
  const applyMetadata = (data: AiAdminSettings): void => {
    version = data.version;
    const time = new Intl.DateTimeFormat(i18n.locale, {
      dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
    }).format(new Date(data.reset_at));
    settingsInfo.textContent = i18n.t("admin_ai.reset_at", { time })
      + " · " + i18n.t("admin_ai.generation", { count: data.quota_generation });
  };
  const applySettings = (data: AiAdminSettings): void => {
    applyMetadata(data);
    dissection.checked = data.dissection_enabled;
    bestBuzz.checked = data.best_buzz_enabled;
    freeDissection.apply(data.free.dissection);
    freeBestBuzz.apply(data.free.best_buzz);
    premiumDissection.apply(data.premium.dissection);
    premiumBestBuzz.apply(data.premium.best_buzz);
    period.value = data.reset_period;
    controls.disabled = false;
    resetButton.disabled = false;
  };
  const stalePrompt = (): void => {
    const dialog = element("dialog", { className: "tournament-dialog", "aria-labelledby": "admin-ai-stale" },
      element("div", { className: "dialog-heading" },
        element("h2", { id: "admin-ai-stale" }, i18n.t("admin_ai.stale_title"))),
      element("p", {}, i18n.t("admin_ai.stale_body")),
      element("div", { className: "settings-actions" },
        element("button", {
          type: "button", className: "primary-button",
          onclick: (() => {
            if (typeof dialog.close === "function") dialog.close();
            else dialog.remove();
            actions.reload();
          }) as EventListener,
        }, i18n.t("admin_ai.reload")),
        element("button", {
          type: "button", className: "secondary-button",
          onclick: (() => {
            if (typeof dialog.close === "function") dialog.close();
            else dialog.remove();
          }) as EventListener,
        }, i18n.t("common.close"))));
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  };
  const saveButton = element("button", { type: "submit", className: "primary-button" }, i18n.t("admin_ai.save"));
  const resetButton = element("button", { type: "button", className: "danger-button", disabled: true },
    i18n.t("admin_ai.reset_all"));
  resetButton.addEventListener("click", () => {
    if (resetButton.disabled) return;
    if (!actions.confirm(i18n.t("admin_ai.reset_confirm"))) return;
    resetButton.disabled = true;
    saveButton.disabled = true;
    void api.request<AiAdminSettings>(RESET_PATH, {
      method: "POST", body: { confirm: true, expected_version: version }, signal,
    }).then((data) => {
      if (signal.aborted) return;
      applyMetadata(data);
      actions.message(title, [i18n.t("admin_ai.reset_done", { count: data.quota_generation })]);
    }).catch((error: unknown) => {
      writeError(error, stalePrompt);
    }).finally(() => {
      if (!signal.aborted) {
        resetButton.disabled = false;
        saveButton.disabled = false;
      }
    });
  });
  const controls = element("fieldset", { disabled: true },
    element("label", { className: "checkbox-label" }, dissection, i18n.t("admin_ai.dissection_enabled")),
    element("label", { className: "checkbox-label" }, bestBuzz, i18n.t("admin_ai.best_buzz_enabled")),
    freeDissection.node, freeBestBuzz.node, premiumDissection.node, premiumBestBuzz.node,
    element("label", {}, i18n.t("admin_ai.reset_period"), period),
    settingsInfo,
    element("p", { className: "field-help" }, i18n.t("admin_ai.quota_help")),
    saveButton, resetButton);
  const form = element("form", { className: "settings-form" }, controls);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (controls.disabled || saveButton.disabled) return;
    saveButton.disabled = true;
    resetButton.disabled = true;
    void api.request<AiAdminSettings>(SETTINGS_PATH, {
      method: "POST",
      body: {
        dissection_enabled: dissection.checked,
        best_buzz_enabled: bestBuzz.checked,
        free: { dissection: freeDissection.read(), best_buzz: freeBestBuzz.read() },
        premium: { dissection: premiumDissection.read(), best_buzz: premiumBestBuzz.read() },
        reset_period: period.value,
        expected_version: version,
      },
      signal,
    }).then((data) => {
      if (signal.aborted) return;
      applyMetadata(data);
      actions.message(title, [i18n.t("admin_ai.saved")]);
    }).catch((error: unknown) => {
      writeError(error, stalePrompt);
    }).finally(() => {
      if (!signal.aborted) {
        saveButton.disabled = false;
        resetButton.disabled = false;
      }
    });
  });

  // Player tier assignment: bounded server-side search with debounce and abort.
  const search = element("input", {
    type: "search", "aria-label": i18n.t("admin_ai.player_search"),
    placeholder: i18n.t("admin_ai.player_search_help"),
  });
  const searchProgress = element("p", { className: "field-help", hidden: true }, i18n.t("common.loading"));
  const results = element("ul", { className: "resource-list", "aria-live": "polite" });
  let searchTimer: number | undefined;
  let searchSequence = 0;
  let searchController: AbortController | undefined;
  const clearSearch = (): void => {
    window.clearTimeout(searchTimer);
    searchController?.abort();
    searchController = undefined;
    ++searchSequence;
  };
  const tierRow = (player: AiAdminPlayer): HTMLElement => {
    const select = element("select", { "aria-label": i18n.t("admin_ai.tier") },
      ...(["free", "premium", "no_ai"] as const).map((value) =>
        element("option", { value }, i18n.t(`admin_ai.tier.${value}`))));
    select.value = player.tier;
    const apply = element("button", { type: "button", className: "secondary-button" },
      i18n.t("admin_ai.apply"));
    const row = element("li", { className: "resource-card", "data-player-id": player.id },
      element("h3", {}, player.name),
      element("p", { className: "field-help" }, player.id),
      element("div", { className: "admin-ai-tier" }, select, apply));
    apply.addEventListener("click", () => {
      if (apply.disabled) return;
      const tier = select.value as AiAdminTier;
      apply.disabled = true;
      void api.request<{ id: string; tier: AiAdminTier }>(TIER_PATH, {
        method: "POST", body: { player_id: player.id, tier }, signal,
      }).then((result) => {
        if (signal.aborted || result.id !== player.id) return;
        // A stale response never lands on a row that a new search replaced.
        if (!row.isConnected) return;
        select.value = result.tier;
      }).catch((error: unknown) => {
        writeError(error, stalePrompt);
      }).finally(() => {
        if (row.isConnected) apply.disabled = false;
      });
    });
    return row;
  };
  const runSearch = (): void => {
    const query = search.value.trim();
    if (query.length < 2) {
      clearSearch();
      replaceChildren(results, element("li", { className: "field-help" },
        i18n.t("admin_ai.player_search_min")));
      return;
    }
    const sequence = ++searchSequence;
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;
    searchProgress.hidden = false;
    void api.request<AiAdminPlayers>(
      `${PLAYERS_PATH}?search=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`,
      { signal: controller.signal },
    ).then((data) => {
      if (signal.aborted || sequence !== searchSequence) return;
      replaceChildren(results, ...data.items.map(tierRow));
      if (!data.items.length) results.append(element("li", { className: "field-help" },
        i18n.t("admin_ai.player_search_empty")));
    }).catch((error: unknown) => {
      if (signal.aborted || sequence !== searchSequence) return;
      if (error instanceof DOMException && error.name === "AbortError") return;
      replaceChildren(results, element("li", { className: "error-text", role: "alert" },
        i18n.t(`error.${error instanceof ApiError ? error.code : "internal_error"}`)));
    }).finally(() => {
      if (sequence === searchSequence) searchProgress.hidden = true;
    });
  };
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
  });
  signal.addEventListener("abort", () => clearSearch(), { once: true });

  const root = element("section", { className: "route-content" },
    renderAdminManagementTabs("ai", i18n, navigate),
    saveReminder(i18n),
    providerCard,
    form,
    element("section", { className: "admin-ai-players", "aria-labelledby": "admin-ai-players" },
      element("h2", { id: "admin-ai-players" }, i18n.t("admin_ai.players")),
      element("div", { className: "settings-form admin-ai-player-search" },
        element("label", {}, i18n.t("admin_ai.player_search"), search), searchProgress),
      element("p", { className: "field-help" }, i18n.t("admin_ai.tier_help")),
      results));

  const loadFailed = (error: unknown): void => {
    if (signal.aborted) return;
    actions.message(title, [i18n.t(`error.${error instanceof ApiError ? error.code : "internal_error"}`)]);
  };
  // One observation per page open; no polling, no provider request, no background work.
  void api.request<AiAdminSettings>(SETTINGS_PATH, { signal }).then((data) => {
    if (signal.aborted) return;
    applySettings(data);
  }).catch((error: unknown) => {
    if (error instanceof DOMException && error.name === "AbortError") return;
    loadFailed(error);
  });
  void api.request<AiAdminProvider>(PROVIDER_PATH, { signal }).then((data) => {
    if (signal.aborted) return;
    applyProvider(data);
  }).catch((error: unknown) => {
    if (error instanceof DOMException && error.name === "AbortError") return;
    loadFailed(error);
  });
  return root;
}
