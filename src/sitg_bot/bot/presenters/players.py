"""Escaped profile links for player lists, suitable for localized placeholders."""

import html
import re
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PlayerName:
    name: str
    player_id: str | None
    bot_username: str | None

    def __str__(self) -> str:
        name = html.escape(self.name)
        if not self.player_id or not self.bot_username:
            return name
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.bot_username):
            return name
        try:
            player_id = UUID(str(self.player_id)).hex
        except ValueError:
            return name
        return (
            f'<a href="https://t.me/{self.bot_username}/profiles'
            f'?startapp=player_{player_id}">{name}</a>'
        )


def player_name(player: dict, bot_username: str | None = None) -> PlayerName:
    return PlayerName(
        player.get("name", player.get("display_name", "")),
        None if player.get("is_chair") else player.get("player_id", player.get("id")),
        bot_username,
    )
