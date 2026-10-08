"""Tests for tagging.py: which categories an email gets, and how they reach Outlook."""

import pytest

from exceptions import MessageGone, SystemProblem
from models import EmailDecision, Outcome, ReviewTag, StacResult
from tagging import (
    categories_for,
    mark_processing,
    mark_queued,
    mark_saving,
    merge_categories,
    move_email,
    needs_handling,
    should_move,
    tag_email,
    was_interrupted,
)


def _row(decision: str, outcome: str, stac_result: str = "") -> dict:
    """One attachment row, with only the fields tagging reads."""
    return {
        "email_number": 1,
        "email_decision": decision,
        "outcome": outcome,
        "stac_result": stac_result,
    }


class FakeClient:
    """Stands in for GraphClient.set_categories, recording what would be sent."""

    def __init__(self, error: Exception | None = None) -> None:
        """Optionally raise this error instead of recording the call."""
        self.error = error
        self.calls: list[tuple[str, str, list[str]]] = []

    def set_categories(self, mailbox: str, message_id: str, categories: list[str]) -> None:
        """Record the call, or raise the scripted error."""
        if self.error:
            raise self.error
        self.calls.append((mailbox, message_id, categories))


# categories_for


@pytest.mark.parametrize(
    ("stac_result", "tag"),
    [
        (StacResult.ENTERED, ReviewTag.FILED),
        (StacResult.REACHED_SAVE, ReviewTag.READY_TO_SAVE),
        (StacResult.REHEARSED, ReviewTag.REHEARSED),
    ],
)
def test_uploaded_email_is_tagged_by_how_far_stac_got(stac_result: str, tag: str) -> None:
    """Filed only when Save was really pressed."""
    rows = [_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, stac_result)]

    assert categories_for(rows) == [tag]


def test_upload_under_review_pair_adds_no_rule_tag() -> None:
    """Staff can see which uploads still need a Type sorted out inside STAC."""
    rows = [
        _row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.REHEARSED),
        _row(EmailDecision.UPLOAD, Outcome.PLS_RVW, StacResult.REHEARSED),
    ]

    assert categories_for(rows) == [ReviewTag.REHEARSED, ReviewTag.NO_RULE]


def test_uploaded_email_without_stac_result_is_a_bug() -> None:
    """A guessed tag would hide a broken loop, so it raises instead."""
    with pytest.raises(ValueError):
        categories_for([_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER)])


@pytest.mark.parametrize(
    ("outcome", "tag"),
    [
        (Outcome.NO_FILES, ReviewTag.NO_ATTACHMENTS),
        (Outcome.NON_PDF, ReviewTag.CANT_READ),
        (Outcome.TOO_LARGE, ReviewTag.CANT_READ),
        (Outcome.UNREADABLE, ReviewTag.CANT_READ),
        (Outcome.NO_UCN, ReviewTag.NO_UCN),
        (Outcome.UCN_CONFLICT, ReviewTag.UCN_CONFLICT),
        (Outcome.UCN_OTHER_COUNTY, ReviewTag.OTHER_COUNTY),
        (Outcome.INTERRUPTED, ReviewTag.INTERRUPTED),
    ],
)
def test_manual_review_reason_maps_to_its_tag(outcome: str, tag: str) -> None:
    """Every reason an attachment is held back has a tag."""
    assert categories_for([_row(EmailDecision.MANUAL_REVIEW, outcome)]) == [tag]


def test_manual_review_gets_one_tag_per_distinct_reason() -> None:
    """Two unreadable attachments give one Can't read, not two."""
    rows = [
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NON_PDF),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.UNREADABLE),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.WOULD_ENTER),
    ]

    assert categories_for(rows) == [ReviewTag.CANT_READ, ReviewTag.NO_UCN]


@pytest.mark.parametrize(
    ("stac_result", "tag"),
    [
        (StacResult.FAILED, ReviewTag.STAC_FAILED),
        (StacResult.UNKNOWN, ReviewTag.MAY_BE_SAVED),
    ],
)
def test_stac_failure_is_tagged(stac_result: str, tag: str) -> None:
    """An upload STAC turned down becomes Manual Review with STAC's reason."""
    rows = [_row(EmailDecision.MANUAL_REVIEW, Outcome.WOULD_ENTER, stac_result)]

    assert categories_for(rows) == [tag]


