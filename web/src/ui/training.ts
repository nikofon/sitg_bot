import { ApiClient, ApiError } from "../api/client";
import type { TrainingDraft, TrainingPacket, TrainingResult } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element, replaceChildren } from "./dom";
import { PacketLoader, type PacketPage } from "./packet-loading";
import { saveReminder } from "./save-reminder";

type ModifierKey = "reversed" | "hidden" | "best_buzz";

/** Haptic feedback bridge matching the regular-lobby MiniAppPlatform behavior. */
export interface TrainingFeedback {
  notifySuccess(): void;
  notifyError(): void;
}

const MODIFIER_KEYS: readonly ModifierKey[] = ["reversed", "hidden", "best_buzz"];
// Best buzz lives in its own settings section; the picker configures the rest.
const PICKER_KEYS: readonly ModifierKey[] = ["reversed", "hidden"];
const modifierLabelKey = (key: ModifierKey): MessageKey =>
  key === "reversed" ? "training.reversed" : key === "hidden" ? "training.hidden" : "training.best_buzz";
// Verified modifier/setting override map: hidden reveals the question instantly,
// so the effective reading interval is 0 while the stored token_delay is retained.
const OVERRIDDEN_BY_HIDDEN = 0;

export function renderTraining(
  initial: TrainingResult,
  api: ApiClient,
  i18n: I18n,
  signal: AbortSignal,
  uploadOnly = false,
  feedback?: TrainingFeedback,
): HTMLElement {
  const root = element("section", { className: "route-content lobby-content training-lobby" });
  let lobby = initial.lobby ?? null;
  // Optimistic overlay while a packet autosave is in flight; the server state below wins.
  let pending: { id: string; name: string } | null = null;
  const selectedId = (): string | null => pending?.id ?? lobby?.version_id ?? null;
  const selectedName = (): string | null => pending?.name ?? lobby?.packet_name ?? null;
  let dirty = false;
  let saveBusy = false;
  let queued = 0;
  // Mutations run one at a time in user-action order; each reads the newest version.
  let queue: Promise<void> = Promise.resolve();
  const notice = element("p", { role: "status", "aria-live": "polite" });
  const play = element("div", { className: "stack" });
  const list = element("div", { className: "stack", "aria-live": "polite" });
  const drafts = element("div", { className: "stack" });
  // Persistent setting inputs are the single source of truth for local drafts; the
  // modifier panel moves them in and out of the DOM, so nothing loses values.
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
  const modifierBox: Record<ModifierKey, HTMLInputElement> = { reversed, hidden, best_buzz: bestBuzz };
  const conflicts: Record<ModifierKey, readonly ModifierKey[]> = {
    reversed: ["best_buzz"], hidden: ["best_buzz"], best_buzz: ["reversed", "hidden"],
  };
  let displacedNow = false;
  let state = initial.session ?? null;
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
    // Presentation-only refresh: adopt the authoritative lobby version/selection,
    // but never overwrite the local unsaved settings draft or focused inputs.
    if (result.lobby && queued === 0 && !pending) {
      lobby = result.lobby;
      updateSelection();
      syncPacketSelection();
    }
    const next = result.session ?? null;
    const changed = state?.id !== next?.id || state?.version !== next?.version
      || state?.presentation_version !== next?.presentation_version;
    const keepAnswer = state?.id === next?.id
      && state?.question_revision_id === next?.question_revision_id
      && state?.phase === "answering" && next?.phase === "answering"
      && state?.status === "active" && next?.status === "active";
    state = next;
    if (changed) {
      if (keepAnswer) updateQuestion();
      else renderSession();
    }
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
  const serialize = (task: () => Promise<void>): void => {
    ++queued;
    queue = queue.then(task, task).then(() => {
      --queued;
      syncSave();
      syncPacketSelection();
    }, () => {
      --queued;
      syncSave();
      syncPacketSelection();
    });
  };
  // Stale writes are never retried silently: reload the authoritative lobby,
  // announce it, and keep the local settings draft untouched.
  const reloadLobby = async (): Promise<void> => {
    try {
      const result = await api.request<TrainingResult>("/api/miniapp/training/current", { signal });
      if (signal.aborted) return;
      if (result.lobby) lobby = result.lobby;
      pending = null;
      notice.textContent = i18n.t("error.stale_write");
    } catch (error) { failure(error); }
    updateSelection();
    syncPacketSelection();
  };
  let busy = false;
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
  const updateQuestion = (): void => {
    window.clearTimeout(hideTimer);
    const question = play.querySelector<HTMLElement>(".training-question");
    if (!state || !question) return;
    question.textContent = state.hidden ? i18n.t("training.text_hidden") : state.text;
    if (state.hidden_deadline && !state.hidden && state.phase !== "revealed") {
      const delay = Date.parse(state.hidden_deadline) - Date.now();
      if (delay <= 0) question.textContent = i18n.t("training.text_hidden");
      else hideTimer = window.setTimeout(() => { question.textContent = i18n.t("training.text_hidden"); }, delay);
    }
  };
  const renderSession = (): void => {
    window.clearTimeout(hideTimer);
    play.replaceChildren();
    settingsFieldset.disabled = state !== null && ["active", "paused"].includes(state.status);
    syncSave();
    syncPacketSelection();
    if (!state) return;
    const current = state;
    const question = element("p", { className: "training-question", "aria-live": "polite" },
      current.hidden ? i18n.t("training.text_hidden") : current.text);
    const controls = element("div", { className: "button-row" });
    const button = (command: string, key: MessageKey): void => {
      controls.append(element("button", { type: "button", onclick: (() => void act(command)) as EventListener }, i18n.t(key)));
    };
    play.append(element("h2", {}, `${current.question_index + 1}. ${current.theme} · ${current.value}`), question, controls);
    updateQuestion();
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
    const enabled = MODIFIER_KEYS.filter((key) => modifierBox[key].checked);
    for (const card of selectionCards) {
      card.name.textContent = selectedName() ?? i18n.t("training.select_packet");
      replaceChildren(card.badges, ...enabled.map((key) =>
        element("li", { className: "training-modifier-badge" }, i18n.t(modifierLabelKey(key)))));
    }
    syncSave();
  };
  const syncSave = (): void => {
    // Settings can be saved without a packet; only an active session blocks saving.
    save.disabled = !lobby || !!state || saveBusy;
  };
  const syncPacketSelection = (): void => {
    const current = selectedId();
    const disabled = !lobby || !!state || !!pending;
    for (const card of list.querySelectorAll<HTMLElement>(".training-packet-card")) {
      const id = card.dataset.packetId ?? "";
      const button = card.querySelector<HTMLButtonElement>("button[data-select]");
      if (button) {
        button.disabled = disabled;
        button.className = id === current ? "primary-button" : "secondary-button";
      }
      const badge = card.querySelector<HTMLElement>(".lobby-packet-selected");
      if (id === current) {
        if (!badge) card.querySelector("dl")?.before(
          element("span", { className: "lobby-packet-selected" }, i18n.t("lobby.packet_selected")));
      } else badge?.remove();
    }
  };

  // Packet selection autosaves immediately through the owner's split mutation
  // contract: select_packet never carries settings, so unsaved drafts stay local.
  const selectPacket = (packet: TrainingPacket): void => {
    if (!lobby || state || pending) return;
    pending = { id: packet.id, name: packet.name };
    notice.textContent = i18n.t("training.selecting");
    syncPacketSelection();
    serialize(async () => {
      if (!lobby) return;
      try {
        const result = await post<TrainingResult>("lobby", {
          command: "select_packet", expected_version: lobby.version, version_id: packet.id,
        });
        if (signal.aborted) return;
        if (result.lobby) lobby = result.lobby;
        feedback?.notifySuccess();
        notice.textContent = dirty ? i18n.t("training.unsaved") : i18n.t("training.packet_saved");
      } catch (error) {
        feedback?.notifyError();
        if (error instanceof ApiError && error.code === "stale_write") await reloadLobby();
        else failure(error);
      } finally {
        pending = null;
        updateSelection();
        syncPacketSelection();
      }
    });
  };

  // Modifier panels: pick a display modifier from the dropdown, then enable it and
  // tune the settings that belong to that concrete modifier. Each modifier carries
  // a description of its verified game behavior.
  let modifierView: ModifierKey | null = PICKER_KEYS.find((key) => modifierBox[key].checked) ?? null;
  const modifierPicker = element("select", { "aria-label": i18n.t("training.modifier") },
    element("option", { value: "", selected: modifierView === null }, i18n.t("training.modifier_pick")),
    ...PICKER_KEYS.map((key) => element("option", { value: key, selected: modifierView === key }, i18n.t(modifierLabelKey(key)))));
  modifierPicker.addEventListener("change", () => {
    modifierView = (modifierPicker.value as ModifierKey | "") || null;
    renderModifierConfig();
  });
  const modifierConfig = element("div", { className: "training-modifier-config", "aria-live": "polite" });
  const renderModifierConfig = (): void => {
    replaceChildren(modifierConfig);
    if (!modifierView) return;
    const checkbox = modifierBox[modifierView];
    const enable = element("label", { className: "checkbox-label" }, checkbox, i18n.t(modifierLabelKey(modifierView)));
    const description = element("p", { className: "training-modifier-description" },
      i18n.t(modifierView === "reversed" ? "training.reversed_description" : "training.hidden_description"));
    modifierConfig.append(enable, description);
    if (modifierView === "hidden") {
      modifierConfig.append(settingLabel("training.hidden_seconds", seconds));
      seconds.disabled = !hidden.checked;
    }
  };
  const tokenOverrideNote = element("p", { className: "training-setting-override", role: "note", hidden: true });
  const syncModifierUi = (): void => {
    // Hidden reveals the whole question instantly, so the effective reading
    // interval becomes 0: grey the stored value out and explain the override,
    // but never persist the stored preference as an invalid zero.
    const overridden = hidden.checked;
    seconds.disabled = !hidden.checked;
    tokenDelay.disabled = overridden;
    tokenOverrideNote.hidden = !overridden;
    if (overridden) {
      tokenOverrideNote.textContent = i18n.t("training.override_note", {
        value: String(OVERRIDDEN_BY_HIDDEN), modifier: i18n.t("training.hidden"),
      });
    }
    updateSelection();
    renderModifierConfig();
  };
  const applyModifier = (key: ModifierKey, next: boolean): void => {
    const box = modifierBox[key];
    if (next) {
      for (const other of conflicts[key]) {
        if (modifierBox[other].checked) {
          modifierBox[other].checked = false;
          displacedNow = true;
          notice.textContent = i18n.t("training.modifier_conflict", {
            modifier: i18n.t(modifierLabelKey(other)), other: i18n.t(modifierLabelKey(key)),
          });
        }
      }
      box.checked = true;
    } else {
      box.checked = false;
    }
    syncModifierUi();
  };
  [reversed, hidden, bestBuzz].forEach((input) => input.addEventListener("change", () => {
    applyModifier(input === reversed ? "reversed" : input === hidden ? "hidden" : "best_buzz", input.checked);
  }));

  const save = element("button", { type: "submit", className: "primary-button" }, i18n.t("lobby.settings_save"));
  const settingsFieldset = element("fieldset", {},
    element("legend", {}, i18n.t("training.settings")),
    element("div", { className: "descriptor-categories" },
      element("section", { className: "descriptor-category-group training-modifier-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.modifiers")),
        element("div", { className: "training-modifier-picker" },
          settingLabel("training.modifier", modifierPicker)),
        modifierConfig),
      element("section", { className: "descriptor-category-group training-best-buzz-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.best_buzz_section")),
        element("label", { className: "checkbox-label" }, bestBuzz, i18n.t("training.best_buzz")),
        element("p", { className: "training-modifier-description" }, i18n.t("training.best_buzz_description")),
        element("p", { className: "training-modifier-note" }, i18n.t("training.best_buzz_note"))),
      element("section", { className: "descriptor-category-group training-timing-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.settings_timing")),
        settingLabel("training.buzz_seconds", buzzSeconds),
        settingLabel("training.answer_seconds", answerSeconds)),
      element("section", { className: "descriptor-category-group training-reading-group" },
        element("h3", { className: "descriptor-category" }, i18n.t("training.settings_reading")),
        settingLabel("training.token_delay", tokenDelay),
        tokenOverrideNote,
        settingLabel("training.token_chars", tokenChars))));
  const form = element("form", { className: "settings-form" });
  // Settings save is a separate, explicitly manual mutation: save_settings never
  // carries a packet, so a newly autosaved packet selection is never reverted.
  const draftSettings = (): Record<string, unknown> => ({
    reversed: reversed.checked,
    hidden_after: hidden.checked ? Number(seconds.value) : null,
    buzz_timeout: Number(buzzSeconds.value),
    best_buzz: bestBuzz.checked,
    answer_timeout: Number(answerSeconds.value),
    token_delay: Number(tokenDelay.value),
    token_target_chars: Number(tokenChars.value),
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (hidden.checked && !seconds.checkValidity()) {
      modifierView = "hidden";
      modifierPicker.value = "hidden";
      renderModifierConfig();
      seconds.reportValidity();
      return;
    }
    if (!lobby || state || saveBusy || !form.reportValidity()) return;
    const settings = draftSettings();
    saveBusy = true;
    syncSave();
    serialize(async () => {
      if (!lobby) return;
      const current = lobby;
      try {
        const result = await post<TrainingResult>("lobby", {
          command: "save_settings", expected_version: current.version,
          settings: { ...current.settings, ...settings },
        });
        if (signal.aborted) return;
        if (result.lobby) lobby = result.lobby;
        dirty = JSON.stringify(draftSettings()) !== JSON.stringify(settings);
        feedback?.notifySuccess();
        notice.textContent = i18n.t(dirty ? "training.unsaved" : "training.saved");
        updateSelection();
      } catch (error) {
        feedback?.notifyError();
        if (error instanceof ApiError && error.code === "stale_write") await reloadLobby();
        else failure(error);
      } finally {
        saveBusy = false;
        syncSave();
      }
    });
  });
  form.addEventListener("change", (event) => {
    // Browsing a modifier panel or using the packet search bubble is not a setting change.
    if (event.target === modifierPicker) return;
    if (event.target instanceof Element && event.target.closest(".training-packet-filters")) return;
    dirty = true;
    // A displacement notice explains more than the generic unsaved reminder.
    if (!displacedNow) notice.textContent = i18n.t("training.unsaved");
    displacedNow = false;
  });

  // Packets section: search, sorting and the own-uploads filter in one distinct
  // filter bubble; information-rich packet cards. Filters and order run
  // server-side on the catalogue endpoint (backend contract v1, sitg-knowledge-data),
  // including owner_uploads_only for training-scope packets uploaded by the player.
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
        className: selectedId() === packet.id ? "primary-button" : "secondary-button",
        disabled: !lobby || !!state || !!pending,
        onclick: (() => { selectPacket(packet); }) as EventListener, "data-select": packet.id },
        i18n.t("training.select"));
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
      const retire = packet.scope === "training" ? element("button", { type: "button", className: "danger-button", onclick: (() => {
        if (queued || !window.confirm(i18n.t("training.retire_confirm"))) return;
        serialize(async () => {
          try {
            await post("retire", { version_id: packet.id });
            const current = await api.request<TrainingResult>("/api/miniapp/training/current", { signal });
            if (signal.aborted) return;
            // Retirement does not deselect a saved packet. Retain the authoritative
            // selection/version, explain its unavailability, and preserve drafts.
            lobby = current.lobby ?? null;
            show(current);
            notice.textContent = lobby?.version_id === packet.id
              ? i18n.t("training.retired_selected") : i18n.t("training.retired");
            updateSelection();
            syncPacketSelection();
            loader.reconcile();
          } catch (error) { failure(error); }
        });
      }) as EventListener }, i18n.t("training.retire")) : null;
      return element("article", { className: "resource-card lobby-packet-card training-packet-card", "data-packet-id": packet.id },
        element("h3", {}, packet.name),
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
    syncPacketSelection();
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
  // Owner-only filter: only training packets this player uploaded themselves.
  const mine = element("input", { type: "checkbox" }) as HTMLInputElement;
  mine.addEventListener("change", () => {
    if (mine.checked) filters.owner_uploads_only = "true";
    else delete filters.owner_uploads_only;
    loader.start(filters);
  });
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
    mine.checked = false;
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
    element("label", { className: "checkbox-label" }, mine, i18n.t("training.my_uploads")),
    settingLabel("lobby.packet_sort", order),
    yearRange("lobby.packet_year", "year_from", "year_to"),
    yearRange("lobby.packet_publication_year", "publication_from", "publication_to"),
    reset);
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
  optionsPanel.append(form);
  form.append(settingsFieldset, save);
  root.append(notice);
  if (uploadOnly) {
    root.append(element("h2", {}, i18n.t("training.upload")),
      settingLabel("training.question_values", values), file, drafts);
  } else {
    root.append(play, tabs, ...panels);
  }
  signal.addEventListener("abort", () => { window.clearTimeout(timer); window.clearTimeout(hideTimer); }, { once: true });
  syncModifierUi();
  renderSession(); schedule();
  if (!uploadOnly) loader.start({});
  return root;
}
