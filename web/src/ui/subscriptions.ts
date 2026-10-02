import type { TournamentSubscriptions } from "../api/types";
import type { I18n } from "../i18n";
import { element, replaceChildren } from "./dom";

export function renderSubscriptions(
  data: TournamentSubscriptions, active: boolean, i18n: I18n,
  save: (command: string, values: Record<string, unknown>) => Promise<void>,
  selectedPlayer = "", selectPlayer: (id: string) => void = () => {},
): HTMLElement {
  const root = element("div", { className: "subscription-cards" });
  const controls = element("fieldset", { className: "subscription-regions" });
  const submit = async (command: string, values: Record<string, unknown>): Promise<void> => {
    if (!active || controls.disabled) return;
    controls.disabled = true;
    try { await save(command, values); } finally { controls.disabled = false; }
  };
  const rights = ["discoverable", "readable", "playable"] as const;
  const countText = (count: number | null): string => count === null ? i18n.t("subscriptions.unlimited") : String(count);
  const rightText = (right: boolean | null): string => i18n.t(right === null ? "subscriptions.default" : right ? "subscriptions.yes" : "subscriptions.no");
  const cardRights = (card: TournamentSubscriptions["cards"][number]): HTMLElement => element("dl", { className: "subscription-rights" },
    ...rights.map((right) => element("div", {},
      element("dt", {}, i18n.t(`manager_management.${right}`)), element("dd", {}, rightText(card[right])))));
  const list = element("ul", { className: "resource-list subscription-list" }, ...data.cards.map((card) => element("li", { className: "resource-card subscription-card" },
    element("h3", {}, card.name),
    element("p", { className: "subscription-status" }, `${i18n.t("subscriptions.packet_count")}: ${countText(card.packet_count)}`),
    cardRights(card),
  )));
  const form = element("form", { className: "settings-form" });
  const fields = element("fieldset", { disabled: !active, className: "subscription-create-fields" });
  const name = element("input", { required: true, maxlength: "200", "aria-label": i18n.t("subscriptions.name") });
  const count = element("input", { type: "number", required: true, min: "1", max: "2147483647", step: "1", value: "1", "aria-label": i18n.t("subscriptions.packet_count") });
  const unlimited = element("input", { type: "checkbox", "aria-label": i18n.t("subscriptions.unlimited") });
  unlimited.addEventListener("change", () => { count.disabled = unlimited.checked; count.required = !unlimited.checked; });
  fields.append(element("label", {}, i18n.t("subscriptions.name"), name),
    element("label", {}, i18n.t("subscriptions.packet_count"), count),
    element("label", { className: "checkbox-label" }, unlimited, i18n.t("subscriptions.unlimited")));
  const selects = rights.map((right) => {
    const select = element("select", { "aria-label": i18n.t(`manager_management.${right}`) },
      ...["default", "yes", "no"].map((value) => element("option", { value }, i18n.t(`subscriptions.${value as "default" | "yes" | "no"}`))));
    fields.append(element("label", {}, i18n.t(`manager_management.${right}`), select));
    return select;
  });
  fields.append(element("button", { type: "submit", className: "primary-button" }, i18n.t("subscriptions.create")));
  form.append(fields);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!name.value.trim() || !form.reportValidity()) return;
    void submit("create", { name: name.value.trim(), packet_count: unlimited.checked ? null : Number(count.value),
      ...Object.fromEntries(rights.map((right, index) => [right, selects[index]!.value === "default" ? null : selects[index]!.value === "yes"])),
    });
  });
  const search = element("input", { type: "search", placeholder: i18n.t("subscriptions.search"), "aria-label": i18n.t("subscriptions.search") });
  const players = element("select", { "aria-label": i18n.t("subscriptions.participant") });
  const details = element("div", { className: "subscription-details" });
  const showPlayer = (): void => {
    selectedPlayer = players.value;
    selectPlayer(selectedPlayer);
    replaceChildren(details);
    if (!selectedPlayer) return;
    const instances = data.instances.filter((item) => item.player_id === selectedPlayer);
    if (!instances.length) details.append(element("p", {}, i18n.t("subscriptions.empty")));
    const assigned = element("ul", { className: "resource-list subscription-list" });
    for (const instance of instances) {
      const card = data.cards.find((item) => item.id === instance.card_id)!;
      const status = instance.revoked_at ? i18n.t("subscriptions.revoked")
        : instance.remaining_packets === 0 ? i18n.t("subscriptions.exhausted")
          : `${i18n.t("subscriptions.remaining")}: ${countText(instance.remaining_packets)}`;
      const row = element("li", { className: "resource-card subscription-card" },
        element("h3", {}, card.name), element("p", { className: "subscription-status" }, status), cardRights(card));
      if (!instance.revoked_at && instance.remaining_packets !== 0) row.append(element("button", {
        type: "button", className: "secondary-button", disabled: !active, onclick: (() => void submit("revoke", { subscription_id: instance.id })) as EventListener,
      }, i18n.t("subscriptions.revoke")));
      assigned.append(row);
    }
    details.append(assigned);
    const cards = element("select", { "aria-label": i18n.t("subscriptions.card") },
      ...data.cards.map((card) => element("option", { value: card.id }, card.name)));
    details.append(element("div", { className: "settings-form subscription-assignment" }, element("label", {}, i18n.t("subscriptions.card"), cards), element("button", {
      type: "button", className: "primary-button", disabled: !active || !data.cards.length || !data.players.find((player) => player.id === selectedPlayer)?.active,
      onclick: (() => void submit("assign", { card_id: cards.value, player_id: selectedPlayer })) as EventListener,
    }, i18n.t("subscriptions.assign"))));
  };
  const filter = (): void => {
    const matches = data.players.filter((player) => player.name.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()));
    replaceChildren(players, element("option", { value: "" }, i18n.t("subscriptions.select_participant")),
      ...matches.map((player) => element("option", { value: player.id }, player.name)));
    players.value = matches.some((player) => player.id === selectedPlayer) ? selectedPlayer : "";
    showPlayer();
  };
  search.addEventListener("input", filter);
  players.addEventListener("change", showPlayer);
  const region = (id: string, title: string, ...children: HTMLElement[]): HTMLElement => element("section",
    { className: "subscription-region", "aria-labelledby": id }, element("h2", { id }, title), ...children);
  controls.append(
    region("subscription-create", i18n.t("subscriptions.new"), form),
    region("subscription-existing", i18n.t("subscriptions.existing"),
      data.cards.length ? list : element("p", { className: "field-help" }, i18n.t("subscriptions.no_cards"))),
    region("subscription-participants", i18n.t("subscriptions.participants"),
      element("div", { className: "settings-form subscription-picker" },
        element("label", {}, i18n.t("subscriptions.search"), search),
        element("label", {}, i18n.t("subscriptions.participant"), players)), details),
  );
  root.append(element("p", { className: "field-help" }, i18n.t("subscriptions.help")), controls);
  filter();
  return root;
}