def test_stac_tag_comes_before_other_reasons() -> None:
    """May be saved is what a person must check first, so it leads."""
    rows = [
        _row(EmailDecision.MANUAL_REVIEW, Outcome.PLS_RVW, StacResult.UNKNOWN),
        _row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN),
    ]

    assert categories_for(rows) == [ReviewTag.MAY_BE_SAVED, ReviewTag.NO_UCN]


def test_gone_email_gets_no_tag() -> None:
    """It is no longer in the folder, so there is nothing to tag."""
    assert categories_for([_row(EmailDecision.GONE, Outcome.GONE)]) == []


# merge_categories


def test_merge_keeps_staff_categories_and_replaces_old_dalyn_ones() -> None:
    """A re-run with a new result leaves one DALYN answer, and staff tags untouched."""
    existing = ["Staff category", ReviewTag.NO_UCN, "Another staff one"]

    assert merge_categories(existing, [ReviewTag.REHEARSED]) == [
        "Staff category",
        "Another staff one",
        ReviewTag.REHEARSED,
    ]


# tag_email


def test_tag_email_sends_merged_list() -> None:
    """The PATCH carries staff categories plus the new DALYN tag."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category"]}
    rows = [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]

    assert tag_email(client, "box@example.com", message, rows) is True
    assert client.calls == [("box@example.com", "message-1", ["Staff category", ReviewTag.NO_UCN])]


def test_tag_email_skips_write_when_nothing_changes() -> None:
    """A dry run re-reading the same email does not rewrite the same tag."""
    client = FakeClient()
    message = {"id": "message-1", "categories": [ReviewTag.NO_UCN]}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]) is False
    assert client.calls == []


def test_tag_email_skips_gone_email() -> None:
    """Nothing to tag, and nothing to write."""
    client = FakeClient()
    message = {"id": "message-1", "categories": []}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.GONE, Outcome.GONE)]) is False
    assert client.calls == []


def test_tag_email_handles_message_deleted_meanwhile() -> None:
    """Purged between processing and tagging: logged and skipped, run carries on."""
    client = FakeClient(error=MessageGone("404"))
    message = {"id": "message-1"}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]) is False


def test_tag_email_lets_graph_failure_stop_the_pass() -> None:
    """Untagged after filing would mean filing again next pass, so it is not swallowed."""
    client = FakeClient(error=SystemProblem("Graph PATCH failed with 500"))
    message = {"id": "message-1"}

    with pytest.raises(SystemProblem):
        tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)])


def test_gone_email_has_processing_tag_cleared() -> None:
    """Moved away mid-pass: Processing must not follow it to its new folder."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category", ReviewTag.PROCESSING]}

    assert tag_email(client, "box@example.com", message, [_row(EmailDecision.GONE, Outcome.GONE)]) is True
    assert client.calls == [("box@example.com", "message-1", ["Staff category"])]


# mark_processing


def test_mark_processing_replaces_old_dalyn_tags_and_keeps_staff_ones() -> None:
    """A re-read email shows Processing, not last pass's result, while it is worked on."""
    client = FakeClient()
    message = {"id": "message-1", "categories": ["Staff category", ReviewTag.NO_UCN]}

    mark_processing(client, "box@example.com", message, 1)

    assert client.calls == [("box@example.com", "message-1", ["Staff category", ReviewTag.PROCESSING])]
    assert message["categories"] == ["Staff category", ReviewTag.PROCESSING]


def test_result_tag_replaces_processing() -> None:
    """The end-of-email write swaps Processing for the result."""
    client = FakeClient()
    message = {"id": "message-1", "categories": []}
    mark_processing(client, "box@example.com", message, 1)

    tag_email(client, "box@example.com", message, [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)])

    assert client.calls[-1] == ("box@example.com", "message-1", [ReviewTag.NO_UCN])


def test_mark_processing_on_vanished_email_leaves_message_unchanged() -> None:
    """Gone before it was marked: logged, and the folder check finds it GONE."""
    client = FakeClient(error=MessageGone("404"))
    message = {"id": "message-1", "categories": ["Staff category"]}

    mark_processing(client, "box@example.com", message, 1)

    assert message["categories"] == ["Staff category"]


def test_mark_processing_lets_graph_failure_stop_the_pass() -> None:
    """An email DALYN cannot mark is one it could not mark as done either."""
    client = FakeClient(error=SystemProblem("Graph PATCH failed with 500"))

    with pytest.raises(SystemProblem):
        mark_processing(client, "box@example.com", {"id": "message-1"}, 1)


# was_interrupted


