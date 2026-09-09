from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest

from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.services.packets import PacketAdminService


def content():
    return asdict(Packet("Packet", (Theme("Theme", (Question("Text", "Answer", "", 10),)),)))


@pytest.mark.parametrize("kind", ["correction", "substitution"])
def test_changed_field_must_be_enabled_and_enabled_field_must_change(kind):
    old = content()
    new = deepcopy(old)
    path = "themes.0.questions.0.text"
    with pytest.raises(ValueError, match="Every enabled field"):
        PacketAdminService._validate_changes(old, new, {}, {}, {path: kind})
    new["themes"][0]["questions"][0]["text"] = "Changed"
    with pytest.raises(ValueError, match="Every enabled field"):
        PacketAdminService._validate_changes(old, new, {}, {}, {})
    PacketAdminService._validate_changes(old, new, {}, {}, {path: kind})


def test_author_identity_change_counts_even_when_names_are_identical():
    packet = content()
    PacketAdminService._validate_changes(
        packet,
        packet,
        {"lead_author": uuid4()},
        {"lead_author": uuid4()},
        {"lead_author": "correction"},
    )


def test_substitution_cannot_change_authorship_or_packet_metadata():
    old = content()
    new = deepcopy(old)
    new["name"] = "New packet"
    with pytest.raises(ValueError, match="Substitution is only"):
        PacketAdminService._validate_changes(old, new, {}, {}, {"name": "substitution"})


def test_structure_changes_cannot_bypass_field_classification():
    old = content()
    new = deepcopy(old)
    new["themes"] = ()
    with pytest.raises(ValueError, match="structure"):
        PacketAdminService._validate_changes(old, new, {}, {}, {})
