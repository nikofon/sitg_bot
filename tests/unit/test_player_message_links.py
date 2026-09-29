from uuid import UUID

from sitg_bot.bot.handlers.lobby import lobby_info_text
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.game import join_message, score_text, start_message


def test_player_lists_link_profiles_and_escape_nicknames():
    player = {"id": str(UUID(int=1)), "name": '<b>A & B</b>', "self": True, "score": 5}
    view = {"id": str(UUID(int=2)), "participants": [player], "bot_username": "test_bot"}
    localization = LocalizationService()
    for locale in ("en", "ru"):
        for text in (
            join_message(view, localization, locale).text,
            start_message(view, localization, locale).text,
            score_text(view, localization, locale),
            lobby_info_text({"members": [{
                "player_id": player["id"], "display_name": player["name"], "role": "player",
            }]}, localization, locale, "test_bot"),
        ):
            assert f'href="https://t.me/test_bot?startapp=player_{UUID(int=1).hex}"' in text
            assert "&lt;b&gt;A &amp; B&lt;/b&gt;</a>" in text
            assert "&lt;a" not in text


def test_chair_and_unconfigured_bot_names_stay_plain():
    from sitg_bot.bot.presenters.players import player_name

    assert str(player_name({"name": "A & B", "id": str(UUID(int=1))})) == "A &amp; B"
    chair = {"name": "Chair", "id": str(UUID(int=1)), "is_chair": True}
    assert str(player_name(chair, "test_bot")) == "Chair"
