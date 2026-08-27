"""Tests for what a refused person is actually shown.

A denial reaches someone as a toast on their dashboard, so it has to read like
a sentence rather than a diagnostic. The `detail` beside it names commands,
tiers and entity ids: useful in the deny log, noise on screen, and an existence
oracle for anything the role hides.
"""

from custom_components.ha_rbac.decide import (
    DEFAULT_USER_MESSAGE,
    REASON_APP,
    REASON_DEGRADED,
    REASON_RESOURCE,
    REASON_TIER,
    REASON_UNBOUNDED,
    Decision,
)


def test_every_reason_has_a_sentence_for_the_person() -> None:
    """And it is a sentence, not a fragment of policy."""
    for reason in (
        REASON_TIER,
        REASON_APP,
        REASON_RESOURCE,
        REASON_UNBOUNDED,
        REASON_DEGRADED,
    ):
        message = Decision(allowed=False, reason=reason).message
        assert message.endswith((".", "?")), reason
        assert message[0].isupper(), reason
        assert "_" not in message, reason


def test_a_refusal_never_falls_back_to_its_diagnostic() -> None:
    """The failure this guards is a new gate that forgets to set a message.

    Defaulting from `reason` rather than writing the message at each refusal is
    what makes that safe, so an unrecognised reason still gets a sentence.
    """
    decision = Decision(
        allowed=False,
        reason="something_added_later",
        detail="role does not permit admin-tier request 'ha_rbac/roles/list'",
    )
    assert decision.message == DEFAULT_USER_MESSAGE
    assert "ha_rbac" not in decision.message
    # The diagnostic is kept -- it is what the deny log is for.
    assert "ha_rbac/roles/list" in decision.detail


def test_an_explicit_message_is_left_alone() -> None:
    """Refusals that are not about permission say so in their own words."""
    decision = Decision(
        allowed=False,
        reason="id_reuse",
        detail="Message id reused on this connection",
        message="Lost track of this connection. Reload the page to continue.",
    )
    assert decision.message.startswith("Lost track")
    assert "permission" not in decision.message


def test_an_allowed_decision_carries_no_message() -> None:
    """Nothing to say when nothing was refused."""
    assert Decision(allowed=True).message == ""