def test_leftover_saving_tag_means_interrupted() -> None:
    """Saving is only on around the Save click; finding it means a run died there."""
    assert was_interrupted({"categories": ["Staff category", ReviewTag.SAVING]})


def test_leftover_queued_or_processing_is_not_interrupted() -> None:
    """Save was never pressed, so the email is simply handled again."""
    assert not was_interrupted({"categories": [ReviewTag.QUEUED]})
    assert not was_interrupted({"categories": [ReviewTag.PROCESSING]})


def test_finished_or_untouched_email_is_not_interrupted() -> None:
    """Only a leftover Saving counts."""
    assert not was_interrupted({"categories": [ReviewTag.FILED]})
    assert not was_interrupted({"categories": ["Staff category"]})
    assert not was_interrupted({"categories": []})
    assert not was_interrupted({})


# needs_handling


def test_untagged_email_needs_handling() -> None:
    """No DALYN category: never handled."""
    assert needs_handling({"categories": []})
    assert needs_handling({})
    assert needs_handling({"categories": ["Staff category"]})


def test_red_or_green_email_is_skipped() -> None:
    """A result tag means DALYN is done with it."""
    assert not needs_handling({"categories": [ReviewTag.FILED]})
    assert not needs_handling({"categories": [ReviewTag.READY_TO_SAVE]})
    assert not needs_handling({"categories": ["Staff category", ReviewTag.NO_UCN]})
    assert not needs_handling({"categories": [ReviewTag.INTERRUPTED]})


@pytest.mark.parametrize("tag", [ReviewTag.QUEUED, ReviewTag.PROCESSING, ReviewTag.SAVING])
def test_yellow_email_left_by_a_crash_comes_through(tag: str) -> None:
    """A run that stopped mid-batch must not strand its yellow emails."""
    assert needs_handling({"categories": ["Staff category", tag]})


# Fake Graph for queueing, saving and moving


class FakeGraph:
    """Stands in for the GraphClient calls tagging makes, recording each one."""

    def __init__(
        self,
        folder: str = "source-id",
        gone: frozenset[str] = frozenset(),
        error: Exception | None = None,
    ) -> None:
        """folder: where every email is now. gone: IDs that 404. error: raised by every call."""
        self.folder = folder
        self.gone = gone
        self.error = error
        self.calls: list[tuple] = []

    def _check(self, message_id: str) -> None:
        """Raise the scripted error, or MessageGone for a gone ID."""
        if self.error:
            raise self.error
        if message_id in self.gone:
            raise MessageGone("not found")

    def set_categories(self, mailbox: str, message_id: str, categories: list[str]) -> None:
        """Record a tag write."""
        self._check(message_id)
        self.calls.append(("tag", message_id, categories))

    def get_parent_folder_id(self, mailbox: str, message_id: str) -> str:
        """Return the scripted folder."""
        self._check(message_id)
        self.calls.append(("folder", message_id))
        return self.folder

    def move_message(self, mailbox: str, message_id: str, destination_id: str) -> None:
        """Record a move."""
        self._check(message_id)
        self.calls.append(("move", message_id, destination_id))


# mark_queued


def test_whole_batch_is_queued_keeping_staff_categories() -> None:
    """Every email goes yellow before any work, and staff tags stay."""
    client = FakeGraph()
    messages = [{"id": "m1", "categories": ["Staff category"]}, {"id": "m2"}]

    assert mark_queued(client, "box", messages) == 2
    assert client.calls == [
        ("tag", "m1", ["Staff category", ReviewTag.QUEUED]),
        ("tag", "m2", [ReviewTag.QUEUED]),
    ]
    assert messages[1]["categories"] == [ReviewTag.QUEUED]


def test_queueing_leaves_saving_for_the_interrupted_check() -> None:
    """Overwriting Saving would hide that a run died around Save."""
    client = FakeGraph()
    message = {"id": "m1", "categories": [ReviewTag.SAVING]}

    assert mark_queued(client, "box", [message]) == 0
    assert client.calls == []
    assert was_interrupted(message)


def test_queueing_replaces_a_leftover_processing() -> None:
    """Processing from a stopped run is safe to redo, so it is queued again."""
    client = FakeGraph()

    mark_queued(client, "box", [{"id": "m1", "categories": [ReviewTag.PROCESSING]}])

    assert client.calls == [("tag", "m1", [ReviewTag.QUEUED])]


