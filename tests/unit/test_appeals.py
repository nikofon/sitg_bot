import pytest

from sitg_bot.domain.appeals import AppealPolicy


def test_majority_appeal_policy_counts_missing_votes_as_opposition_at_timeout() -> None:
    policy = AppealPolicy(voting_rule="majority")

    assert policy.vote_decided(2, 0, 3) is True
    assert policy.vote_decided(1, 2, 3) is False
    assert policy.vote_decided(1, 0, 3) is None
    assert policy.vote_approved(1, 3) is False


def test_unanimous_appeal_policy_rejects_on_first_opposition() -> None:
    policy = AppealPolicy(voting_rule="unanimous")

    assert policy.vote_decided(2, 0, 3) is None
    assert policy.vote_decided(2, 1, 3) is False
    assert policy.vote_decided(3, 0, 3) is True


@pytest.mark.parametrize(
    ("policies", "message"),
    [
        ({"appeal_voting_rule": "plurality"}, "majority or unanimous"),
        ({"appeal_vote_timeout_seconds": 0}, "finite positive"),
        ({"appeal_escalation_enabled": 1}, "true or false"),
    ],
)
def test_appeal_policy_rejects_invalid_tournament_values(
    policies: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AppealPolicy.from_mapping(policies)
