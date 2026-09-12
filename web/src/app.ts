import { ApiClient, ApiError } from "./api/client";
import type {
  RoutePayload,
  LibraryAccess,
  RouteResource,
  LobbyResource,
  StableErrorCode,
  TournamentAction,
  TournamentDetailsPayload,
  TournamentListItem,
  TournamentManagerSettingsResource,
  TournamentManagerManagementResource,
  ManagementPacket,
  ClassicStage,
  ClassicTournament,
  ManagerSettingDescriptor,
  PacketDraftResource,
  RegisteredAuthor,
  TournamentRegistrationPayload,
  TournamentRouteResource,
  AdminSuspicionInspectionPayload,
  AdminSuspicionLedgerResource,
  SuspicionLedgerCard,
} from "./api/types";
import { I18n } from "./i18n";
import type { MessageKey } from "./i18n/en";
import type { MiniAppPlatform } from "./platform/telegram";
import { Router } from "./routing/router";
import { routeRequestPath, type RouteMatch } from "./routing/routes";
import { FilterStore } from "./state/filter-store";
import { element, replaceChildren } from "./ui/dom";
import { filterNames, renderFilters } from "./ui/filters";
import { renderLobbyPackets, type LobbyPacketFilters } from "./ui/lobby-packets";
import { renderLibrary, renderLibraryReader } from "./ui/library";

export class MiniAppShell {
  private readonly i18n = new I18n("ru");
  private readonly filters = new FilterStore();
  private request?: AbortController;
  private pollTimer?: number;
  private eventTimer?: number;
  private readonly lobbyPacketFilters = new Map<string, LobbyPacketFilters>();
  private managerSection?: {
    tournamentRef?: string;
    name: TournamentManagerManagementResource["sections"][number];
  };

  constructor(
    private readonly root: HTMLElement,
    private readonly api: ApiClient,
    private readonly router: Router,
    private readonly platform: MiniAppPlatform,
    private readonly developmentHarness: boolean,
  ) {}

  start(): void {
    this.platform.initialize();
    this.router.subscribe((route) => void this.load(route));
    this.router.start();
  }

  stop(): void {
    this.request?.abort();
    if (this.pollTimer !== undefined) window.clearTimeout(this.pollTimer);
    if (this.eventTimer !== undefined) window.clearTimeout(this.eventTimer);
    this.router.stop();
    this.platform.destroy();
  }

