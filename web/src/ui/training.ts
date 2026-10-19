import { ApiClient, ApiError } from "../api/client";
import type { LibraryAccess, TrainingDraft, TrainingPacket, TrainingResult } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element, replaceChildren } from "./dom";
import { PacketLoader, type PacketPage } from "./packet-loading";
import { renderLibraryReader } from "./library";
import { saveReminder } from "./save-reminder";

export function renderTraining(
  initial: TrainingResult, api: ApiClient, i18n: I18n, signal: AbortSignal, uploadOnly = false,
): HTMLElement {
  const root = element("section", { className: "route-content lobby-content training-lobby" });
  let lobby = initial.lobby ?? null;
  let selectedId = lobby?.version_id ?? null;
  let selectedName = lobby?.packet_name ?? null;
  const selection = element("p", { className: "resource-card" });
  const updateSelection = (): void => {
    selection.textContent = selectedName ?? i18n.t("training.select_packet");
  };
  updateSelection();
  const notice = element("p", { role: "status", "aria-live": "polite" });
  const play = element("div", { className: "stack" });
  const list = element("div", { className: "stack" });
  const drafts = element("div", { className: "stack" });
  const reader = element("div", { className: "stack" });
  const reversed = element("input", { type: "checkbox" });
  const hidden = element("input", { type: "checkbox" });
  const seconds = element("input", { type: "number", min: "0.1", max: "3599", step: "0.1", value: "3" });
  const buzzSeconds = element("input", { type: "number", min: "1", max: "3600", value: "10" });
  const answerSeconds = element("input", { type: "number", min: "0.1", max: "3600", step: "0.1", value: "15", required: true });
  const tokenDelay = element("input", { type: "number", min: "0.1", max: "3600", step: "0.1", value: "0.6", required: true });
  const tokenChars = element("input", { type: "number", min: "1", max: "200", step: "1", value: "18", required: true });
  seconds.required = buzzSeconds.required = true;
  const bestBuzz = element("input", { type: "checkbox" });
  const settings = element("fieldset", { className: "settings-form resource-card" }, element("legend", {}, i18n.t("training.settings")),
    element("label", {}, reversed, i18n.t("training.reversed")),
    element("label", {}, hidden, i18n.t("training.hidden")),
    element("label", {}, i18n.t("training.hidden_seconds"), seconds),
    element("label", {}, i18n.t("training.buzz_seconds"), buzzSeconds),
    element("label", {}, i18n.t("training.answer_seconds"), answerSeconds),
    element("label", {}, i18n.t("training.token_delay"), tokenDelay),
    element("label", {}, i18n.t("training.token_chars"), tokenChars),
    element("label", {}, bestBuzz, i18n.t("training.best_buzz")));
  const compatibility = (): void => {
    bestBuzz.disabled = reversed.checked || hidden.checked;
    if (bestBuzz.disabled) bestBuzz.checked = false;
    reversed.disabled = hidden.disabled = bestBuzz.checked;
    seconds.disabled = !hidden.checked;
  };
  [reversed, hidden, bestBuzz].forEach((input) => input.addEventListener("change", compatibility));
  const saved = lobby?.settings ?? {};
  reversed.checked = saved.reversed === true;
  hidden.checked = typeof saved.hidden_after === "number";
  seconds.value = String(saved.hidden_after ?? 3);
  buzzSeconds.value = String(saved.buzz_timeout ?? 10);
  answerSeconds.value = String(saved.answer_timeout ?? 15);
  tokenDelay.value = String(saved.token_delay ?? 0.6);
  tokenChars.value = String(saved.token_target_chars ?? 18);
  bestBuzz.checked = saved.best_buzz === true;
  compatibility();
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
    settings.disabled = state !== null && ["active", "paused"].includes(state.status);
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
  const save = element("button", { type: "submit", className: "primary-button" }, i18n.t("lobby.settings_save"));
  const form = element("form", { className: "settings-form" });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
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
      finally { busy = false; save.disabled = false; }
    })();
  });
  const loader = new PacketLoader<TrainingPacket>(signal,
    async (query, pageSignal) => {
      const page = await api.request<PacketPage<TrainingPacket>>(
        `/api/miniapp/training/packets?${query}`, { signal: pageSignal },
      );
      return { ...page, items: page.items.map((packet) => ({ ...packet, packet_id: packet.id })) };
    },
    (packets, loading) => {
      if (signal.aborted) return;
      replaceChildren(list, ...packets.map((packet) => element("article", { className: "resource-card" },
        element("h3", {}, packet.name), element("button", { type: "button",
          className: selectedId === packet.id ? "primary-button" : "secondary-button",
          disabled: !lobby || !!state,
          onclick: ((event: Event) => {
            selectedId = packet.id; selectedName = packet.name; updateSelection();
            list.querySelectorAll("button[data-select]").forEach((button) => { button.className = "secondary-button"; });
            (event.currentTarget as HTMLButtonElement).className = "primary-button";
            notice.textContent = i18n.t("training.unsaved");
          }) as EventListener, "data-select": packet.id }, i18n.t("training.select")),
        element("button", { type: "button", onclick: (async () => {
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
        }) as EventListener }, i18n.t("training.prepare")),
        ...[false, true].map((download) => element("button", { type: "button", onclick: (async () => {
          try {
            let result = await post<LibraryAccess>("access", { version_id: packet.id, confirm: false, download });
            if (signal.aborted) return;
            if (result.confirmation_required) {
              if (!window.confirm(i18n.t("library.confirm"))) return;
              result = await post<LibraryAccess>("access", { version_id: packet.id, confirm: true, download });
            }
            if (signal.aborted || result.confirmation_required) return;
            if ("pages" in result) replaceChildren(reader, renderLibraryReader(result.name, result.pages, i18n));
            else notice.textContent = i18n.t("library.queued");
          } catch (error) { failure(error); }
        }) as EventListener }, i18n.t(download ? "library.download" : "library.view"))),
        packet.scope === "training" ? element("button", { type: "button", onclick: (async () => {
          if (!window.confirm(i18n.t("training.retire_confirm"))) return;
          try {
            await post("retire", { version_id: packet.id });
            if (state?.packet_version_id === packet.id) show({ session: null });
            reader.replaceChildren();
            loader.reconcile();
          }
          catch (error) { failure(error); }
        }) as EventListener }, i18n.t("training.retire")) : null,
      )), loading ? element("p", {}, i18n.t("common.loading")) : null);
    }, (error) => { failure(error); return true; });
  const search = element("input", { type: "search", maxlength: "200", "aria-label": i18n.t("training.search") });
  search.addEventListener("input", () => loader.start({ name: search.value }, true));
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
  const packetsPanel = element("section", { className: "stack" },
    element("h2", {}, i18n.t("lobby.packets")), search, list, reader);
  const optionsPanel = element("section", { className: "stack" }, settings);
  const overview = element("section", { className: "lobby-overview stack" },
    element("h2", {}, i18n.t("training.lobby")),
    element("p", {}, i18n.t(lobby ? "training.lobby_hint" : "training.create_hint")));
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
  form.append(saveReminder(i18n), selection, ...panels, save);
  save.disabled = !lobby || !!state;
  form.addEventListener("change", () => { notice.textContent = i18n.t("training.unsaved"); });
  root.append(notice);
  if (uploadOnly) {
    root.append(element("h2", {}, i18n.t("training.upload")),
      element("label", {}, i18n.t("training.question_values"), values), file, drafts);
  } else {
    root.append(play, tabs, form);
  }
  signal.addEventListener("abort", () => { window.clearTimeout(timer); window.clearTimeout(hideTimer); }, { once: true });
  renderSession(); schedule();
  if (!uploadOnly) loader.start({});
  return root;
}