def test_queueing_skips_a_vanished_email() -> None:
    """Gone between listing and queueing: the rest of the batch still gets tagged."""
    client = FakeGraph(gone=frozenset({"m1"}))

    assert mark_queued(client, "box", [{"id": "m1"}, {"id": "m2"}]) == 1
    assert client.calls == [("tag", "m2", [ReviewTag.QUEUED])]


def test_queueing_lets_graph_failure_stop_the_pass() -> None:
    """Staff must not be left unwarned while DALYN works on."""
    with pytest.raises(SystemProblem):
        mark_queued(FakeGraph(error=SystemProblem("Graph 500")), "box", [{"id": "m1"}])


# mark_saving


def test_saving_is_marked_when_the_email_is_still_there() -> None:
    """Checked fresh, then Processing becomes Saving."""
    client = FakeGraph()
    message = {"id": "m1", "categories": ["Staff category", ReviewTag.PROCESSING]}

    assert mark_saving(client, "box", message, "source-id", 1)
    assert client.calls == [("folder", "m1"), ("tag", "m1", ["Staff category", ReviewTag.SAVING])]
    assert was_interrupted(message)


def test_email_moved_by_a_person_is_not_saved() -> None:
    """Someone took it mid-upload and may be filing it by hand."""
    client = FakeGraph(folder="somewhere-else")
    message = {"id": "m1", "categories": [ReviewTag.PROCESSING]}

    assert not mark_saving(client, "box", message, "source-id", 1)
    assert client.calls == [("folder", "m1")]
    assert message["categories"] == [ReviewTag.PROCESSING]


def test_deleted_email_is_not_saved() -> None:
    """Gone entirely: same answer, do not press Save."""
    assert not mark_saving(FakeGraph(gone=frozenset({"m1"})), "box", {"id": "m1"}, "source-id", 1)


def test_saving_lets_graph_failure_stop_the_pass() -> None:
    """Unable to mark Saving means a crash after Save could not be recognized."""
    with pytest.raises(SystemProblem):
        mark_saving(FakeGraph(error=SystemProblem("Graph 500")), "box", {"id": "m1"}, "source-id", 1)


# should_move and move_email


def test_filed_email_should_move() -> None:
    """Only a document STAC actually saved counts as finished."""
    assert should_move([_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.ENTERED)])


@pytest.mark.parametrize(
    "rows",
    [
        [_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.REACHED_SAVE)],
        [_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.REHEARSED)],
        [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)],
        [_row(EmailDecision.MANUAL_REVIEW, Outcome.WOULD_ENTER, StacResult.UNKNOWN)],
        [_row(EmailDecision.MANUAL_REVIEW, Outcome.INTERRUPTED)],
        [_row(EmailDecision.GONE, Outcome.GONE)],
    ],
)
def test_everything_else_stays_put(rows: list[dict]) -> None:
    """Green mail stays for staff; unsaved and gone mail is not finished here."""
    assert not should_move(rows)


FILED = [_row(EmailDecision.UPLOAD, Outcome.WOULD_ENTER, StacResult.ENTERED)]


def test_filed_email_is_moved_to_the_done_folder() -> None:
    """The done folder's ID is what reaches Graph."""
    client = FakeGraph()

    assert move_email(client, "box", {"id": "m1"}, FILED, "done-id", "source-id")
    assert client.calls == [("move", "m1", "done-id")]


def test_review_email_is_not_moved() -> None:
    """Green mail stays where staff work."""
    client = FakeGraph()
    rows = [_row(EmailDecision.MANUAL_REVIEW, Outcome.NO_UCN)]

    assert not move_email(client, "box", {"id": "m1"}, rows, "done-id", "source-id")
    assert client.calls == []


def test_move_into_the_source_folder_is_skipped() -> None:
    """done_folder pointed at the source: nothing to do."""
    client = FakeGraph()

    assert not move_email(client, "box", {"id": "m1"}, FILED, "source-id", "source-id")
    assert client.calls == []


def test_move_of_vanished_email_is_skipped() -> None:
    """Someone else moved or deleted it first, which is not a failure."""
    client = FakeGraph(gone=frozenset({"m1"}))

    assert not move_email(client, "box", {"id": "m1"}, FILED, "done-id", "source-id")


def test_move_lets_graph_failure_stop_the_pass() -> None:
    """Any other failure is DALYN's problem, not this email's."""
    client = FakeGraph(error=SystemProblem("Graph 500"))

    with pytest.raises(SystemProblem):
        move_email(client, "box", {"id": "m1"}, FILED, "done-id", "source-id")
