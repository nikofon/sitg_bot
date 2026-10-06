"""Small event presenters for Telegram gameplay."""

import html
from decimal import Decimal
from uuid import UUID

from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
    RemoveKeyboardModel,
    ReplyKeyboardModel,
)
from sitg_bot.bot.presenters.players import player_name


def game_keyboard():
    # Bot API supports rows, not row spans or custom button dimensions.
    return ReplyKeyboardModel(rows=(("+",), ("||", "!")), persistent=True)


def abandon_confirmation(view, localization, locale, references):
    rows = []
    for confirmed in (True, False):
        reference = references.issue(
            scope="game_abandon",
            actor_id=UUID(view["viewer_id"]),
            resource_id=UUID(view["id"]),
            action="confirm" if confirmed else "cancel",
        )
        rows.append(
            InlineButtonModel(
                localization.text("button.yes" if confirmed else "button.no", locale),
                callback_data=f"gameleave:{reference}",
            )
        )
    return MessageModel(
        localization.text("flow.abandon_confirm", locale), InlineKeyboardModel(rows=(tuple(rows),))
    )


def appeal_ballot(ballot, localization, locale):
    text = localization.text(
        "flow.vote",
        locale,
        kind=localization.text("game.appeal_kind." + ballot["kind"], locale),
        answer=ballot["submitted_answer"],
        approvals=ballot["approvals"],
        rejections=ballot["rejections"],
        total=ballot["electorate_size"],
        status=localization.text("game.appeal_status." + ballot["status"], locale),
    )
    rows = ()
    if ballot["can_vote"]:
        rows = (
            tuple(
                InlineButtonModel(
                    localization.text(
                        "game.button.vote_yes" if approve else "game.button.vote_no", locale
                    ),
                    callback_data=f"appealvote:{UUID(ballot['id']).hex}:{int(approve)}",
                )
                for approve in (True, False)
            ),
        )
    return MessageModel(text, InlineKeyboardModel(rows=rows) if rows else None)


def join_message(view, localization, locale):
    if any(p["self"] and p.get("joined") for p in view["participants"]):
        return MessageModel(
            localization.text(
                "flow.joined_waiting",
                locale,
                joined=sum(bool(p.get("joined")) for p in view["participants"]),
                total=len(view["participants"]),
            )
        )
    text = localization.text("flow.created", locale)
    details = []
    if view.get("tournament_name"):
        details.append(
            localization.text(
                "game.info.tournament", locale, tournament=view["tournament_name"]
            )
        )
    if view.get("packet_names"):
        details.append(
            localization.text(
                "game.info.packets", locale, packets=", ".join(view["packet_names"])
            )
        )
    names = [str(player_name(p, view.get("bot_username"))) for p in view.get("participants") or ()]
    if names:
        details.append(
            localization.text("game.info.players", locale, players="{players}")
            .replace("{players}", ", ".join(names))
        )
    if details:
        text += "\n" + "\n".join(details)
    return MessageModel(
        text,
        InlineKeyboardModel(
            rows=(
                (
                    InlineButtonModel(
                        localization.text("flow.join", locale),
                        callback_data=f"gamejoin:{UUID(view['id']).hex}",
                    ),
                ),
            )
        ),
    )


def start_message(view, localization, locale):
    players = "\n".join(
        f"{i}) {player_name(p, view.get('bot_username'))}"
        for i, p in enumerate(view["participants"], 1)
    )
    keyboard = (
        game_keyboard() if any(p["self"] for p in view["participants"]) else RemoveKeyboardModel()
    )
    return MessageModel(
        localization.text("flow.started", locale, players="{players}")
        .replace("{players}", players),
        keyboard,
    )


def score_text(view, localization, locale, *, final=False):
    lines = [localization.text("flow.final_score" if final else "game.scores", locale)]
    players = sorted(view["participants"], key=lambda p: -p["score"])
    for i, player in enumerate(players, 1):
        # Preserve shared official places (including fractional places).
        rank = player.get("place") if final and player.get("place") is not None else i
        if not final and not player.get("is_chair") and not player.get("joined", True):
            lines.append(localization.text(
                "game.score_not_connected", locale,
                rank=rank, name=player_name(player, view.get("bot_username")),
            ))
            continue
        lines.append(
            localization.text(
                "flow.score_line",
                locale,
                rank=rank,
                name=player_name(player, view.get("bot_username")),
                score=player["score"],
                correct=player.get("correct_points", 0),
            )
        )
        if not final and not player.get("is_chair") and not player.get("active", True):
            lines[-1] += " — " + localization.text("game.score_disconnected", locale)
        if final:
            for scope in ("ruleset", "tournament"):
                for change in view.get("rating_changes", []):
                    if change["player_id"] != player["id"] or change["scope"] != scope:
                        continue
                    lines.append(
                        localization.text(
                            "flow.rating_" + scope,
                            locale,
                            before=f"{Decimal(str(change['before'])):.2f}",
                            after=f"{Decimal(str(change['after'])):.2f}",
                            delta=f"{Decimal(str(change['delta'])):+.2f}",
                        )
                    )
    if final:
        if view.get("rating_pending"):
            lines.append(localization.text("game.rating_pending", locale))
        lines.append("\n" + localization.text("flow.quit_hint", locale))
    return "\n".join(lines)


def rating_message(view, index, localization, locale):
    player = view["participants"][index]
    rows = ()
    if not player.get("vote_reason"):
        rows = (
            tuple(
                InlineButtonModel(
                    label, callback_data=f"gamevote:{UUID(view['id']).hex}:{index}:{vote}"
                )
                for label, vote in (("👍", 1), ("👎", -1))
            ),
        )
    text = html.escape(player["name"])
    if player.get("vote_reason"):
        text += "\n" + localization.text(f"game.{player['vote_reason']}", locale)
    return MessageModel(text, InlineKeyboardModel(rows=rows) if rows else None)


def plain_chunks(text, size=1800):
    return tuple(html.escape(text[start : start + size]) for start in range(0, len(text), size))