  private async load(route: RouteMatch): Promise<void> {
    this.request?.abort();
    if (this.pollTimer !== undefined) window.clearTimeout(this.pollTimer);
    if (this.eventTimer !== undefined) window.clearTimeout(this.eventTimer);
    const restored = this.restoreFilters(route);
    if (restored) return;
    const controller = new AbortController();
    this.request = controller;
    this.configureTelegram(route);
    this.renderFrame(route, this.statusCard("loading", this.i18n.t("common.loading")));
    try {
      const path = encodeURIComponent(routeRequestPath(route));
      const payload = await this.api.request<RoutePayload>(
        `/api/miniapp/routes/resolve?path=${path}`,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      if (payload.locale === "ru" || payload.locale === "en") {
        this.i18n.setLocale(payload.locale);
        this.configureTelegram(route);
      }
      if (!payload.authorization?.allowed) {
        this.renderError(route, payload.authorization?.reason_code ?? "forbidden");
        return;
      }
      this.renderRoute(route, payload);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.renderError(route, code);
      if (code !== "authentication_required") this.platform.notifyError();
    }
  }

  private restoreFilters(route: RouteMatch): boolean {
    const names = filterNames(route.id);
    if (names.length === 0) return false;
    const persisted = this.filters.read(route.id);
    const merged = new URLSearchParams(route.query);
    let changed = false;
    for (const name of names) {
      if (!merged.has(name) && persisted[name]) {
        merged.set(name, persisted[name]);
        changed = true;
      }
    }
    if (!changed) return false;
    this.router.navigate(`${route.path}?${merged.toString()}`, { replace: true });
    return true;
  }

  private configureTelegram(route: RouteMatch): void {
    this.platform.setBackHandler(route.isRoot ? undefined : () => this.router.back());
    this.platform.setMainAction(this.i18n.t("common.return_to_bot"), () =>
      this.platform.returnToBot(),
    );
  }

  private renderFrame(route: RouteMatch, content: Node): void {
    document.title = `${this.i18n.t(route.titleKey)} · ${this.i18n.t("app.name")}`;
    const title = element("h1", { id: "page-title", tabindex: "-1" }, this.i18n.t(route.titleKey));
    const header = element(
      "header",
      { className: "shell-header" },
      element("div", { className: "brand", "aria-label": this.i18n.t("app.name") }, "SITG"),
      this.developmentHarness
        ? element("span", { className: "dev-badge" }, this.i18n.t("auth.dev_harness"))
        : null,
    );
    const filterBar = renderFilters(route.id, route.query, this.i18n, (name, value) => {
      const query = new URLSearchParams(route.query);
      if (value) query.set(name, value);
      else query.delete(name);
      query.delete("cursor");
      const values = Object.fromEntries(
        filterNames(route.id)
          .map((key) => [key, query.get(key) ?? ""] as const)
          .filter((entry) => entry[1]),
      );
      this.filters.write(route.id, values);
      this.router.navigate(`${route.path}${query.size ? `?${query.toString()}` : ""}`);
    });
    const main = element(
      "main",
      { id: "main", className: "shell-main", "aria-labelledby": "page-title" },
      title,
      filterBar,
      content,
    );
    replaceChildren(this.root, element("div", { className: "shell" }, header, main));
  }

  private renderRoute(route: RouteMatch, payload: RoutePayload): void {
    if ("kind" in payload.resource && payload.resource.kind === "library") {
      if (route.id === "library_reader") {
        void this.accessLibrary(route, route.params.version_id ?? "", "view");
      } else {
        this.renderFrame(route, renderLibrary(
          payload.resource.items, this.i18n, this.filters.read("library"),
          (filters) => this.filters.write("library", filters),
          (packet, command, button) => {
            if (command === "view") this.router.navigate(`/library/${encodeURIComponent(packet.version_id)}`);
            else void this.accessLibrary(route, packet.version_id, command, button);
          },
          (tournament) => void this.openLibraryTournament(tournament),
        ));
      }
      return;
    }
    if (route.id === "tournaments" && isTournamentResource(payload.resource)) {
      this.renderTournamentRoute(route, payload.resource, payload.pagination);
      const info = route.query.get("info");
      if (info) void this.openLibraryTournament({ id: info, role: payload.resource.role });
      return;
    }
    if (route.id === "manager_settings" && isManagerSettingsResource(payload.resource)) {
      this.renderManagerSettings(route, payload.resource);
      return;
    }
    if (route.id === "manager_management" && isManagerManagementResource(payload.resource)) {
      this.renderManagerManagement(route, payload.resource);
      return;
    }
    if (route.id === "admin_suspicion" && isSuspicionLedgerResource(payload.resource)) {
      this.renderSuspicionLedger(route, payload.resource);
      return;
    }
    if (route.id === "lobby" && isLobbyResource(payload.resource)) {
      this.renderLobby(route, payload.resource);
      return;
    }
    if (route.id === "packet_editor" && isPacketDraftResource(payload.resource)) {
      this.renderPacketEditor(route, payload.resource);
      return;
    }
    const resource = payload.resource as RouteResource;
    let content: HTMLElement;
    if (resource.state === "empty") {
      content = this.statusCard("empty", this.i18n.t(route.emptyKey));
    } else {
      const children: Node[] = [];
      if (resource.summary) {
        children.push(element("p", { className: "resource-summary" }, resource.summary));
      }
      if (resource.items?.length) {
        const list = element("ul", { className: "resource-list" });
        for (const item of resource.items) {
          list.append(
            element(
              "li",
              {},
              element("article", { className: "resource-card" }, element("h2", {}, item.label),
                item.description ? element("p", {}, item.description) : null),
            ),
          );
        }
        children.push(list);
      }
      if (children.length === 0) children.push(this.statusCard("empty", this.i18n.t(route.emptyKey)));
      content = element("section", { className: "route-content" }, ...children);
    }
    if (payload.pagination?.previous || payload.pagination?.next) {
      content.append(this.pagination(route, payload.pagination));
    }
    this.renderFrame(route, content);
    queueMicrotask(() => document.querySelector<HTMLElement>("#page-title")?.focus());
  }

  private async openLibraryTournament(tournament: { id: string; role: TournamentRouteResource["role"] }): Promise<void> {
    const signal = this.request?.signal;
    try {
      const details = await this.api.request<TournamentDetailsPayload>(
        `/api/miniapp/tournaments/${encodeURIComponent(tournament.id)}?role=${tournament.role}`, { signal },
      );
      if (!signal?.aborted) this.showTournamentDetails(details);
    } catch (error) {
      if (signal?.aborted) return;
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.showTextDialog(this.i18n.t("route.library.title"), [this.i18n.t(`error.${code}`)]);
    }
  }

  private async accessLibrary(route: RouteMatch, version: string, command: "view" | "download", button?: HTMLButtonElement): Promise<void> {
    const signal = this.request?.signal;
    if (button) button.disabled = true;
    try {
      const path = `/api/miniapp/library/${encodeURIComponent(version)}/${command}`;
      let result = await this.api.request<LibraryAccess>(path, { method: "POST", body: { confirm: false }, signal });
      if (signal?.aborted) return;
      if (result.confirmation_required) {
        if (!window.confirm(this.i18n.t("library.confirm"))) {
          if (command === "view") this.router.navigate("/library", { replace: true });
          return;
        }
        result = await this.api.request<LibraryAccess>(path, { method: "POST", body: { confirm: true }, signal });
      }
      if (signal?.aborted) return;
      if ("pages" in result) {
        this.renderFrame(route, element("section", {},
          element("button", {
            type: "button", className: "secondary-button",
            onclick: (() => this.router.navigate("/library")) as EventListener,
          }, this.i18n.t("route.library.title")),
          renderLibraryReader(result.name, result.pages, this.i18n)));
      } else if ("queued" in result) {
        this.showTextDialog(this.i18n.t("library.download"), [this.i18n.t("library.queued")]);
      }
    } catch (error) {
      if (signal?.aborted) return;
      const code = error instanceof ApiError ? error.code : "internal_error";
      const message = code === "forbidden" ? this.i18n.t("library.unavailable") : this.i18n.t(`error.${code}`);
      if (command === "view") {
        this.renderFrame(route, element("section", {}, this.statusCard("error", message),
          element("button", { type: "button", onclick: (() => this.router.navigate("/library")) as EventListener }, this.i18n.t("route.library.title"))));
      } else this.showTextDialog(this.i18n.t("library.download"), [message]);
    } finally {
      if (button) button.disabled = false;
    }
  }

  private renderPacketEditor(route: RouteMatch, resource: PacketDraftResource): void {
    const modifying = Boolean(resource.assignment_id);
    const changes: Record<string, "correction" | "substitution"> = {};
    const fieldAuthors = { ...resource.field_author_ids };
    const managementPath = `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}`;
    const packet = structuredClone(resource.packet);
    const authorBindings = new Map(Object.entries(resource.author_bindings ?? {}));
    const associatedAuthors = new Map((resource.associated_authors ?? []).map((author) => [author.author_id, author]));
    let leadAuthorId = resource.lead_author_id;
    const authorsPath = modifying ? `${managementPath}/authors?return_author=true`
      : `/api/miniapp/manager/packets/${encodeURIComponent(route.params.launch_ref ?? "")}/authors`;
    let themeIndex = 0;
    const form = element("form", { className: "settings-form packet-editor" }) as HTMLFormElement;
    const label = (field: string): string => this.i18n.t(`packet_editor.${field}` as MessageKey);
    const valueAt = (source: unknown, path: string): unknown => path.split(".").reduce<unknown>(
      (value, key) => (value as Record<string, unknown>)[key], source,
    );
    const control = (field: string, value: string | number | null, multiline = false, path = field): HTMLElement => {
      const item = element(
        "label",
        {},
        label(field),
        multiline
          ? element("textarea", { "data-field": field, rows: "3" }, value == null ? "" : String(value))
          : element("input", {
              "data-field": field,
              type: field === "year" || field === "value" ? "number" : "text",
              value: value == null ? "" : String(value),
            }),
      );
      if (modifying) {
        const input = item.querySelector<HTMLInputElement | HTMLTextAreaElement>("[data-field]")!;
        input.disabled = !changes[path];
        let authorPicker: HTMLFieldSetElement | undefined;
        if (field === "author" || field === "lead_author") {
          authorPicker = element("fieldset", { disabled: !changes[path], className: "packet-field-author" },
            this.registeredAuthorPicker(route, authorsPath,
              associatedAuthors.get(fieldAuthors[path] ?? ""), (author) => {
                fieldAuthors[path] = author?.author_id ?? null;
                if (author) {
                  associatedAuthors.set(author.author_id, author);
                  input.value = author.display_name;
                } else input.value = "";
                input.dispatchEvent(new Event("change", { bubbles: true }));
              }),
          );
          input.addEventListener("input", () => { fieldAuthors[path] = null; });
        }
        const buttons = element("span", { className: "packet-change-actions" });
        const substitution = (path.startsWith("themes.") && field === "name")
          || (path.includes(".questions.") && ["text", "answer", "accepted_answers"].includes(field));
        for (const kind of substitution ? ["correction", "substitution"] as const : ["correction"] as const) {
          const button = element("button", {
            type: "button", className: `packet-change-button is-${kind}`,
            title: this.i18n.t(`packet_management.${kind}`),
            "aria-label": `${this.i18n.t(`packet_management.${kind}`)}: ${label(field)}`,
            "aria-pressed": String(changes[path] === kind),
          }, kind === "correction" ? "✎" : "↪");
          button.addEventListener("click", () => {
            if (changes[path] === kind) {
              const current = field === "accepted_answers"
                ? input.value.split(/\n/).map((answer) => answer.trim()).filter(Boolean)
                : field === "year" ? (input.value ? Number(input.value) : null)
                : field === "value" ? Number(input.value) : input.value;
              if (!input.validity.valid
                || JSON.stringify(current) !== JSON.stringify(valueAt(resource.packet, path))
                || fieldAuthors[path] !== resource.field_author_ids?.[path]) return;
              // Capture a restored value before locking the field, including after page navigation.
              input.dispatchEvent(new Event("change", { bubbles: true }));
              delete changes[path];
              input.disabled = true;
              if (authorPicker) authorPicker.disabled = true;
              button.setAttribute("aria-pressed", "false");
              return;
            }
            changes[path] = kind;
            input.disabled = false;
            if (authorPicker) authorPicker.disabled = false;
            for (const sibling of buttons.querySelectorAll("button")) sibling.setAttribute("aria-pressed", String(sibling === button));
            input.focus();
          });
          buttons.append(button);
        }
        item.prepend(buttons);
        if (authorPicker) item.append(authorPicker);
      }
      return item;
    };
    const diagnostics = (title: MessageKey, items: string[], kind: string): HTMLElement =>
      element(
        "section",
        { className: `packet-diagnostics packet-${kind}` },
        element("h2", {}, this.i18n.t(title)),
        items.length
          ? element("ul", { className: "detail-list" }, ...items.map((item) => element("li", {}, item)))
          : element("p", { className: "field-help" }, this.i18n.t("packet_editor.none")),
      );
    const metadata = element("fieldset", {}, element("legend", {}, this.i18n.t("packet_editor.metadata")));
    for (const field of resource.editor.packet_fields) {
      const value = packet[field as keyof typeof packet];
      if (field !== "themes") metadata.append(control(field, value as string | number | null));
    }
    const normalizeAuthor = (name: string): string => name.trim().replace(/\s+/g, " ");
    const packetAuthors = (): string[] => [...new Set([
      packet.lead_author,
      ...packet.themes.flatMap((theme) => [theme.author, ...theme.questions.map((question) => question.author)]),
    ].map(normalizeAuthor).filter(Boolean))];
    const authorList = element("div", { className: "packet-author-list" });
    const authorRows = new Map<string, HTMLElement>();
    const markChanged = (): void => { if (publishButton) publishButton.disabled = true; };
    const refreshAuthors = (): void => {
      const names = packetAuthors();
      for (const name of names) {
        if (authorRows.has(name)) continue;
        const row = element(
          "section",
          { className: "packet-author-association", "data-packet-author": name },
          element("h3", {}, name),
          this.registeredAuthorPicker(
            route, authorsPath,
            associatedAuthors.get(authorBindings.get(name) ?? ""),
            (author) => {
              if (author) {
                authorBindings.set(name, author.author_id);
                associatedAuthors.set(author.author_id, author);
              } else authorBindings.delete(name);
              markChanged();
            },
          ),
        );
        authorRows.set(name, row);
      }
      replaceChildren(authorList, ...names.map((name) => authorRows.get(name)!));
    };
    const leadInput = metadata.querySelector<HTMLInputElement>("[data-field=lead_author]");
    if (leadInput && !modifying) {
      const leadPicker = this.registeredAuthorPicker(
        route, authorsPath, associatedAuthors.get(leadAuthorId ?? ""),
        (author) => {
          leadAuthorId = author?.author_id ?? null;
          if (author) {
            associatedAuthors.set(author.author_id, author);
            leadInput.value = author.display_name;
            packet.lead_author = author.display_name;
          }
          refreshAuthors();
          markChanged();
        },
      );
      leadInput.parentElement?.after(leadPicker);
      leadInput.addEventListener("input", () => {
        leadAuthorId = null;
        packet.lead_author = leadInput.value;
        const selected = leadPicker.querySelector<HTMLSelectElement>("select");
        if (selected) selected.value = "";
      });
      leadInput.addEventListener("change", refreshAuthors);
    }
    if (!modifying) metadata.append(
      element("h2", {}, this.i18n.t("packet_editor.authors")),
      element("p", { className: "field-help" }, this.i18n.t("packet_editor.authors_help")),
      authorList,
    );
    const page = element("section", { className: "packet-page" });
    const decide = async (decision: "publish" | "reject"): Promise<void> => {
      if (!window.confirm(this.i18n.t(`packet_editor.${decision}_confirm` as MessageKey))) return;
      try {
        const result = await this.api.request<{ status: string }>(
          `/api/miniapp/manager/packets/${encodeURIComponent(route.params.launch_ref ?? "")}/${decision}`,
          { method: "POST", body: {} },
        );
        const resolvedDecision = result.status === "published" ? "publish" : "reject";
        this.platform.notifySuccess();
        this.renderFrame(
          route,
          this.statusCard("empty", this.i18n.t(`packet_editor.${resolvedDecision}ed` as MessageKey)),
        );
      } catch (error) {
        const code = error instanceof ApiError ? error.code : "internal_error";
        this.platform.notifyError();
        if (code === "stale_write") await this.load(route);
        else this.showTextDialog(this.i18n.t(`error.${code}`), []);
      }
    };
    const publishButton = resource.can_publish
      ? element("button", { type: "button", className: "primary-button", onclick: (() => void decide("publish")) as EventListener }, this.i18n.t("packet_editor.publish")) as HTMLButtonElement
      : null;
    const capturePage = (): void => {
      const theme = packet.themes[themeIndex];
      if (!theme) return;
      for (const input of page.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-theme-field]")) {
        if (modifying && input.disabled) continue;
        theme[input.dataset.themeField as "name" | "author"] = input.value;
      }
      for (const input of page.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("[data-question-field]")) {
        if (modifying && input.disabled) continue;
        const question = theme.questions[Number(input.dataset.questionIndex)];
        if (!question) continue;
        const field = input.dataset.questionField as keyof typeof question;
        if (field === "value") question.value = Number(input.value);
        else if (field === "accepted_answers") {
          question.accepted_answers = input.value.split(modifying ? /\n/ : /\n|,/)
            .map((item) => item.trim()).filter(Boolean);
        } else {
          (question[field] as string) = input.value;
        }
      }
      if (!modifying) refreshAuthors();
    };
    page.addEventListener("change", capturePage);
    const renderPage = (): void => {
      const theme = packet.themes[themeIndex];
      if (!theme) {
        replaceChildren(page, this.statusCard("empty", this.i18n.t("packet_editor.no_themes")));
        return;
      }
      const previous = element("button", { type: "button", className: "secondary-button", disabled: themeIndex === 0 }, this.i18n.t("common.previous"));
      const next = element("button", { type: "button", className: "secondary-button", disabled: themeIndex >= packet.themes.length - 1 }, this.i18n.t("common.next"));
      previous.addEventListener("click", () => { capturePage(); themeIndex -= 1; renderPage(); });
      next.addEventListener("click", () => { capturePage(); themeIndex += 1; renderPage(); });
      const themeFields = element("fieldset", {}, element("legend", {}, `${this.i18n.t("packet_editor.theme")} ${themeIndex + 1} / ${packet.themes.length}`));
      for (const field of resource.editor.theme_fields) {
        const item = control(field, theme[field as "name" | "author"], false, `themes.${themeIndex}.${field}`);
        const input = item.querySelector<HTMLInputElement>("[data-field]");
        if (input) { input.dataset.themeField = field; delete input.dataset.field; }
        themeFields.append(item);
      }
      const questions = element("div", { className: "packet-questions" });
      theme.questions.forEach((question, questionIndex) => {
        const fields = element("fieldset", {}, element("legend", {}, `${this.i18n.t("packet_editor.question")} ${questionIndex + 1}`));
        for (const field of resource.editor.question_fields) {
          const current = field === "accepted_answers" ? question.accepted_answers.join("\n") : question[field as keyof typeof question];
          const item = control(field, current as string | number, ["text", "answer", "accepted_answers", "commentary", "source"].includes(field), `themes.${themeIndex}.questions.${questionIndex}.${field}`);
          const input = item.querySelector<HTMLInputElement | HTMLTextAreaElement>("[data-field]");
          if (input) {
            input.dataset.questionField = field;
            input.dataset.questionIndex = String(questionIndex);
            delete input.dataset.field;
          }
          fields.append(item);
        }
        questions.append(fields);
      });
      replaceChildren(
        page,
        element("nav", { className: "packet-page-nav" }, previous, element("strong", {}, theme.name || `${this.i18n.t("packet_editor.theme")} ${themeIndex + 1}`), next),
        themeFields,
        questions,
      );
    };
    form.append(
      diagnostics("packet_editor.errors", resource.errors, "errors"),
      diagnostics("packet_editor.warnings", resource.warnings, "warnings"),
      metadata,
      page,
      element(
        "div",
        { className: "settings-actions" },
        element("button", { type: "submit", className: "primary-button" }, this.i18n.t(modifying ? "packet_management.save" : "packet_editor.save")),
        modifying ? element("button", { type: "button", className: "secondary-button", onclick: (() => void this.load(route)) as EventListener }, this.i18n.t("common.back")) : null,
        publishButton,
        resource.can_reject
          ? element("button", { type: "button", className: "danger-button", onclick: (() => void decide("reject")) as EventListener }, this.i18n.t("packet_editor.reject"))
          : null,
      ),
    );
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      capturePage();
      for (const input of metadata.querySelectorAll<HTMLInputElement>("[data-field]")) {
        if (modifying && input.disabled) continue;
        const field = input.dataset.field;
        if (field === "year") packet.year = input.value ? Number(input.value) : null;
        else if (field && field !== "themes") (packet[field as "name" | "language" | "lead_author"] as string) = input.value;
      }
      try {
        if (modifying) {
          const unchanged = Object.keys(changes).some((path) =>
            JSON.stringify(valueAt(resource.packet, path)) === JSON.stringify(valueAt(packet, path))
            && fieldAuthors[path] === resource.field_author_ids?.[path]);
          if (!Object.keys(changes).length || unchanged) {
            this.showTextDialog(this.i18n.t("packet_management.changes_required"), []);
            return;
          }
          const updated = await this.api.request<TournamentManagerManagementResource>(
            `${managementPath}/packets/${encodeURIComponent(resource.assignment_id!)}/save`,
            { method: "POST", body: { expected_version: resource.version, content: packet,
              changes, field_author_ids: fieldAuthors } },
          );
          this.platform.notifySuccess();
          this.renderManagerManagement(route, { ...updated, kind: "manager_management", state: "ready" });
          return;
        }
        const updated = await this.api.request<PacketDraftResource>(
          `/api/miniapp/manager/packets/${encodeURIComponent(route.params.launch_ref ?? "")}`,
          { method: "POST", body: {
            expected_version: resource.version, content: packet,
            author_bindings: Object.fromEntries(packetAuthors()
              .filter((name) => authorBindings.has(name))
              .map((name) => [name, authorBindings.get(name)])),
            lead_author_id: leadAuthorId ?? null,
          } },
        );
        this.platform.notifySuccess();
        this.renderPacketEditor(route, { ...updated, kind: "packet_draft", state: "ready" });
      } catch (error) {
        const code = error instanceof ApiError ? error.code : "internal_error";
        if (code === "stale_write") await this.load(route);
        else this.showTextDialog(this.i18n.t(`error.${code}`), []);
      }
    });
    form.addEventListener("input", (event) => {
      if (event.target instanceof HTMLInputElement && event.target.type === "search") return;
      if (publishButton) publishButton.disabled = true;
    });
    if (!modifying) refreshAuthors();
    renderPage();
    this.renderFrame(route, form);
  }

  private renderLobby(route: RouteMatch, lobby: LobbyResource): void {
    if (lobby.game_id) {
      this.renderFrame(route, this.statusCard("empty", this.i18n.t("lobby.game_assigned")));
      this.platform.setMainAction(this.i18n.t("common.return_to_bot"), () => this.platform.returnToBot());
      return;
    }
    if (lobby.state === "empty" || lobby.status !== "assembling") {
      this.renderFrame(route, this.statusCard("empty", this.i18n.t(route.emptyKey)));
      return;
    }
    const can = (action: string): boolean => lobby.available_actions.includes(action);
    let dirty = false;
    let busy = false;
    const section = route.query.get("section") ?? "overview";
    const mutate = async (command: string, body: Record<string, unknown> = {}): Promise<void> => {
      if (busy) return;
      busy = true;
      try {
        await this.api.request(
          `/api/miniapp/lobbies/${encodeURIComponent(route.params.launch_ref ?? "")}/${command}`,
          { method: "POST", body: { expected_version: lobby.version, ...body } },
        );
        this.platform.notifySuccess();
        if (command === "leave" || command === "cancel") this.platform.returnToBot();
        else await this.load(route);
      } catch (error) {
        const code = error instanceof ApiError ? error.code : "internal_error";
        if (code === "stale_write") await this.load(route);
        else {
          this.platform.notifyError();
          this.showTextDialog(this.i18n.t(`error.${code}`), []);
        }
      } finally {
        busy = false;
      }
    };
    const members = element(
      "ul", { className: "detail-list" },
      ...lobby.members.map((member) => element(
        "li", {}, `${member.display_name} · ${this.i18n.t(member.role === "observer" ? "lobby.observer" : "lobby.player")} · ${member.ready ? this.i18n.t("lobby.ready") : this.i18n.t("lobby.not_ready")}`,
      )),
    );
    const packetFilters = this.lobbyPacketFilters.get(lobby.id) ?? {};
    this.lobbyPacketFilters.set(lobby.id, packetFilters);
    const packetList = renderLobbyPackets(lobby, this.i18n, packetFilters, section === "packets", mutate);
    const descriptors = lobby.setting_descriptors ?? Object.entries(lobby.settings).map(([name, value]) => ({
      name, value, value_type: typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : "string",
      description_key: name, options: [],
    }));
    const editable = descriptors.filter((item) => can("settings_update") && lobby.mutable_parameters.includes(item.name));
    const settings = element("form", { className: "settings-form" });
    settings.addEventListener("input", () => { dirty = true; });
    if (editable.length) {
      settings.append(this.renderDescriptorGroup(editable, "setting"));
      settings.append(element("button", { type: "submit", className: "primary-button" }, this.i18n.t("lobby.settings_save")));
      settings.addEventListener("submit", (event) => {
        event.preventDefault();
        try {
          const values = this.descriptorValues(settings, editable, "setting");
          const changes = Object.fromEntries(Object.entries(values).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(lobby.settings[key])));
          if (Object.keys(changes).length) void mutate("settings", { changes });
        } catch {
          this.showTextDialog(this.i18n.t("error.validation_failed"), []);
        }
      });
    }
    const currentSettings = element("div", { className: "resource-card" },
      ...descriptors.map((item) => this.detail(this.descriptorLabel(item.name, "setting"),
        typeof item.value === "boolean" ? this.i18n.t(item.value ? "common.enabled" : "common.disabled") : JSON.stringify(item.value))));
    const tabs = element("nav", { className: "settings-actions", "aria-label": this.i18n.t("lobby.sections") });
    for (const [value, key] of [["overview", "lobby.overview"], ["packets", "lobby.packets"], ["settings", "lobby.options"]] as const) {
      const query = new URLSearchParams(route.query);
      query.set("section", value);
      tabs.append(element("button", {
        type: "button", className: section === value ? "primary-button" : "secondary-button",
        onclick: (() => this.router.navigate(`${route.path}?${query}`)) as EventListener,
      }, this.i18n.t(key)));
    }
    const actions = element("div", { className: "settings-actions" });
    const action = (capability: string, command: string, key: MessageKey, body: Record<string, unknown> = {}, dangerous = false): void => {
      if (!can(capability)) return;
      actions.append(element("button", {
        type: "button", className: dangerous ? "danger-button" : "primary-button",
        onclick: (() => {
          if (!dangerous || window.confirm(this.i18n.t("lobby.confirm"))) void mutate(command, body);
        }) as EventListener,
      }, this.i18n.t(key)));
    };
    action("ready", "ready", "lobby.action.ready", { ready: true });
    action("unready", "ready", "lobby.action.unready", { ready: false });
    action("role_player", "role", "lobby.action.play", { role: "player" });
    action("role_observer", "role", "lobby.action.observe", { role: "observer", confirm_fresh: true }, true);
    action("search_start", "search-start", "lobby.action.search_start");
    action("search_cancel", "search-cancel", "lobby.action.search_cancel");
    action("start", "start", "lobby.action.start");
    action("leave", "leave", "lobby.action.leave", {}, true);
    action("cancel", "cancel", "lobby.action.cancel", {}, true);
    const violationKeys: Record<string, MessageKey> = {
      insufficient_fresh_content: "lobby.error.fresh",
      packet_not_playable: "lobby.error.packet",
      packet_content_incompatible: "lobby.error.content",
      ruleset_player_limit_exceeded: "lobby.error.players",
      tournament_stage_closed: "lobby.error.closed",
      tournament_capacity_restriction: "lobby.error.players",
      tournament_packet_limit_exceeded: "lobby.error.packet_count",
      tournament_membership_required: "lobby.error.membership",
      classic_participants_required: "classic.participants_required",
    };
    const violations = lobby.validation_violations.length
      ? element("aside", { className: "lobby-warnings", role: "alert" },
        element("h3", {}, this.i18n.t("lobby.errors")),
        element("ul", {}, ...lobby.validation_violations.map((item) => {
          const key = item.code === "packet_not_playable" && item.details?.reason === "missing"
            ? "lobby.error.packet_required" : violationKeys[item.code];
          return element("li", {}, key ? this.i18n.t(key)
            : `${this.i18n.t("lobby.error.other")} (${item.code})`);
        })))
      : null;
    this.renderFrame(route, element(
      "section", { className: "route-content lobby-content" }, tabs,
      element("h2", { className: "lobby-tournament-title" }, lobby.tournament_name ?? ""),
      section === "packets" ? element("section", {}, element("h2", {}, this.i18n.t("lobby.packets")), packetList) :
      section === "settings" ? element("section", {}, element("h2", {}, this.i18n.t("lobby.options")), currentSettings, settings) :
      element("section", { className: "route-content lobby-overview" },
        lobby.invitation_url ? element("a", { href: lobby.invitation_url, className: "resource-card lobby-invitation-link" },
          this.i18n.t("lobby.invitation"), ": ", lobby.invitation_url) : this.detail(this.i18n.t("lobby.invitation"), lobby.invitation_code),
        this.detail(this.i18n.t("lobby.capacity"), `${lobby.members.filter((member) => member.role === "player").length}/${lobby.max_players}`),
        element("h2", {}, this.i18n.t("lobby.members")), members,
        element("h2", {}, this.i18n.t("lobby.packets")), packetList,
        element("h2", {}, this.i18n.t("lobby.options")), currentSettings,
        actions, violations,
      ),
    ));
    const watchEvents = async (): Promise<void> => {
      try {
        const feed = await this.api.request<{ items: Array<{ sequence: number }> }>(
          `/api/miniapp/lobbies/${encodeURIComponent(route.params.launch_ref ?? "")}/events?after=${lobby.last_event_sequence}`,
        );
        if (feed.items.length && !dirty && !busy) {
          await this.load(route);
          return;
        }
      } catch {
        // The full snapshot reconciliation below remains authoritative.
      }
      this.eventTimer = window.setTimeout(() => void watchEvents(), Math.max(2, lobby.poll_after_seconds) * 1000);
    };
    this.eventTimer = window.setTimeout(() => void watchEvents(), Math.max(2, lobby.poll_after_seconds) * 1000);
    const reconcile = (): void => {
      if (!dirty && !busy) void this.load(route);
      else this.pollTimer = window.setTimeout(reconcile, 30_000);
    };
    this.pollTimer = window.setTimeout(reconcile, 30_000);
  }

  private renderTournamentRoute(
    route: RouteMatch,
    resource: TournamentRouteResource,
    page?: RoutePayload["pagination"],
  ): void {
    if (resource.state === "empty" || resource.items.length === 0) {
      this.renderFrame(route, this.statusCard("empty", this.i18n.t(route.emptyKey)));
      return;
    }
    const list = element("ul", { className: "resource-list tournament-list" });
    for (const item of resource.items) {
      const badges = element(
        "div",
        { className: "tournament-badges" },
        this.badge(this.i18n.t(`tournament.phase.${item.phase}`)),
        this.badge(this.i18n.t(`tournament.visibility.${item.visibility}`)),
        this.badge(
          this.i18n.t(
            item.registration_open
              ? "tournament.registration.open"
              : "tournament.registration.closed",
          ),
        ),
      );
      if (item.membership_status) {
        const membershipKey = `tournament.membership.${item.membership_status}` as MessageKey;
        badges.append(this.badge(this.i18n.t(membershipKey), "accent"));
      }
      if (item.managed) badges.append(this.badge(this.i18n.t("tournament.managed"), "accent"));
      if (resource.role === "manager") {
        badges.append(
          this.badge(
            this.i18n.t(
              item.finalized_at ? "tournament.setup.finalized" : "tournament.setup.unfinalized",
            ),
            item.finalized_at ? "accent" : "warning",
          ),
        );
      }
      const actions = element("div", { className: "tournament-actions" });
      for (const action of item.available_actions) {
        if (!isTournamentAction(action)) continue;
        const button = element(
          "button",
          {
            type: "button",
            className: action === "register" || action.startsWith("select_") ? "primary-button" : "secondary-button",
            onclick: ((event: Event) => {
              const target = event.currentTarget as HTMLButtonElement;
              void this.handleTournamentAction(route, resource, item, action, target);
            }) as EventListener,
          },
          this.i18n.t(`tournament.action.${action}`),
        );
        actions.append(button);
      }
      list.append(
        element(
          "li",
          {},
          element(
            "article",
            { className: "resource-card tournament-card" },
            element("div", { className: "tournament-heading" }, element("h2", {}, item.name), element("code", {}, item.slug)),
            badges,
            element(
              "p",
              { className: "tournament-summary" },
              `${item.type_key} · ${item.ruleset_key} · ${this.formatDate(item.starts_at)}`,
            ),
            actions,
          ),
        ),
      );
    }
    const content = element(
      "section",
      { className: "route-content" },
      element("p", { className: "resource-summary" }, `${resource.items.length} / ${resource.total}`),
      list,
    );
    if (page?.previous || page?.next) content.append(this.pagination(route, page));
    this.renderFrame(route, content);
    queueMicrotask(() => document.querySelector<HTMLElement>("#page-title")?.focus());
  }

  private renderManagerManagement(
    route: RouteMatch,
    resource: TournamentManagerManagementResource,
  ): void {
    const can = (action: string): boolean => resource.available_actions.includes(action);
    const content = element("div", { className: "management-sections" });
    const panels = new Map<string, HTMLElement>();
    const section = (name: string, ...children: Node[]): void => {
      if (!resource.sections.includes(name as TournamentManagerManagementResource["sections"][number])) return;
      panels.set(name, element(
        "section",
        { className: "management-section", "data-section": name },
        element("h2", {}, this.i18n.t(`manager_management.${name}` as MessageKey)),
        ...children,
      ));
    };

    const counts = element(
      "dl",
      { className: "management-stats" },
      ...([
        ["registered_count", resource.registration_count],
        ["approved_count", resource.approved_count],
        ["participant_count", resource.participant_count],
        ["packet_count", resource.packet_count],
      ] as const).flatMap(([key, value]) => [
        element("div", { className: "management-stat" },
          element("dt", {}, this.i18n.t(`manager_management.${key}`)),
          element("dd", {}, String(value)),
        ),
      ]),
    );
    const registrationSwitch = element("input", {
      type: "checkbox",
      role: "switch",
      checked: resource.registration_open,
      disabled: !can("registration_override"),
    });
    registrationSwitch.addEventListener("change", () => {
      void this.mutateManagerManagement(
        route,
        "/registration-availability",
        {
          expected_version: resource.settings_version,
          registration_open: (registrationSwitch as HTMLInputElement).checked,
        },
      );
    });
    const finalized = resource.finalized_at != null;
    const finished = resource.tournament.status === "completed";
    const finalizeButton = element(
      "button",
      {
        type: "button",
        className: "danger-button",
        disabled: finalized || !can("finalize"),
        onclick: (() => {
          if (window.confirm(this.i18n.t("manager_management.finalize_confirm"))) {
            void this.mutateManagerManagement(
              route,
              "/finalize",
              { expected_version: resource.settings_version },
              true,
            );
          }
        }) as EventListener,
      },
      this.i18n.t(finalized ? "manager_management.finalized" : "manager_management.finalize"),
    );
    const finishButton = element(
      "button",
      {
        type: "button",
        className: "danger-button",
        disabled: finished || !can("mark_finished"),
        onclick: (() => {
          if (window.confirm(this.i18n.t("manager_management.finish_confirm"))) {
            void this.mutateManagerManagement(
              route,
              "/complete",
              { expected_version: resource.settings_version },
            );
          }
        }) as EventListener,
      },
      this.i18n.t(finished ? "manager_management.finished" : "manager_management.mark_finished"),
    );
    section(
      "general",
      counts,
      this.managerNavigationButton(route, "settings"),
      element(
        "div",
        { className: "management-control" },
        element("label", { className: "switch-label" }, registrationSwitch, element("span", {}, this.i18n.t("manager_management.registration_available"))),
        element("p", { className: "field-help" }, this.i18n.t("manager_management.registration_manual")),
        element("p", { className: "field-help" }, this.i18n.t(
          resource.registration_scheduled_open
            ? "manager_management.registration_scheduled_open"
            : "manager_management.registration_scheduled_closed",
        )),
      ),
      element("div", { className: "settings-actions" }, finalizeButton, finishButton),
    );
    if (resource.classic) {
      for (const kind of ["first", "playoff"] as const) {
        const stage = resource.classic.stages.find((s) => s.kind === kind);
        panels.get("general")?.append(element("button", {
          type: "button", className: "primary-button",
          disabled: !finalized || finished || !stage || stage.stage_type === "none" || !!stage.started_at,
          onclick: (() => void this.mutateClassic(route, resource.settings_version, "start", kind, {})) as EventListener,
        }, this.i18n.t(kind === "first" ? "classic.start_first" : "classic.start_playoff")));
        section(kind === "first" ? "first_stage" : "playoff_stage",
          this.classicRounds(route, resource, stage));
      }
      section("first_round_seeding", this.classicSeeding(route, resource));
    }

    const registrationList = resource.registrations.length
      ? element(
          "ul",
          { className: "resource-list registration-list" },
          ...resource.registrations.map((registration) => {
            const actions = element("div", { className: "inline-actions" });
            for (const decision of registration.available_actions) {
              actions.append(element(
                "button",
                {
                  type: "button",
                  className: decision === "reject" ? "danger-button compact-button" : "primary-button compact-button",
                  onclick: (() => void this.mutateManagerManagement(
                    route,
                    `/registrations/${encodeURIComponent(registration.player_id)}`,
                    { decision },
                  )) as EventListener,
                },
                this.i18n.t(`manager_management.${decision}`),
              ));
            }
            return element(
              "li",
              {},
              element(
                "article",
                { className: "resource-card registration-card" },
                element("div", {}, element("strong", {}, registration.display_name),
                  registration.real_name && registration.real_name !== registration.display_name
                    ? element("p", { className: "field-help" }, registration.real_name)
                    : null,
                  element("p", { className: "field-help" }, this.formatDate(registration.registered_at)),
                ),
                this.badge(this.i18n.t(`tournament.membership.${registration.status}` as MessageKey)),
                actions,
              ),
            );
          }),
        )
      : this.statusCard("empty", this.i18n.t("manager_management.registrations_empty"));
    section("registrations", registrationList);

    const packetAccessibility = element("div", { className: "packet-accessibility" });
    if (!resource.packets.length) {
      packetAccessibility.append(this.statusCard("empty", this.i18n.t("manager_management.packets_empty")));
    } else {
      const picker = element(
        "select",
        { "aria-label": this.i18n.t("manager_management.packet_select") },
        ...resource.packets.map((packet) => element(
          "option",
          { value: packet.assignment_id },
          packet.version ? `${packet.name} · v${packet.version}` : packet.name,
        )),
      );
      const tableContainer = element("div", { className: "packet-access-table-wrap" });
      const renderPacket = (packet: ManagementPacket): void => {
        replaceChildren(tableContainer, this.packetAccessTable(route, packet, can("packet_access"), resource.tournament.type_key === "classic"));
      };
      picker.addEventListener("change", () => {
        const selected = resource.packets.find((packet) => packet.assignment_id === picker.value);
        if (selected) renderPacket(selected);
      });
      packetAccessibility.append(
        element("label", { className: "packet-picker-label" }, this.i18n.t("manager_management.packet_select"), picker),
        tableContainer,
        element("p", { className: "field-help" }, this.i18n.t("manager_management.readable_help")),
      );
      renderPacket(resource.packets[0]!);
    }
    section("packet_accessibility", packetAccessibility);
    const managedPackets = element("div", { className: "lobby-packets" });
    for (const packet of resource.packets) {
      const card = element("article", { className: "resource-card lobby-packet-card", "data-packet-id": packet.packet_id },
        element("h3", {}, packet.name),
        element("dl", { className: "lobby-packet-details" },
          ...([
            ["packet_management.packet_id", packet.packet_id],
            ["packet_management.packet_version_id", packet.packet_version_id ?? "—"],
            ["lobby.packet_year", packet.year?.toString() ?? "—"],
            ["lobby.packet_publication_year", packet.published_at?.slice(0, 4) || "—"],
            ["lobby.packet_lead_author", packet.lead_author || "—"],
            ["lobby.packet_authors", packet.authors?.join(", ") || "—"],
          ] as const).map(([key, value]) => element("div", { className: "lobby-packet-detail" },
            element("dt", {}, this.i18n.t(key)), element("dd", {}, value))),
        ),
      );
      const actions = element("div", { className: "inline-actions" });
      if (can("packet_management")) for (const command of ["modify", "release", "delete"] as const) {
        const button = element("button", {
          type: "button", className: command === "delete" ? "danger-button" : "secondary-button",
          disabled: command === "release" && packet.released,
        }, this.i18n.t(packet.released && command === "release" ? "packet_management.released" : `packet_management.${command}`));
        button.addEventListener("click", async () => {
          if (command === "delete" && !window.confirm(this.i18n.t("packet_management.delete_confirm"))) return;
          if (command === "release" && !window.confirm(this.i18n.t("packet_management.release_confirm"))) return;
          const path = `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/packets/${encodeURIComponent(packet.assignment_id)}`;
          button.disabled = true;
          try {
            if (command === "modify") {
              const editor = await this.api.request<PacketDraftResource>(path);
              this.renderPacketEditor(route, editor);
            } else {
              const updated = await this.api.request<TournamentManagerManagementResource>(`${path}/${command}`, {
                method: "POST", body: { expected_version: packet.version },
              });
              this.platform.notifySuccess();
              this.renderManagerManagement(route, { ...updated, kind: "manager_management", state: "ready" });
            }
          } catch (error) {
            const code = error instanceof ApiError ? error.code : "internal_error";
            if (code === "stale_write") await this.load(route);
            else this.showTextDialog(this.i18n.t(`error.${code}`), []);
          } finally { button.disabled = false; }
        });
        actions.append(button);
      }
      card.append(actions);
      managedPackets.append(card);
    }
    if (!resource.packets.length) managedPackets.append(this.statusCard("empty", this.i18n.t("manager_management.packets_empty")));
    section("packet_management", managedPackets);

    const sectionNavigation = element(
      "nav",
      {
        className: "management-section-nav",
        "aria-label": this.i18n.t("route.manager_management.title"),
      },
    );
    const sectionContainer = element("div", { className: "management-section-container" });
    const navigationButtons = new Map<string, HTMLButtonElement>();
    const showSection = (
      name: TournamentManagerManagementResource["sections"][number],
    ): void => {
      const panel = panels.get(name);
      if (!panel) return;
      this.managerSection = { tournamentRef: route.params.launch_ref, name };
      replaceChildren(sectionContainer, panel);
      for (const [sectionName, button] of navigationButtons) {
        const active = sectionName === name;
        button.classList.toggle("active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
    };
    for (const name of resource.sections) {
      if (!panels.has(name)) continue;
      const button = element(
        "button",
        {
          type: "button",
          onclick: (() => showSection(name)) as EventListener,
        },
        this.i18n.t(`manager_management.${name}`),
      );
      navigationButtons.set(name, button);
      sectionNavigation.append(button);
    }
    const savedSection = this.managerSection;
    const previousSection = savedSection && savedSection.tournamentRef === route.params.launch_ref
      ? savedSection.name
      : undefined;
    const initialSection = previousSection && panels.has(previousSection)
      ? previousSection
      : resource.sections.find((name) => panels.has(name));
    if (initialSection) showSection(initialSection);
    content.append(sectionNavigation, sectionContainer);
    this.renderFrame(route, content);
    queueMicrotask(() => document.querySelector<HTMLElement>("#page-title")?.focus());
  }

  private packetAccessTable(
    route: RouteMatch,
    packet: ManagementPacket,
    enabled: boolean,
    classic = false,
  ): HTMLElement {
    const rights: Array<"playable" | "discoverable" | "readable"> = classic ? ["readable"] : ["playable", "discoverable", "readable"];
    const table = element("table", { className: "packet-access-table" });
    const head = element("thead", {}, element(
      "tr",
      {},
      element("th", { scope: "col" }, ""),
      ...rights.map((right) => element("th", { scope: "col" }, this.i18n.t(`manager_management.${right}`))),
    ));
    const body = element("tbody");
    const accessCheckbox = (
      right: typeof rights[number],
      checked: boolean,
      playerId?: string,
      indeterminate = false,
    ): HTMLInputElement => {
      const checkbox = element("input", {
        type: "checkbox",
        checked,
        disabled: !enabled,
        "aria-label": this.i18n.t(`manager_management.${right}`),
      });
      checkbox.indeterminate = indeterminate;
      checkbox.addEventListener("change", () => void this.mutateManagerManagement(
        route,
        "/packet-access",
        {
          assignment_id: packet.assignment_id,
          player_id: playerId ?? null,
          right,
          enabled: checkbox.checked,
        },
      ));
      return checkbox;
    };
    body.append(element(
      "tr",
      { className: "set-all-row" },
      element("th", { scope: "row" }, this.i18n.t("manager_management.set_for_all")),
      ...rights.map((right) => {
        const selected = packet.player_access.filter((player) => player[right]).length;
        return element("td", {}, accessCheckbox(
          right,
          packet.player_access.length > 0 && selected === packet.player_access.length,
          undefined,
          selected > 0 && selected < packet.player_access.length,
        ));
      }),
    ));
    for (const player of packet.player_access) {
      body.append(element(
        "tr",
        {},
        element("th", { scope: "row" }, player.display_name),
        ...rights.map((right) => element("td", {}, accessCheckbox(right, player[right], player.player_id))),
      ));
    }
    if (!packet.player_access.length) {
      body.append(element("tr", {}, element("td", { colspan: String(rights.length + 1), className: "empty-table-cell" }, this.i18n.t("manager_management.players_empty"))));
    }
    table.append(head, body);
    return table;
  }

  private async mutateManagerManagement(
    route: RouteMatch,
    suffix: string,
    body: Record<string, unknown>,
    reload = false,
  ): Promise<void> {
    try {
      const response = await this.api.request<TournamentManagerManagementResource>(
        `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}${suffix}`,
        { method: "POST", body },
      );
      this.platform.notifySuccess();
      if (reload) await this.load(route);
      else this.renderManagerManagement(route, { ...response, kind: "manager_management", state: "ready" });
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.platform.notifyError();
      if (code === "stale_write") await this.load(route);
      else {
        await this.load(route);
        this.showTextDialog(this.i18n.t(`error.${code}`), []);
      }
    }
  }

  private renderManagerSettings(
    route: RouteMatch,
    resource: TournamentManagerSettingsResource,
  ): void {
    const item = resource.tournament;
    const canChangeCompetition = resource.available_actions.includes("update_competition");
    const inferredType = (value: unknown): ManagerSettingDescriptor["value_type"] =>
      Array.isArray(value) ? "array" : typeof value === "boolean" ? "boolean" : Number.isInteger(value) ? "integer" : typeof value === "number" ? "number" : "string";
    const settingDescriptors = resource.setting_descriptors ?? Object.entries(resource.default_parameters).map(([name, value]) => ({
      name, value, value_type: inferredType(value), description_key: `ruleset.${item.ruleset_key}.${name}.description`, options: [],
    }));
    const policyDescriptors = resource.policy_descriptors ?? Object.entries(resource.policies).filter(([name]) => name !== "ruleset_rating_weight").map(([name, value]) => ({
      name, value, value_type: inferredType(value), description_key: `policy.${name}.description`, options: [],
    }));
    const form = element("form", { className: "settings-form" });
    const input = (
      label: MessageKey,
      name: string,
      value: string,
      type = "text",
      disabled = false,
    ): HTMLElement => element(
      "label",
      {},
      this.i18n.t(label),
      element("input", { name, type, value, disabled }),
    );
    const select = (
      label: MessageKey,
      name: string,
      values: string[],
      value: string,
      disabled = false,
    ): HTMLElement => element(
      "label",
      {},
      this.i18n.t(label),
      element(
        "select",
        { name, disabled },
        ...values.map((option) => element("option", { value: option, selected: option === value }, option)),
      ),
    );
    const group = (legend: MessageKey, ...children: Node[]): HTMLElement => element(
      "fieldset", {}, element("legend", {}, this.i18n.t(legend)), ...children,
    );

    const authors = element("div", { className: "repeating-list", id: "selected-authors" });
    const appendAuthor = (id: string, displayName: string): void => {
      if (Array.from(authors.querySelectorAll<HTMLElement>("[data-author-id]")).some(
        (row) => row.dataset.authorId === id,
      )) return;
      const row = element(
        "div",
        { className: "repeating-row", "data-author-id": id },
        element("input", { type: "hidden", name: "author_ids", value: id }),
        element("span", {}, displayName),
        element(
          "button",
          { type: "button", className: "secondary-button" },
          this.i18n.t("common.remove"),
        ),
      );
      row.querySelector("button")?.addEventListener("click", () => row.remove());
      authors.append(row);
    };
    for (const author of resource.authors ?? []) appendAuthor(author.id, author.display_name);

    const authorSearch = element("input", {
      type: "search",
      placeholder: this.i18n.t("manager_settings.author_search_placeholder"),
      "aria-label": this.i18n.t("manager_settings.author_search"),
    });
    const authorResults = element("select", {
      "aria-label": this.i18n.t("manager_settings.author_results"),
    });
    let authorSearchTimer: number | undefined;
    authorSearch.addEventListener("input", () => {
      if (authorSearchTimer !== undefined) window.clearTimeout(authorSearchTimer);
      authorSearchTimer = window.setTimeout(async () => {
        try {
          const result = await this.api.request<{
            items: Array<{ author_id: string; display_name: string }>;
          }>(
            `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/authors?query=${encodeURIComponent(authorSearch.value)}`,
          );
          replaceChildren(
            authorResults,
            ...result.items.map((author) => element(
              "option",
              { value: author.author_id },
              author.display_name,
            )),
          );
        } catch (error) {
          this.platform.notifyError();
        }
      }, 200);
    });
    const addExistingAuthor = element(
      "button",
      { type: "button", className: "secondary-button" },
      this.i18n.t("manager_settings.author_add"),
    );
    addExistingAuthor.addEventListener("click", () => {
      const option = authorResults.selectedOptions[0];
      if (option) appendAuthor(option.value, option.textContent ?? option.value);
    });
    const addNewAuthor = element(
      "button",
      { type: "button", className: "secondary-button" },
      this.i18n.t("manager_settings.author_add_new"),
    );
    addNewAuthor.addEventListener("click", () => this.showNewAuthorDialog(route));

    const pricingPlans = element("div", { className: "repeating-list", id: "pricing-plans" });
    const appendPricingPlan = (
      name = "",
      prices: Array<{ amount: number | string; currency: string }> = [{ amount: "", currency: "" }],
    ): void => {
      const priceList = element("div", { className: "pricing-price-list" });
      const appendPrice = (amount: number | string = "", currency = ""): void => {
        const row = element(
          "div",
          { className: "pricing-price-row" },
          element("input", { name: "pricing_amount", type: "number", min: "1", step: "1", value: String(amount), placeholder: this.i18n.t("manager_settings.pricing_amount") }),
          element("input", { name: "pricing_currency", value: currency, maxlength: "3", placeholder: this.i18n.t("manager_settings.pricing_currency") }),
          element("button", { type: "button", className: "secondary-button" }, this.i18n.t("common.remove")),
        );
        row.querySelector("button")?.addEventListener("click", () => row.remove());
        priceList.append(row);
      };
      for (const price of prices.length ? prices : [{ amount: "", currency: "" }]) {
        appendPrice(price.amount, price.currency);
      }
      const addPrice = element("button", { type: "button", className: "secondary-button" }, this.i18n.t("manager_settings.pricing_add_price"));
      addPrice.addEventListener("click", () => appendPrice());
      const plan = element(
        "section",
        { className: "pricing-plan", "data-pricing-plan": "" },
        element("input", { name: "pricing_name", value: name, placeholder: this.i18n.t("manager_settings.pricing_name") }),
        priceList,
        element(
          "div",
          { className: "inline-actions" },
          addPrice,
          element("button", { type: "button", className: "secondary-button remove-pricing-plan" }, this.i18n.t("common.remove")),
        ),
      );
      plan.querySelector<HTMLButtonElement>(".remove-pricing-plan")?.addEventListener("click", () => plan.remove());
      pricingPlans.append(plan);
    };
    for (const plan of item.pricing_plans) appendPricingPlan(plan.name, plan.prices);
    const addPricing = element("button", { type: "button", className: "secondary-button" }, this.i18n.t("manager_settings.pricing_add"));
    addPricing.addEventListener("click", () => appendPricingPlan());

    const mutable = element("div", { className: "checkbox-grid" }, ...settingDescriptors.map((descriptor) => element(
      "label",
      { className: "checkbox-label" },
      element("input", { type: "checkbox", name: "player_mutable_parameters", value: descriptor.name, checked: resource.player_mutable_parameters.includes(descriptor.name) }),
      this.descriptorLabel(descriptor.name, "setting"),
    )));

    form.append(
      this.managerNavigationButton(route, "management"),
      ...(resource.classic ? [this.classicSettings(route, resource.settings_version, resource.classic)] : []),
      element(
        "p",
        { className: "setup-state" },
        this.i18n.t(resource.finalized_at ? "manager_settings.finalized" : "manager_settings.unfinalized"),
      ),
      group(
        "manager_settings.metadata",
        input("manager_settings.name", "name", item.name),
        input("tournament.slug", "slug", item.slug),
        select("tournament.visibility", "visibility", ["private", "public"], item.visibility),
        input("tournament.language", "language", item.language),
        element("h3", {}, this.i18n.t("tournament.authors")),
        authors,
        authorSearch,
        authorResults,
        element("div", { className: "inline-actions" }, addExistingAuthor, addNewAuthor),
      ),
      group(
        "manager_settings.competition",
        select("tournament.type", "type_key", resource.type_options, item.type_key, !canChangeCompetition),
        select("tournament.ruleset", "game_ruleset_key", resource.ruleset_options, item.ruleset_key, !canChangeCompetition),
        !canChangeCompetition
          ? element("p", { className: "field-help" }, this.i18n.t("manager_settings.competition_locked"))
          : element("p", { className: "field-help" }, this.i18n.t("manager_settings.competition_editable")),
      ),
      group(
        "manager_settings.pricing",
        select("tournament.payment", "payment_type", ["free", "one-time", "per-stage"], item.payment_type),
        pricingPlans,
        addPricing,
      ),
      group(
        "manager_settings.registration",
        input("tournament.registration_starts", "registration_starts_at", dateTimeLocal(item.registration_starts_at), "datetime-local"),
        input("tournament.registration_ends", "registration_ends_at", dateTimeLocal(item.registration_ends_at), "datetime-local"),
        element("label", { className: "checkbox-label" }, element("input", {
          name: "ignore_late_registrations", type: "checkbox", checked: resource.ignore_late_registrations,
        }), this.i18n.t("manager_settings.ignore_late_registrations")),
        input("tournament.starts", "starts_at", dateTimeLocal(item.starts_at), "datetime-local"),
        input("tournament.ends", "planned_ends_at", dateTimeLocal(item.planned_ends_at), "datetime-local"),
      ),
      group(
        "manager_settings.gameplay",
        element("h3", {}, this.i18n.t("tournament.defaults")),
        this.renderDescriptorGroup(settingDescriptors, "setting"),
        element("h3", {}, this.i18n.t("tournament.mutable")),
        mutable,
        element("h3", {}, this.i18n.t("tournament.policies")),
        this.renderDescriptorGroup(policyDescriptors, "policy"),
      ),
      element(
        "div",
        { className: "settings-actions" },
        element("button", { type: "submit", className: "primary-button" }, this.i18n.t("manager_settings.save")),
        resource.available_actions.includes("finalize")
          ? element(
              "button",
              {
                type: "button",
                className: "danger-button",
                onclick: (() => void this.finalizeTournament(route, resource)) as EventListener,
              },
              this.i18n.t("manager_settings.finalize"),
            )
          : null,
      ),
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.saveManagerSettings(route, resource, form);
    });
    this.renderFrame(route, form);
  }

  private async mutateClassic(route: RouteMatch, version: number, command: string,
    kind: string, values: Record<string, unknown>): Promise<void> {
    const controls = Array.from(this.root.querySelectorAll<HTMLButtonElement>("button"));
    controls.forEach((button) => { button.disabled = true; });
    try {
      await this.api.request(`/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/classic`, {
        method: "POST", body: { expected_version: version, command, kind, values },
      });
      this.platform.notifySuccess();
      await this.load(route);
    } catch (error) {
      await this.load(route);
      this.platform.notifyError();
      this.showTextDialog(this.i18n.t(`error.${error instanceof ApiError ? error.code : "internal_error"}`), []);
    }
  }

  private classicSettings(route: RouteMatch, version: number, classic: ClassicTournament): HTMLElement {
    const container = element("div", { className: "classic-settings" });
    for (const kind of ["first", "playoff"] as const) {
      const stage = classic.stages.find((s) => s.kind === kind);
      const locked = !!stage?.started_at || (kind === "first" && classic.stages.some((s) => s.kind === "playoff" && s.started_at));
      const type = element("select", {}, ...(
        kind === "first" ? ["none", "groups", "quiz"] : ["none", "playoff"]
      ).map((value) => element("option", { value, selected: value === (stage?.stage_type ?? "none") },
        this.i18n.t(`classic.${value}` as MessageKey))));
      const scheme = element("select", {});
      const refresh = (): void => {
        replaceChildren(scheme, ...classic.schemes.filter((s) => s.kind === type.value).map((s) =>
          element("option", { value: s.id, selected: s.id === stage?.scheme_key },
            `${s.id} · ${s.size} ${this.i18n.t("classic.players")} · ${s.round_count} ${this.i18n.t("classic.rounds")}`)));
        scheme.disabled = locked || !["groups", "playoff"].includes(type.value);
      };
      type.addEventListener("change", refresh);
      refresh();
      const points = element("input", { value: (stage?.place_points ?? ["4", "3", "2", "1"]).join(", ") });
      const multiplier = element("input", { type: "number", step: "any", value: stage?.score_multiplier ?? "0.02" });
      container.append(element("fieldset", { disabled: locked },
        element("legend", {}, this.i18n.t(kind === "first" ? "manager_management.first_stage" : "manager_management.playoff_stage")),
        element("label", {}, this.i18n.t("classic.type"), type),
        element("label", {}, this.i18n.t("classic.scheme"), scheme),
        element("label", {}, this.i18n.t("classic.points"), points),
        element("label", {}, this.i18n.t("classic.multiplier"), multiplier),
        element("button", { type: "button", onclick: (() => void this.mutateClassic(route, version, "configure", kind, {
          stage_type: type.value, scheme_key: scheme.value || null,
          place_points: points.value.split(",").map((p) => p.trim()), score_multiplier: multiplier.value,
        })) as EventListener }, this.i18n.t("classic.save_stage")),
        locked ? element("p", {}, this.i18n.t("classic.locked")) : null,
      ));
    }
    return container;
  }

  private classicRounds(route: RouteMatch, resource: TournamentManagerManagementResource, stage?: ClassicStage): HTMLElement {
    const container = element("div", { className: "classic-rounds" });
    const names = new Map(resource.classic?.players.map((p) => [p.id, p.name]));
    if (!stage || stage.stage_type === "none") {
      container.append(element("p", {}, this.i18n.t("classic.configure_first")));
      return container;
    }
    for (const round of stage.rounds) {
      const packet = element("select", {}, element("option", { value: "" }, "—"),
        ...resource.packets.map((p) => element("option", { value: p.assignment_id, selected: p.assignment_id === round.assignment_id }, p.name)));
      packet.disabled = round.packet_locked;
      const discoverable = element("input", { type: "checkbox", role: "switch", checked: round.discoverable });
      const playable = element("input", { type: "checkbox", role: "switch", checked: round.playable });
      const deadline = element("input", { type: "datetime-local", value: dateTimeLocal(round.start_deadline) });
      container.append(element("article", { className: "resource-card" },
        element("h3", {}, `${this.i18n.t("classic.round")} ${round.number}`),
        element("label", {}, this.i18n.t("manager_management.packet_select"), packet),
        element("label", {}, discoverable, this.i18n.t("manager_management.discoverable")),
        element("label", {}, playable, this.i18n.t("manager_management.playable")),
        element("label", {}, this.i18n.t("classic.deadline"), deadline),
        element("p", { className: "field-help" }, this.i18n.t("classic.deadline_help")),
        element("button", { type: "button", disabled: resource.tournament.status !== "active",
          onclick: (() => void this.mutateClassic(route, resource.settings_version, "round", stage.kind, {
            round_id: round.id, assignment_id: packet.value || null, discoverable: discoverable.checked,
            playable: playable.checked, start_deadline: deadline.value ? new Date(deadline.value).toISOString() : null,
          })) as EventListener }, this.i18n.t("classic.save_round")),
        ...round.matches.map((m) => {
          const results = m.results?.map((r) => `${r.place}. ${r.seat.startsWith("chair:") ? this.i18n.t("classic.chair") : names.get(r.seat) ?? r.seat} (${r.score})`).join("; ");
          return element("p", {},
            `${this.i18n.t("classic.group")} ${m.group} · ${this.i18n.t("classic.game")} ${m.number}: ${results || m.players.join(", ") || this.i18n.t("classic.awaiting_results")} · ${this.i18n.t(m.randomized ? "classic.randomized" : m.results ? "classic.completed" : m.game_id ? "classic.running" : "classic.pending")}`);
        }),
      ));
    }
    if (stage.standings.length) container.append(element("table", {},
      element("thead", {}, element("tr", {}, ...["classic.players", ...(stage.stage_type === "quiz" ? [] : ["classic.total_points"]), "classic.score"].map((k) => element("th", {}, this.i18n.t(k as MessageKey))))),
      element("tbody", {}, ...stage.standings.map((row) => element("tr", {}, element("td", {}, row.name), stage.stage_type === "quiz" ? null : element("td", {}, row.points), element("td", {}, row.score))))));
    return container;
  }

  private classicSeeding(route: RouteMatch, resource: TournamentManagerManagementResource): HTMLElement {
    const classic = resource.classic!;
    const container = element("div", { className: "classic-seeding" });
    const first = classic.stages.find((s) => s.kind === "first" && s.stage_type !== "none");
    const stage = first ?? classic.stages.find((s) => s.kind === "playoff" && s.stage_type !== "none");
    if (!stage) return element("p", {}, this.i18n.t("classic.configure_first"));
    const locked = !!stage.started_at || resource.tournament.status !== "active";
    container.append(element("p", {}, this.i18n.t("classic.seeding_help")));
    for (const mode of ["automatic", "random"] as const) container.append(element("button", {
      type: "button", disabled: locked,
      onclick: (() => void this.mutateClassic(route, resource.settings_version, "seed", stage.kind, { mode })) as EventListener,
    }, this.i18n.t(`classic.${mode}`)));
    const groups: HTMLSelectElement[][] = [];
    stage.seeds.forEach((group, i) => {
      const selectors = group.map((seat) => element("select", { disabled: locked },
        element("option", { value: "", selected: seat === null || seat.startsWith("chair:") }, this.i18n.t("classic.chair")),
        ...classic.players.map((p) => element("option", { value: p.id, selected: p.id === seat }, p.name))));
      groups.push(selectors);
      container.append(element("fieldset", {}, element("legend", {}, `${this.i18n.t("classic.group")} ${i + 1}`),
        ...selectors.map((select, index) => element("label", {}, `${this.i18n.t("classic.seat")} ${index + 1}`, select))));
    });
    if (groups.length) container.append(element("button", { type: "button", disabled: locked,
      onclick: (() => void this.mutateClassic(route, resource.settings_version, "seed", stage.kind, {
        mode: "manual", seeds: groups.map((group) => group.map((select) => select.value || null)),
      })) as EventListener }, this.i18n.t("classic.save_seeding")));
    return container;
  }

  private managerNavigationButton(route: RouteMatch, view: "settings" | "management"): HTMLButtonElement {
    return element("button", {
      type: "button",
      className: "secondary-button",
      onclick: (() => this.router.navigate(
        `/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/${view}`,
      )) as EventListener,
    }, this.i18n.t(view === "settings" ? "route.manager_settings.title" : "route.manager_management.title"));
  }

  private renderDescriptorGroup(
    descriptors: TournamentManagerSettingsResource["setting_descriptors"],
    prefix: "setting" | "policy",
  ): HTMLElement {
    const wrapper = element("div", { className: "descriptor-editor" });
    const picker = element("select", { "aria-label": this.i18n.t(prefix === "setting" ? "manager_settings.setting_select" : "manager_settings.policy_select") });
    const panels = element("div", { className: "descriptor-panels" });
    const show = (name: string): void => {
      for (const panel of panels.querySelectorAll<HTMLElement>("[data-descriptor]")) {
        panel.hidden = panel.dataset.descriptor !== name;
      }
    };
    for (const descriptor of descriptors) {
      picker.append(element("option", { value: descriptor.name }, this.descriptorLabel(descriptor.name, prefix)));
      const fieldName = `${prefix}:${descriptor.name}`;
      let control: HTMLElement;
      if (descriptor.value_type === "boolean") {
        control = element("label", { className: "checkbox-label" }, element("input", {
          type: "checkbox", name: fieldName, checked: descriptor.value === true,
        }), this.i18n.t("common.enabled"));
      } else if (descriptor.value_type === "enum") {
        control = element("select", { name: fieldName }, ...descriptor.options.map((option) => element("option", {
          value: option, selected: option === descriptor.value,
        }, option)));
      } else {
        const serialized = descriptor.value_type === "array"
          ? JSON.stringify(descriptor.value)
          : descriptor.value == null ? "" : String(descriptor.value);
        control = element("input", {
          name: fieldName,
          value: serialized,
          type: descriptor.value_type === "array" || descriptor.value_type === "string" ? "text" : "number",
          step: descriptor.value_type === "integer" ? "1" : "any",
        });
      }
      panels.append(element(
        "section",
        { className: "descriptor-panel", "data-descriptor": descriptor.name },
        element("h3", {}, this.descriptorLabel(descriptor.name, prefix)),
        element("p", { className: "field-help" }, this.descriptorDescription(descriptor.description_key)),
        control,
      ));
    }
    picker.addEventListener("change", () => show(picker.value));
    wrapper.append(picker, panels);
    show(descriptors[0]?.name ?? "");
    return wrapper;
  }


  private descriptorLabel(name: string, kind: "setting" | "policy"): string {
    const key = `${kind}.${name}.label` as MessageKey;
    return this.i18n.t(key) || name.replaceAll("_", " ");
  }

  private descriptorDescription(key: string): string {
    return this.i18n.t(key as MessageKey) || key;
  }

  private descriptorValues(
    form: HTMLFormElement,
    descriptors: ManagerSettingDescriptor[],
    prefix: "setting" | "policy",
  ): Record<string, unknown> {
    const values: Record<string, unknown> = {};
    for (const descriptor of descriptors) {
      const control = form.elements.namedItem(`${prefix}:${descriptor.name}`) as HTMLInputElement | HTMLSelectElement | null;
      if (!control) continue;
      if (descriptor.value_type === "boolean") values[descriptor.name] = (control as HTMLInputElement).checked;
      else if (descriptor.value_type === "array") values[descriptor.name] = JSON.parse(control.value);
      else if (descriptor.value_type === "integer") values[descriptor.name] = control.value ? Number.parseInt(control.value, 10) : null;
      else if (descriptor.value_type === "number") values[descriptor.name] = Number(control.value);
      else values[descriptor.name] = control.value;
    }
    return values;
  }

  private pricingPlans(form: HTMLFormElement): Array<{
    name: string;
    prices: Array<{ amount: number; currency: string }>;
  }> {
    const plans: Array<{ name: string; prices: Array<{ amount: number; currency: string }> }> = [];
    for (const plan of form.querySelectorAll<HTMLElement>("[data-pricing-plan]")) {
      const name = plan.querySelector<HTMLInputElement>("[name=pricing_name]")?.value.trim() ?? "";
      const prices: Array<{ amount: number; currency: string }> = [];
      for (const row of plan.querySelectorAll<HTMLElement>(".pricing-price-row")) {
        const amount = Number(row.querySelector<HTMLInputElement>("[name=pricing_amount]")?.value ?? "");
        const currency = row.querySelector<HTMLInputElement>("[name=pricing_currency]")?.value.trim().toUpperCase() ?? "";
        if (!amount && !currency) continue;
        prices.push({ amount, currency });
      }
      if (name || prices.length) plans.push({ name, prices });
    }
    return plans;
  }

  private registeredAuthorPicker(
    route: RouteMatch,
    path: string,
    selected: RegisteredAuthor | undefined,
    onSelect: (author: RegisteredAuthor | undefined) => void,
  ): HTMLElement {
    const search = element("input", {
      type: "search", maxlength: "300",
      placeholder: this.i18n.t("manager_settings.author_search_placeholder"),
      "aria-label": this.i18n.t("manager_settings.author_search"),
    });
    const choices = new Map<string, RegisteredAuthor>();
    if (selected) choices.set(selected.author_id, selected);
    const picker = element("select", { "aria-label": this.i18n.t("packet_editor.author_select") });
    const renderOptions = (items: RegisteredAuthor[], id: string): void => {
      for (const author of items) choices.set(author.author_id, author);
      const visible = new Map(items.map((author) => [author.author_id, author]));
      if (id && choices.has(id)) visible.set(id, choices.get(id)!);
      replaceChildren(picker,
        element("option", { value: "" }, this.i18n.t("packet_editor.author_select")),
        ...Array.from(visible.values()).map((author) => element("option", {
          value: author.author_id,
        }, author.display_name)),
      );
      picker.value = id;
    };
    renderOptions(selected ? [selected] : [], selected?.author_id ?? "");
    picker.addEventListener("change", () => onSelect(choices.get(picker.value)));
    const errorText = element("p", { className: "field-help", role: "status" });
    let requestSequence = 0;
    let timer: number | undefined;
    let loaded = false;
    const loadAuthors = async (): Promise<void> => {
      const sequence = ++requestSequence;
      try {
        const result = await this.api.request<{ items: RegisteredAuthor[] }>(
          `${path}${path.includes("?") ? "&" : "?"}query=${encodeURIComponent(search.value)}`,
        );
        if (sequence !== requestSequence || !picker.isConnected) return;
        renderOptions(result.items, picker.value);
        errorText.textContent = result.items.length ? "" : this.i18n.t("packet_editor.authors_no_matches");
        loaded = true;
      } catch (error) {
        if (sequence === requestSequence) {
          const code = error instanceof ApiError ? error.code : "internal_error";
          errorText.textContent = this.i18n.t(`error.${code}`);
        }
      }
    };
    picker.addEventListener("focus", () => { if (!loaded) void loadAuthors(); });
    search.addEventListener("input", () => {
      requestSequence += 1;
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => void loadAuthors(), 200);
    });
    const create = element("button", { type: "button", className: "secondary-button" }, this.i18n.t("manager_settings.author_add_new"));
    create.addEventListener("click", () => this.showNewAuthorDialog(route, {
      path,
      onCreated: (author) => {
        renderOptions([author], author.author_id);
        onSelect(author);
      },
    }));
    return element("div", { className: "packet-author-picker" },
      element("label", {}, this.i18n.t("packet_editor.author_associate"), search, picker),
      errorText,
      element("div", { className: "inline-actions" }, this.i18n.t("packet_editor.or"), create),
    );
  }

  private showNewAuthorDialog(route: RouteMatch, packetAuthor?: {
    path: string;
    onCreated: (author: RegisteredAuthor) => void;
  }): void {
    const authorForm = element(
      "form",
      { className: "settings-form" },
      element("label", {}, this.i18n.t("manager_settings.author_first_name"), element("input", { name: "first_name", required: true })),
      element("label", {}, this.i18n.t("manager_settings.author_second_name"), element("input", { name: "second_name" })),
      element("label", {}, this.i18n.t("manager_settings.author_surname"), element("input", { name: "surname", required: true })),
      element("label", {}, this.i18n.t("manager_settings.author_telegram"), element("input", { name: "telegram_link", type: "text", placeholder: "https://t.me/username" })),
      element("button", { type: "submit", className: "primary-button" }, this.i18n.t("manager_settings.author_register")),
    );
    const dialog = element(
      "dialog",
      { className: "tournament-dialog", "aria-labelledby": "new-author-title" },
      element("div", { className: "dialog-heading" }, element("h2", { id: "new-author-title" }, this.i18n.t("manager_settings.author_add_new")), element("button", { type: "button", className: "icon-button", "aria-label": this.i18n.t("common.close") }, "×")),
      authorForm,
    );
    dialog.querySelector<HTMLButtonElement>(".icon-button")?.addEventListener("click", () => dialog.close());
    authorForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(authorForm);
      try {
        const response = await this.api.request<TournamentManagerSettingsResource | RegisteredAuthor>(
          packetAuthor?.path ?? `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/authors`,
          {
            method: "POST",
            body: {
              first_name: String(data.get("first_name") ?? ""),
              second_name: String(data.get("second_name") ?? "") || null,
              surname: String(data.get("surname") ?? ""),
              telegram_link: String(data.get("telegram_link") ?? "") || null,
            },
          },
        );
        dialog.close();
        this.platform.notifySuccess();
        if (packetAuthor && "author_id" in response) packetAuthor.onCreated(response);
        else if ("tournament" in response) this.renderManagerSettings(route, { ...response, kind: "manager_settings", state: "ready" });
      } catch (error) {
        await this.handleManagerSettingsError(route, authorForm, error);
      }
    });
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  private async saveManagerSettings(
    route: RouteMatch,
    resource: TournamentManagerSettingsResource,
    form: HTMLFormElement,
  ): Promise<void> {
    const data = new FormData(form);
    const inferredType = (candidate: unknown): ManagerSettingDescriptor["value_type"] =>
      Array.isArray(candidate) ? "array" : typeof candidate === "boolean" ? "boolean" : Number.isInteger(candidate) ? "integer" : typeof candidate === "number" ? "number" : "string";
    const settingDescriptors = resource.setting_descriptors ?? Object.entries(resource.default_parameters).map(([name, candidate]) => ({
      name, value: candidate, value_type: inferredType(candidate), description_key: `ruleset.${resource.tournament.ruleset_key}.${name}.description`, options: [],
    }));
    const policyDescriptors = resource.policy_descriptors ?? Object.entries(resource.policies).filter(([name]) => name !== "ruleset_rating_weight").map(([name, candidate]) => ({
      name, value: candidate, value_type: inferredType(candidate), description_key: `policy.${name}.description`, options: [],
    }));
    const value = (name: string): string => String(data.get(name) ?? "").trim();
    const timestamp = (name: string): string | null => {
      const raw = value(name);
      return raw ? new Date(raw).toISOString() : null;
    };
    try {
      const response = await this.api.request<TournamentManagerSettingsResource>(
        `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/settings`,
        {
          method: "POST",
          body: {
            expected_version: resource.settings_version,
            name: value("name"),
            slug: value("slug"),
            type_key: value("type_key") || resource.tournament.type_key,
            game_ruleset_key: value("game_ruleset_key") || resource.tournament.ruleset_key,
            visibility: value("visibility"),
            language: value("language"),
            payment_type: value("payment_type"),
            pricing_plans: value("payment_type") === "free" ? [] : this.pricingPlans(form),
            registration_open: resource.registration_enabled,
            ignore_late_registrations: data.has("ignore_late_registrations"),
            registration_starts_at: timestamp("registration_starts_at"),
            registration_ends_at: timestamp("registration_ends_at"),
            starts_at: timestamp("starts_at"),
            planned_ends_at: timestamp("planned_ends_at"),
            author_ids: data.getAll("author_ids").map(String),
            author_names: [],
            default_parameters: this.descriptorValues(form, settingDescriptors, "setting"),
            player_mutable_parameters: data.getAll("player_mutable_parameters").map(String),
            policies: this.descriptorValues(form, policyDescriptors, "policy"),
          },
        },
      );
      this.platform.notifySuccess();
      this.renderManagerSettings(route, { ...response, kind: "manager_settings", state: "ready" });
    } catch (error) {
      await this.handleManagerSettingsError(route, form, error);
    }
  }

  private async finalizeTournament(
    route: RouteMatch,
    resource: TournamentManagerSettingsResource,
  ): Promise<void> {
    if (!window.confirm(this.i18n.t("manager_settings.finalize_confirm"))) return;
    try {
      const response = await this.api.request<TournamentManagerSettingsResource>(
        `/api/miniapp/manager/tournaments/${encodeURIComponent(route.params.launch_ref ?? "")}/finalize`,
        { method: "POST", body: { expected_version: resource.settings_version } },
      );
      this.platform.notifySuccess();
      this.renderManagerSettings(route, { ...response, kind: "manager_settings", state: "ready" });
    } catch (error) {
      await this.handleManagerSettingsError(route, this.root, error);
    }
  }

  private async handleManagerSettingsError(
    route: RouteMatch,
    container: Element,
    error: unknown,
  ): Promise<void> {
    const code = error instanceof ApiError ? error.code : "validation_failed";
    this.platform.notifyError();
    if (code === "stale_write") {
      await this.load(route);
      return;
    }
    container.querySelector(".inline-error")?.remove();
    const alert = this.statusCard("error", this.i18n.t(`error.${code}`));
    alert.classList.add("inline-error");
    container.append(alert);
  }

  private async handleTournamentAction(
    route: RouteMatch,
    resource: TournamentRouteResource,
    item: TournamentListItem,
    action: TournamentAction,
    button: HTMLButtonElement,
  ): Promise<void> {
    button.disabled = true;
    try {
      if (action === "info") {
        const details = await this.api.request<TournamentDetailsPayload>(
          `/api/miniapp/tournaments/${encodeURIComponent(item.id)}?role=${resource.role}`,
        );
        this.showTournamentDetails(details);
        return;
      }
      if (action === "register") {
        const decision = await this.api.request<TournamentRegistrationPayload>(
          `/api/miniapp/tournaments/${encodeURIComponent(item.id)}/register`,
          { method: "POST", body: {} },
        );
        if (decision.accepted) {
          this.platform.notifySuccess();
          await this.load(route);
        } else {
          this.platform.notifyError();
          this.showTextDialog(
            this.i18n.t("tournament.registration_rejected"),
            decision.reasons.length ? decision.reasons : [this.i18n.t("error.validation_failed")],
          );
        }
        return;
      }
      const mode = action === "select_manager" ? "manager" : "player";
      await this.api.request(`/api/miniapp/tournaments/${encodeURIComponent(item.id)}/select`, {
        method: "POST",
        body: { mode, expected_version: resource.navigation_version },
      });
      this.platform.notifySuccess();
      this.platform.returnToBot();
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.platform.notifyError();
      const alert = this.statusCard("error", this.i18n.t(`error.${code}`));
      alert.classList.add("inline-error");
      button.closest("article")?.append(alert);
    } finally {
      button.disabled = false;
    }
  }

  private showTournamentDetails(details: TournamentDetailsPayload): void {
    const item = details.tournament;
    const requirements = details.registration_requirements.length
      ? element(
          "ul",
          { className: "detail-list" },
          ...details.registration_requirements.map((requirement) =>
            element("li", {}, requirement.failure_message || `${requirement.kind}: ${requirement.target_name}`),
          ),
        )
      : element("p", {}, this.i18n.t("tournament.requirements.empty"));
    const dialog = element(
      "dialog",
      { className: "tournament-dialog", "aria-labelledby": "tournament-dialog-title" },
      element(
        "div",
        { className: "dialog-heading" },
        element("h2", { id: "tournament-dialog-title" }, item.name),
        element(
          "button",
          { type: "button", className: "icon-button", "aria-label": this.i18n.t("common.close") },
          "×",
        ),
      ),
      this.detail(this.i18n.t("tournament.slug"), item.slug),
      this.detail(
        this.i18n.t("tournament.status"),
        this.i18n.t(`tournament.status.${item.status}` as MessageKey),
      ),
      this.detail(
        this.i18n.t("tournament.setup"),
        this.i18n.t(
          item.finalized_at ? "tournament.setup.finalized" : "tournament.setup.unfinalized",
        ),
      ),
      this.detail(
        this.i18n.t("tournament.visibility"),
        this.i18n.t(`tournament.visibility.${item.visibility}`),
      ),
      this.detail(
        this.i18n.t("filters.registration"),
        this.i18n.t(
          item.registration_open
            ? "tournament.registration.open"
            : "tournament.registration.closed",
        ),
      ),
      this.detail(
        this.i18n.t("tournament.registration_starts"),
        this.formatDate(item.registration_starts_at),
      ),
      this.detail(
        this.i18n.t("tournament.registration_ends"),
        this.formatDate(item.registration_ends_at),
      ),
      this.detail(this.i18n.t("tournament.starts"), this.formatDate(item.starts_at)),
      this.detail(this.i18n.t("tournament.ends"), this.formatDate(item.planned_ends_at)),
      this.detail(this.i18n.t("tournament.actual_end"), this.formatDate(item.actual_ends_at)),
      this.detail(this.i18n.t("tournament.language"), item.language),
      this.detail(this.i18n.t("tournament.type"), `${item.type_key} v${item.type_version}`),
      this.detail(this.i18n.t("tournament.ruleset"), `${item.ruleset_key} v${item.ruleset_version}`),
      this.detail(this.i18n.t("tournament.payment"), this.formatPayment(item)),
      this.detail(
        this.i18n.t("tournament.membership"),
        item.membership_status
          ? this.i18n.t(`tournament.membership.${item.membership_status}` as MessageKey)
          : this.i18n.t("tournament.none"),
      ),
      this.detail(this.i18n.t("tournament.policy_version"), String(item.policy_version)),
      this.detail(
        this.i18n.t("tournament.authors"),
        item.authors.length ? item.authors.join(", ") : this.i18n.t("tournament.none"),
      ),
      element("h3", {}, this.i18n.t("tournament.requirements")),
      requirements,
      this.detail(this.i18n.t("tournament.policies"), this.formatObject(details.policies)),
      this.detail(this.i18n.t("tournament.defaults"), this.formatObject(details.default_parameters)),
      this.detail(
        this.i18n.t("tournament.mutable"),
        details.player_mutable_parameters.length
          ? details.player_mutable_parameters.join(", ")
          : this.i18n.t("tournament.none"),
      ),
    );
    const close = dialog.querySelector<HTMLButtonElement>(".icon-button");
    close?.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  private promptSuspicionClear(route: RouteMatch, card: SuspicionLedgerCard): void {
    const note = element("textarea", { rows: "3" }) as HTMLTextAreaElement;
    const form = element(
      "form",
      { className: "settings-form" },
      element("h2", {}, card.display_name ?? card.telegram_username ?? card.player_id),
      element("p", {}, `${this.i18n.t("admin_suspicion.player_id")}: ${card.player_id}`),
      element("p", {}, this.i18n.t("admin_suspicion.clear_prompt")),
      element("label", {}, this.i18n.t("admin_suspicion.clear_note"), note),
      element(
        "div",
        { className: "settings-actions" },
        element("button", { type: "submit", className: "primary-button" }, this.i18n.t("admin_suspicion.clear_confirm")),
        element(
          "button",
          {
            type: "button",
            className: "secondary-button",
            onclick: (() => this.router.navigate("/admin/suspicion")) as EventListener,
          },
          this.i18n.t("admin_suspicion.clear_cancel"),
        ),
      ),
    ) as HTMLFormElement;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = note.value.trim();
      if (!value) return;
      void this.submitSuspicionClear(route, card, value);
    });
    this.renderFrame(route, element("section", { className: "route-content" }, form));
  }

  private async submitSuspicionClear(
    route: RouteMatch,
    card: SuspicionLedgerCard,
    note: string,
  ): Promise<void> {
    this.renderFrame(route, this.statusCard("loading", this.i18n.t("common.loading")));
    try {
      await this.api.request(
        `/api/miniapp/admin/suspicion/ledger/${encodeURIComponent(card.player_id)}/clear`,
        { method: "POST", body: { note } },
      );
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.showTextDialog(this.i18n.t("admin_suspicion.clear"), [this.i18n.t(`error.${code}`)]);
      return;
    }
    this.router.navigate("/admin/suspicion");
  }

  private showTextDialog(title: string, lines: string[]): void {
    const dialog = element(
      "dialog",
      { className: "tournament-dialog", "aria-labelledby": "tournament-message-title" },
      element(
        "div",
        { className: "dialog-heading" },
        element("h2", { id: "tournament-message-title" }, title),
        element(
          "button",
          { type: "button", className: "icon-button", "aria-label": this.i18n.t("common.close") },
          "×",
        ),
      ),
      element("ul", { className: "detail-list" }, ...lines.map((line) => element("li", {}, line))),
    );
    dialog.querySelector<HTMLButtonElement>(".icon-button")?.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  private badge(label: string, kind = "default"): HTMLElement {
    return element("span", { className: `badge badge-${kind}` }, label);
  }

  private renderSuspicionLedger(route: RouteMatch, resource: AdminSuspicionLedgerResource): void {
    if (!resource.items.length) {
      this.renderFrame(
        route,
        this.statusCard("empty", this.i18n.t("route.admin_suspicion.empty")),
      );
      return;
    }
    const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
    for (const card of resource.items) {
      const rulesetLines = card.rulesets.length
        ? card.rulesets.map(
            (stat) =>
              `${stat.ruleset_key}: ${this.i18n.t("admin_suspicion.rating")} ${
                stat.rating ?? "—"
              }, ${this.i18n.t("admin_suspicion.games_played")} ${stat.games_played}`,
          )
        : [this.i18n.t("admin_suspicion.no_rulesets")];
      const children: (Node | null)[] = [
        element("h2", {}, card.display_name ?? card.telegram_username ?? card.player_id),
        element("p", {}, `${this.i18n.t("admin_suspicion.player_id")}: ${card.player_id}`),
        element("p", {}, `${this.i18n.t("admin_suspicion.suspicion")}: ${card.suspicion}`),
        element("h3", {}, this.i18n.t("admin_suspicion.rulesets")),
        ...rulesetLines.map((line) => element("p", {}, line)),
      ];
      if (card.reports.length) {
        children.push(
          element("h3", {}, this.i18n.t("admin_suspicion.reports")),
          ...card.reports.map((report) => element("p", {}, `${report.kind}: ${report.count}`)),
        );
      }
      children.push(
        element(
          "div",
          { className: "settings-actions" },
          element(
            "button",
            {
              type: "button",
              className: "primary-button",
              onclick: (() => void this.inspectSuspicion(route, card)) as EventListener,
            },
            this.i18n.t("admin_suspicion.inspect"),
          ),
          element(
            "button",
            {
              type: "button",
              className: "secondary-button",
              onclick: (() => this.promptSuspicionClear(route, card)) as EventListener,
            },
            this.i18n.t("admin_suspicion.clear"),
          ),
        ),
      );
      list.append(
        element(
          "article",
          { className: "resource-card", "data-player-id": card.player_id },
          ...children.filter((child): child is Node => child !== null),
        ),
      );
    }
    this.renderFrame(route, element("section", { className: "route-content" }, list));
    queueMicrotask(() => document.querySelector<HTMLElement>("#page-title")?.focus());
  }

  private async inspectSuspicion(route: RouteMatch, card: SuspicionLedgerCard): Promise<void> {
    this.renderFrame(route, this.statusCard("loading", this.i18n.t("common.loading")));
    try {
      const payload = await this.api.request<AdminSuspicionInspectionPayload>(
        `/api/miniapp/admin/suspicion/ledger/${encodeURIComponent(card.player_id)}/events`,
      );
      this.renderSuspicionInspection(route, payload);
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "internal_error";
      this.renderError(route, code);
    }
  }

  private renderSuspicionInspection(
    route: RouteMatch,
    payload: AdminSuspicionInspectionPayload,
  ): void {
    const player = payload.player;
    const events: Node[] = payload.events.length
      ? payload.events.map((event) =>
          element(
            "article",
            { className: "library-question" },
            element(
              "h3",
              {},
              `${event.reason}${event.ruleset_key ? ` · ${event.ruleset_key}` : ""} · ${this.formatDate(event.created_at)}`,
            ),
            element(
              "p",
              {},
              `${this.i18n.t("admin_suspicion.event_delta")}: ${event.before} → ${event.after} (+${event.delta})`,
            ),
            event.note
              ? element("p", {}, `${this.i18n.t("admin_suspicion.note")}: ${event.note}`)
              : null,
            ...event.evidence.map((evidence) =>
              element(
                "p",
                {},
                `${this.i18n.t("admin_suspicion.evidence")}: ${evidence.signal} · ${this.formatDate(evidence.created_at)}`,
              ),
            ),
          ),
        )
      : [element("p", {}, this.i18n.t("admin_suspicion.no_events"))];
    this.renderFrame(
      route,
      element(
        "section",
        { className: "route-content" },
        element("h2", {}, player.display_name ?? player.telegram_username ?? player.id),
        element("p", {}, `${this.i18n.t("admin_suspicion.player_id")}: ${player.id}`),
        element("p", {}, `${this.i18n.t("admin_suspicion.suspicion")}: ${player.suspicion}`),
        element("h3", {}, this.i18n.t("admin_suspicion.events_title")),
        ...events,
        element(
          "button",
          {
            type: "button",
            className: "secondary-button",
            onclick: (() => this.router.navigate("/admin/suspicion")) as EventListener,
          },
          this.i18n.t("admin_suspicion.back_to_ledger"),
        ),
      ),
    );
    queueMicrotask(() => document.querySelector<HTMLElement>("#page-title")?.focus());
  }

  private detail(label: string, value: string): HTMLElement {
    return element("p", { className: "tournament-detail" }, element("strong", {}, `${label}: `), value);
  }

  private formatDate(value?: string | null): string {
    if (!value) return this.i18n.t("tournament.not_set");
    const date = new Date(value);
    return Number.isNaN(date.valueOf())
      ? this.i18n.t("tournament.not_set")
      : new Intl.DateTimeFormat(this.i18n.locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  private formatPayment(item: TournamentListItem): string {
    const paymentKey = `tournament.payment.${item.payment_type}` as MessageKey;
    if (item.payment_type === "free") return this.i18n.t(paymentKey);
    const prices = item.pricing_plans.flatMap((plan) =>
      plan.prices.map((price) => `${plan.name}: ${price.amount} ${price.currency}`),
    );
    return prices.length
      ? `${this.i18n.t(paymentKey)} · ${prices.join("; ")}`
      : this.i18n.t(paymentKey);
  }

  private formatObject(value: Record<string, unknown>): string {
    const entries = Object.entries(value);
    return entries.length
      ? entries.map(([key, item]) => `${key}: ${formatValue(item)}`).join("; ")
      : this.i18n.t("tournament.none");
  }

  private renderError(route: RouteMatch, code: StableErrorCode): void {
    const key = `error.${code}` as MessageKey;
    const card = this.statusCard("error", this.i18n.t(key));
    if (code !== "authentication_required" && code !== "forbidden") {
      card.append(
        element(
          "button",
          { type: "button", className: "primary-button", onclick: (() => void this.load(route)) as EventListener },
          this.i18n.t("common.retry"),
        ),
      );
    }
    this.renderFrame(route, card);
  }

  private statusCard(kind: "loading" | "empty" | "error", message: string): HTMLElement {
    const indicator = kind === "loading" ? element("span", { className: "spinner", "aria-hidden": "true" }) : null;
    return element(
      "section",
      { className: `status-card status-${kind}`, role: kind === "error" ? "alert" : "status", "aria-live": "polite" },
      indicator,
      element("p", {}, message),
    );
  }

  private pagination(route: RouteMatch, page: NonNullable<RoutePayload["pagination"]>): HTMLElement {
    const move = (cursor: string): void => {
      const query = new URLSearchParams(route.query);
      query.set("cursor", cursor);
      this.router.navigate(`${route.path}?${query.toString()}`);
    };
    return element(
      "nav",
      { className: "pagination", "aria-label": `${this.i18n.t("common.previous")} / ${this.i18n.t("common.next")}` },
      element(
        "button",
        {
          type: "button",
          disabled: !page.previous,
          onclick: page.previous ? (() => move(page.previous!)) as EventListener : undefined,
        },
        this.i18n.t("common.previous"),
      ),
      element(
        "button",
        {
          type: "button",
          disabled: !page.next,
          onclick: page.next ? (() => move(page.next!)) as EventListener : undefined,
        },
        this.i18n.t("common.next"),
      ),
    );
  }

}

function isTournamentResource(value: RoutePayload["resource"]): value is TournamentRouteResource {
  return "kind" in value && value.kind === "tournaments";
}

function isManagerSettingsResource(
  value: RoutePayload["resource"],
): value is TournamentManagerSettingsResource {
  return "kind" in value && value.kind === "manager_settings";
}

function isManagerManagementResource(
  value: RoutePayload["resource"],
): value is TournamentManagerManagementResource {
  return "kind" in value && value.kind === "manager_management";
}

function isLobbyResource(value: RoutePayload["resource"]): value is LobbyResource {
  return "kind" in value && value.kind === "lobby";
}

function isPacketDraftResource(value: RoutePayload["resource"]): value is PacketDraftResource {
  return "kind" in value && value.kind === "packet_draft";
}

function isSuspicionLedgerResource(
  value: RoutePayload["resource"],
): value is AdminSuspicionLedgerResource {
  return "kind" in value && value.kind === "admin_suspicion_ledger";
}

function isTournamentAction(value: string): value is TournamentAction {
  return ["info", "register", "select_player", "select_manager"].includes(value);
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function dateTimeLocal(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "";
  const local = new Date(date.valueOf() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}
