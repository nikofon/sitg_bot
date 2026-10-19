import { ApiClient, ApiError } from "../api/client";
import type { TrainingDraft, TrainingPacket, TrainingResult } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element, replaceChildren } from "./dom";
import { PacketLoader, type PacketPage } from "./packet-loading";
import { saveReminder } from "./save-reminder";

type ModifierKey = "reversed" | "hidden" | "best_buzz";

const MODIFIER_KEYS: readonly ModifierKey[] = ["reversed", "hidden", "best_buzz"];
const modifierLabelKey = (key: ModifierKey): MessageKey =>
  key === "reversed" ? "training.reversed" : key === "hidden" ? "training.hidden" : "training.best_buzz";

export function renderTraining(
  initial: TrainingResult, api: ApiClient, i18n: I18n, signal: AbortSignal, uploadOnly = false,
): HTMLElement {
  const root = element("section", { className: "route-content lobby-content training-lobby" });
  let lobby = initial.lobby ?? null;
  let selectedId = lobby?.version_id ?? null;
  let selectedName = lobby?.packet_name ?? null;
  const notice = element("p", { role: "status", "aria-live": "polite" });
  const play = element("div", { className: "stack" });
  const list = element("div", { className: "stack", "aria-live": "polite" });
  const drafts = element("div", { className: "stack" });
  // Persistent setting inputs are the single source of truth; the modifier panel
  // moves them in and out of the DOM, so switching the picker never loses values.
  const reversed = element("input", { type: "checkbox" });
  const hidden = element("input", { type: "checkbox" });
  const seconds = element("input", { type: "number", min: "0.1", max: "3599", step: "0.1", value: "3", required: true });
  const buzzSeconds = element("input", { type: "number", min: "1", max: "3600", value: "10", required: true });
  const answerSeconds = element("input", { type: "number", min: "0.1", max: "3600", step: "0.1", value: "15", required: true });
  const tokenDelay = element("input", { type: "number", min: "0.1", max: "3600", step: "0.1", value: "0.6", required: true });
  const tokenChars = element("input", { type: "number", min: "1", max: "200", step: "1", value: "18", required: true });
  const bestBuzz = element("input", { type: "checkbox" });
  const settingLabel = (key: MessageKey, control: HTMLElement): HTMLElement =>
    element("label", {}, i18n.t(key), control);
  const modifierCheckbox = (key: ModifierKey): HTMLInputElement =>
    key === "reversed" ? reversed : key === "hidden" ? hidden : bestBuzz;
  const enabledModifiers = (): ModifierKey[] =>
    MODIFIER_KEYS.filter((key) => modifierCheckbox(key).checked);
  let state = initial.session ?? null;
  let busy = false;
  let timer: number | undefined;
  let hideTimer: number | undefined;
  const failure = (error: unknown): void => {
    if (signal.aborted) return;
    const code = error instanceof ApiError ? error.code : "internal_error";
    notice.textContent = i18n.t(`error.${code}` as MessageKey);
  };
  const post = <T>(command: string, body: unknown): Promise<T> => api.request<T>(
    `/api/miniapp/training/${command}`, { method: "POST", body, signal },
  );
  const show = (result: TrainingResult): void => {
    if (signal.aborted) return;
    const next = result.session ?? null;
    const changed = state?.id !== next?.id || state?.version !== next?.version
      || state?.presentation_version !== next?.presentation_version;
    state = next;
    if (changed) renderSession();
    schedule();
  };
  const refresh = async (): Promise<void> => {
    if (signal.aborted || busy) return;
    try { show(await api.request<TrainingResult>("/api/miniapp/training/current", { signal })); }
    catch (error) { failure(error); schedule(); }
  };
  const schedule = (): void => {
    window.clearTimeout(timer);
    if (state && state.status === "active" && !signal.aborted) {
      timer = window.setTimeout(() => void refresh(), 600);
    }
  };
  const act = async (command: string, answer?: string): Promise<void> => {
    if (!state || busy) return;
    busy = true;
    const current = state;
    try {
      show(await post<TrainingResult>("act", {
        session_id: current.id, expected_version: current.version,
        question_revision_id: current.question_revision_id, command, answer,
      }));
    } catch (error) {
      failure(error);
      if (error instanceof ApiError && error.code === "stale_write") {
        busy = false;
        await refresh();
      }
    } finally { busy = false; schedule(); }
  };
  const renderSession = (): void => {
    window.clearTimeout(hideTimer);
    play.replaceChildren();
    settingsFieldset.disabled = state !== null && ["active", "paused"].includes(state.status);
    save.disabled = !lobby || !selectedId || !!state;
    if (!state) return;
    const current = state;
    const question = element("p", { className: "training-question", "aria-live": "polite" },
      current.hidden ? i18n.t("training.text_hidden") : current.text);
    const controls = element("div", { className: "button-row" });
    const button = (command: string, key: MessageKey): void => {
      controls.append(element("button", { type: "button", onclick: (() => void act(command)) as EventListener }, i18n.t(key)));
    };
    play.append(element("h2", {}, `${current.question_index + 1}. ${current.theme} · ${current.value}`), question, controls);
    if (current.hidden_deadline && !current.hidden && current.phase !== "revealed") {
      const delay = Date.parse(current.hidden_deadline) - Date.now();
      if (delay <= 0) question.textContent = i18n.t("training.text_hidden");
      else hideTimer = window.setTimeout(() => { question.textContent = i18n.t("training.text_hidden"); }, delay);
    }
    if (["completed", "stopped"].includes(current.status)) {
      play.append(element("p", {}, i18n.t("training.finished")));
      return;
    }
    if (current.status === "paused") button("resume", "training.resume");
    else {
      if (["reading", "waiting", "best_buzz"].includes(current.phase)) button("buzz", "training.buzz");
      if (current.phase === "best_buzz") button("continue", "training.continue");
      if (current.phase === "answering") {
        const input = element("input", { type: "text", maxlength: "4000", "aria-label": i18n.t("training.answer") });
        const form = element("form", {}, input, element("button", { type: "submit" }, i18n.t("training.answer")));
        form.addEventListener("submit", (event) => { event.preventDefault(); void act("answer", input.value); });
        play.append(form);
      }
      if (current.phase === "revealed") {
        play.append(element("p", {}, current.answer ?? ""), element("p", {}, current.commentary ?? ""));
        if (current.correct !== null && current.correct !== undefined) {
          play.append(element("p", {}, i18n.t(current.correct ? "training.correct" : "training.incorrect")));
        }
        button("next", "training.next");
      } else {
        button("reveal", "training.reveal");
        button("skip", "training.skip");
      }
      button("pause", "training.pause");
    }
    button("stop", "training.stop");
  };
  const saved = lobby?.settings ?? {};
  reversed.checked = saved.reversed === true;
  hidden.checked = typeof saved.hidden_after === "number";
  seconds.value = String(saved.hidden_after ?? 3);
  buzzSeconds.value = String(saved.buzz_timeout ?? 10);
  answerSeconds.value = String(saved.answer_timeout ?? 15);
  tokenDelay.value = String(saved.token_delay ?? 0.6);
  tokenChars.value = String(saved.token_target_chars ?? 18);
  bestBuzz.checked = saved.best_buzz === true;

  // Shared selection summary: chosen packet plus enabled modifier badges.
  const selectionCards: Array<{ name: HTMLElement; badges: HTMLElement }> = [];
  const buildSelectionCard = (): HTMLElement => {
    const name = element("p", { className: "training-selection-packet" });
    const badges = element("ul", { className: "training-modifier-badges", "aria-label": i18n.t("training.modifiers") });
    selectionCards.push({ name, badges });
    return element("div", { className: "resource-card training-selection" },
      element("h3", {}, i18n.t("training.selected_packet")), name, badges);
  };
  const updateSelection = (): void => {
    const enabled = enabledModifiers();
    for (const card of selectionCards) {
      card.name.textContent = selectedName ?? i18n.t("training.select_packet");
      replaceChildren(card.badges, ...enabled.map((key) =>
        element("li", { className: "training-modifier-badge" }, i18n.t(modifierLabelKey(key)))));
    }
    save.disabled = !lobby || !selectedId || !!state;
  };

  // Modifiers group: pick a modifier from the dropdown, then enable it and tune
  // the settings that belong to that concrete modifier.
  let modifierView: ModifierKey | null = enabledModifiers()[0] ?? null;
  const modifierPicker = element("select", { "aria-label": i18n.t("training.modifier") },
    element("option", { value: "", selected: modifierView === null }, i18n.t("training.modifier_pick")),
    ...MODIFIER_KEYS.map((key) => element("option", { value: key, selected: modifierView === key }, i18n.t(modifierLabelKey(key)))));
  modifierPicker.addEventListener("change", () => {
    modifierView = (modifierPicker.value as ModifierKey | "") || null;
    renderModifierConfig();
  });
  const modifierConfig = element("div", { className: "training-modifier-config", "aria-live": "polite" });
  const renderModifierConfig = (): void => {
    replaceChildren(modifierConfig);
    if (!modifierView) return;
    const checkbox = modifierCheckbox(modifierView);
    const enable = element("label", { className: "checkbox-label" }, checkbox, i18n.t(modifierLabelKey(modifierView)));
    modifierConfig.append(enable);
    if (modifierView === "hidden") {
      modifierConfig.append(settingLabel("training.hidden_seconds", seconds));
      seconds.disabled = !hidden.checked;
    }
  };
  const compatibility = (): void => {
    bestBuzz.disabled = reversed.checked || hidden.checked;
    if (bestBuzz.disabled) bestBuzz.checked = false;
    reversed.disabled = hidden.disabled = bestBuzz.checked;
    if (reversed.disabled) { reversed.checked = false; hidden.checked = false; }
    seconds.disabled = !hidden.checked;
    updateSelection();
    renderModifierConfig();
  };
  [reversed, hidden, bestBuzz].forEach((input) => input.addEventListener("change", compatibility));

  const save = element("button", { type: "submit", className: "primary-button" }, i18n.t("lobby.settings_save"));
  const settingsFieldset = element("fieldset", {},
    element("legend", {}, i18n.t("training.settings")),
    element("div", { className: "descriptor-categories" },
      element("section", { className: "descriptor-category-group training-modifier-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.modifiers")),
        element("div", { className: "training-modifier-picker" },
          settingLabel("training.modifier", modifierPicker)),
        modifierConfig),
      element("section", { className: "descriptor-category-group training-timing-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.settings_timing")),
        settingLabel("training.buzz_seconds", buzzSeconds),
        settingLabel("training.answer_seconds", answerSeconds)),
      element("section", { className: "descriptor-category-group training-reading-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.settings_reading")),
        settingLabel("training.token_delay", tokenDelay),
        settingLabel("training.token_chars", tokenChars))));
  const form = element("form", { className: "settings-form" });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (hidden.checked && !seconds.checkValidity()) {
      modifierView = "hidden";
      modifierPicker.value = "hidden";
      renderModifierConfig();
      seconds.reportValidity();
      return;
    }
    if (busy || !lobby || !selectedId || !form.reportValidity()) return;
    busy = true;
    save.disabled = true;
    void (async () => {
      try {
        const result = await post<TrainingResult>("lobby", {
          command: "save", expected_version: lobby.version, version_id: selectedId,
          settings: { ...lobby.settings,
            reversed: reversed.checked, hidden_after: hidden.checked ? Number(seconds.value) : null,
            buzz_timeout: Number(buzzSeconds.value), best_buzz: bestBuzz.checked,
            answer_timeout: Number(answerSeconds.value), token_delay: Number(tokenDelay.value),
            token_target_chars: Number(tokenChars.value),
          },
        });
        if (signal.aborted) return;
        lobby = result.lobby ?? null;
        notice.textContent = i18n.t("training.saved");
      } catch (error) { failure(error); }
      finally { busy = false; save.disabled = !lobby || !selectedId || !!state; }
    })();
  });
  form.addEventListener("change", (event) => {
    // Browsing a modifier panel or using the packet search bubble is not a setting change.
    if (event.target === modifierPicker) return;
    if (event.target instanceof Element && event.target.closest(".training-packet-filters")) return;
    notice.textContent = i18n.t("training.unsaved");
  });

  // Packets section: search and sorting controls in one distinct filter bubble,
  // and information-rich packet cards. Filters and order run server-side on the
  // catalogue endpoint (backend contract v1, sitg-knowledge-data).
  const filters: Record<string, string> = {};
  const hasActiveFilters = (): boolean => Object.values(filters).some(Boolean);
  const renderPackets = (packets: TrainingPacket[], loading: boolean): void => {
    if (signal.aborted) return;
    const detail = (key: MessageKey, value: string): HTMLElement => element(
      "div", { className: "lobby-packet-detail" }, element("dt", {}, i18n.t(key)), element("dd", {}, value));
    const yesNo = (value: boolean | undefined): string =>
      value === undefined ? "—" : i18n.t(value ? "lobby.packet_yes" : "lobby.packet_no");
    const cards = packets.map((packet) => {
      const authors = packet.authors?.map((author) => author.name).join(", ");
      const tournaments = packet.tournaments?.map((tournament) => tournament.name).join(", ");
      const fresh = packet.fresh_theme_count === undefined || packet.total_theme_count === undefined
        ? "—" : `${packet.fresh_theme_count} / ${packet.total_theme_count}`;
      const dissectedThemes = packet.dissected_theme_count == null || packet.total_theme_count === undefined
        ? "—" : `${packet.dissected_theme_count} / ${packet.total_theme_count}`;
      const select = element("button", { type: "button",
        className: selectedId === packet.id ? "primary-button" : "secondary-button",
        disabled: !lobby || !!state,
        onclick: ((event: Event) => {
          selectedId = packet.id; selectedName = packet.name; updateSelection();
          for (const button of list.querySelectorAll("button[data-select]")) {
            button.className = "secondary-button";
          }
          (event.currentTarget as HTMLButtonElement).className = "primary-button";
          for (const badge of list.querySelectorAll(".lobby-packet-selected")) badge.remove();
          list.querySelector(`[data-packet-id="${packet.id}"] dl`)?.before(
            element("span", { className: "lobby-packet-selected" }, i18n.t("lobby.packet_selected")));
          notice.textContent = i18n.t("training.unsaved");
        }) as EventListener, "data-select": packet.id }, i18n.t("training.select"));
      const prepare = element("button", { type: "button", className: "secondary-button", onclick: (async () => {
        try {
          const body = { version_id: packet.id, confirm_exposure: false };
          let job = await api.request<{ confirmation_required?: boolean; request_id?: string }>(
            "/api/miniapp/knowledge/best_buzz", { method: "POST", body, signal });
          if (job.confirmation_required && window.confirm(i18n.t("knowledge.confirm_exposure"))) {
            job = await api.request("/api/miniapp/knowledge/best_buzz", {
              method: "POST", body: { ...body, confirm_exposure: true }, signal });
          }
          if (job.request_id) {
            notice.textContent = i18n.t("knowledge.dissection.status.queued");
            const poll = async (): Promise<void> => {
              if (signal.aborted) return;
              try {
                const status = await api.request<{ status: string }>(
                  `/api/miniapp/training/analysis?request_id=${encodeURIComponent(job.request_id ?? "")}`, { signal });
                if (signal.aborted) return;
                notice.textContent = i18n.t(`knowledge.dissection.status.${status.status}` as MessageKey);
                if (["queued", "running"].includes(status.status)) window.setTimeout(() => { void poll(); }, 2000);
              } catch (error) { failure(error); }
            };
            void poll();
          }
        } catch (error) { failure(error); }
      }) as EventListener }, i18n.t("training.prepare"));
      const retire = packet.scope === "training" ? element("button", { type: "button", className: "danger-button", onclick: (async () => {
        if (!window.confirm(i18n.t("training.retire_confirm"))) return;
        try {
          await post("retire", { version_id: packet.id });
          if (state?.packet_version_id === packet.id) show({ session: null });
          if (selectedId === packet.id) { selectedId = null; selectedName = null; updateSelection(); }
          loader.reconcile();
        }
        catch (error) { failure(error); }
      }) as EventListener }, i18n.t("training.retire")) : null;
      return element("article", { className: "resource-card lobby-packet-card training-packet-card", "data-packet-id": packet.id },
        element("h3", {}, packet.name),
        selectedId === packet.id
          ? element("span", { className: "lobby-packet-selected" }, i18n.t("lobby.packet_selected")) : null,
        element("dl", { className: "lobby-packet-details" },
          detail("lobby.packet_year", packet.year?.toString() ?? "—"),
          detail("lobby.packet_authors", authors ?? "—"),
          detail("training.packet_tournaments", tournaments ?? "—"),
          detail("training.packet_themes", packet.total_theme_count === undefined ? "—" : String(packet.total_theme_count)),
          detail("training.packet_fresh", fresh),
          detail("training.packet_dissected", yesNo(packet.dissected)),
          detail("training.packet_dissected_themes", dissectedThemes),
          detail("training.packet_best_buzz", yesNo(packet.best_buzz_calculated))),
        element("div", { className: "settings-actions" }, select, prepare, retire));
    });
    replaceChildren(list, ...cards,
      loading ? element("p", { role: "status" }, i18n.t("common.loading")) : null,
      !loading && !cards.length ? element("p", {}, i18n.t(hasActiveFilters() ? "lobby.packet_no_matches" : "training.empty")) : null);
  };
  const loader = new PacketLoader<TrainingPacket>(signal,
    async (query, pageSignal) => {
      const page = await api.request<PacketPage<TrainingPacket>>(
        `/api/miniapp/training/packets?${query}`, { signal: pageSignal },
      );
      return { ...page, items: page.items.map((packet) => ({ ...packet, packet_id: packet.id })) };
    },
    renderPackets,
    (error) => { failure(error); return true; });
  const filterInput = (name: string, numeric = false): HTMLInputElement => {
    const input = element("input", {
      name, type: numeric ? "number" : "search", value: "",
      maxlength: numeric ? undefined : "200",
      min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
      step: numeric ? "1" : undefined,
    }) as HTMLInputElement;
    input.addEventListener("input", () => {
      if (input.value) filters[name] = input.value;
      else delete filters[name];
      loader.start(filters, true);
    });
    return input;
  };
  const search = filterInput("name");
  const author = filterInput("author");
  const order = element("select", { "aria-label": i18n.t("lobby.packet_sort"),
    onchange: (() => {
      filters.order = order.value;
      loader.start(filters);
    }) as EventListener });
  order.append(
    element("option", { value: "id_asc", selected: true }, i18n.t("lobby.packet_sort_default")),
    element("option", { value: "name_asc" }, i18n.t("filters.name_asc")),
    element("option", { value: "name_desc" }, i18n.t("filters.name_desc")),
    element("option", { value: "year_asc" }, i18n.t("lobby.packet_sort_year_asc")),
    element("option", { value: "year_desc" }, i18n.t("lobby.packet_sort_year_desc")));
  const yearRange = (label: MessageKey, from: string, to: string): HTMLElement =>
    element("fieldset", { className: "lobby-packet-year-range" },
      element("legend", {}, i18n.t(label)),
      settingLabel("lobby.packet_from", filterInput(from, true)),
      settingLabel("lobby.packet_to", filterInput(to, true)));
  const reset = element("button", { type: "button", className: "secondary-button", onclick: (() => {
    for (const field of [search, author, ...bubble.querySelectorAll<HTMLInputElement>('.lobby-packet-year-range input')]) {
      field.value = "";
    }
    for (const key of Object.keys(filters)) delete filters[key];
    order.value = "id_asc";
    loader.start({});
  }) as EventListener }, i18n.t("lobby.packet_reset"));
  const file = element("input", { type: "file", accept: ".json,.docx,.pdf", "aria-label": i18n.t("training.upload") });
  const values = element("input", { type: "text", value: "10,20,30,40,50", "aria-label": i18n.t("training.question_values") });
  file.addEventListener("change", () => void (async () => {
    const selected = file.files?.[0];
    if (!selected || selected.size > 4 * 1024 * 1024) return;
    try {
      const bytes = new Uint8Array(await selected.arrayBuffer());
      let binary = "";
      for (const byte of bytes) binary += String.fromCharCode(byte);
      const result = await post<{ drafts: TrainingDraft[] }>("upload", {
        source_filename: selected.name, source_base64: btoa(binary),
        question_values: values.value.split(",").map(Number),
      });
      if (signal.aborted) return;
      for (const draft of result.drafts) {
        const card = element("article", { className: "resource-card" },
          element("pre", {}, JSON.stringify(draft.packet, null, 2)),
          ...draft.errors.map((error) => element("p", {}, error)));
        for (const publish of draft.errors.length ? [false] : [true, false]) {
          card.append(element("button", { type: "button", onclick: (async () => {
            try {
              await post("draft", { draft_id: draft.draft_id, expected_version: draft.version, publish });
              card.remove(); loader.reconcile();
            } catch (error) { failure(error); }
          }) as EventListener }, i18n.t(publish ? "training.publish" : "training.reject")));
        }
        drafts.append(card);
      }
    } catch (error) { failure(error); }
  })());
  const overview = element("section", { className: "lobby-overview stack training-overview" },
    element("h2", {}, i18n.t("training.lobby")),
    element("p", { className: "training-overview-hint" }, i18n.t(lobby ? "training.lobby_hint" : "training.create_hint")),
    buildSelectionCard());
  const bubble = element("div", { className: "lobby-packet-filters training-packet-filters", role: "search", "aria-label": i18n.t("lobby.packet_filters") },
    settingLabel("lobby.packet_search", search),
    settingLabel("lobby.packet_authors", author),
    settingLabel("lobby.packet_sort", order),
    yearRange("lobby.packet_year", "year_from", "year_to"),
    yearRange("lobby.packet_publication_year", "publication_from", "publication_to"),
    reset);
  bubble.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target instanceof HTMLInputElement) event.preventDefault();
  });
  const packetsPanel = element("section", { className: "stack training-packets" },
    element("h2", {}, i18n.t("lobby.packets")),
    bubble,
    list);
  const optionsPanel = element("section", { className: "stack training-options" },
    buildSelectionCard(),
    saveReminder(i18n),
    settingsFieldset,
    save);
  const tabs = element("nav", { className: "settings-actions", "aria-label": i18n.t("lobby.sections") });
  const panels = [overview, packetsPanel, optionsPanel];
  const tabButtons: HTMLButtonElement[] = [];
  (["lobby.overview", "lobby.packets", "lobby.options"] as const).forEach((key, index) => {
    const button = element("button", { type: "button", className: index === 0 ? "primary-button" : "secondary-button",
      onclick: (() => {
        panels.forEach((panel, position) => { panel.hidden = position !== index; });
        tabButtons.forEach((tab, position) => { tab.className = position === index ? "primary-button" : "secondary-button"; });
      }) as EventListener }, i18n.t(key));
    tabButtons.push(button); tabs.append(button);
  });
  panels.forEach((panel, index) => { panel.hidden = index !== 0; });
  form.append(...panels);
  root.append(notice);
  if (uploadOnly) {
    root.append(element("h2", {}, i18n.t("training.upload")),
      settingLabel("training.question_values", values), file, drafts);
  } else {
    root.append(play, tabs, form);
  }
  signal.addEventListener("abort", () => { window.clearTimeout(timer); window.clearTimeout(hideTimer); }, { once: true });
  compatibility();
  renderSession(); schedule();
  if (!uploadOnly) loader.start({});
  return root;
}
