from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.database import normalize_database_url


def test_postgresql_url_selects_async_driver() -> None:
    assert (
        normalize_database_url("postgresql://user:pass@db/test")
        == "postgresql+asyncpg://user:pass@db/test"
    )


def test_missing_theme_and_question_authors_are_non_blocking_warnings() -> None:
    packet = Packet(
        "Test",
        (
            Theme(
                "Theme",
                tuple(
                    Question(
                        text=f"Question {value}",
                        answer="Answer",
                        commentary="",
                        value=value,
                        form="FORM",
                        source="https://example.test",
                    )
                    for value in (10, 20, 30, 40, 50)
                ),
            ),
        ),
    )

    errors, warnings = PacketAdminService.validate(packet)

    assert errors == []
    assert warnings[:3] == [
        "Packet has no lead author",
        "A packet normally contains 8–12 themes",
        "Theme 1 has no author",
    ]
    assert len(warnings) == 8
    assert all("has no author" in warning for warning in warnings[2:])


def test_missing_lead_author_is_a_non_blocking_warning() -> None:
    packet = Packet(
        "Test",
        (
            Theme(
                "Theme",
                tuple(
                    Question(
                        text=f"Question {value}",
                        answer="Answer",
                        commentary="",
                        value=value,
                        form="FORM",
                        source="https://example.test",
                    )
                    for value in (10, 20, 30, 40, 50)
                ),
                author="Theme Author",
            ),
        ),
    )

    errors, warnings = PacketAdminService.validate(packet)

    assert errors == []
    assert "Packet has no lead author" in warnings
