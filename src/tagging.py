"""Decides which DALYN categories an email gets, and puts them on it in Outlook.

Kept out of main.py so the decision can be tested without importing the
script that reads live mail.

An email passes through the in-progress tags in order: mark_queued tags the
whole batch before any work starts, mark_processing the one being worked, and
mark_saving just before Save. tag_email then replaces them with the red or
green result, and move_email takes filed mail out of the folder.
"""

from exceptions import MessageGone
from graph_client import GraphClient
from logger import get_logger
from models import EmailDecision, Outcome, ReviewTag, StacResult

logger = get_logger(__name__)

# How far STAC got with an uploaded email, as the tag staff see. Keyed on the
# STAC result rather than the switches, so the tag reports what happened.
UPLOAD_TAGS = {
    StacResult.ENTERED: ReviewTag.FILED,
    StacResult.REACHED_SAVE: ReviewTag.READY_TO_SAVE,
    StacResult.REHEARSED: ReviewTag.REHEARSED,
}

# Why an email was held back, one tag per attachment outcome. Non-PDF, too
# large and unreadable share a tag because staff do the same thing for all
# three: open it and look. The CSV keeps the difference.
REVIEW_TAGS = {
    Outcome.NO_FILES: ReviewTag.NO_ATTACHMENTS,
    Outcome.NON_PDF: ReviewTag.CANT_READ,
    Outcome.TOO_LARGE: ReviewTag.CANT_READ,
    Outcome.UNREADABLE: ReviewTag.CANT_READ,
    Outcome.NO_UCN: ReviewTag.NO_UCN,
    Outcome.UCN_CONFLICT: ReviewTag.UCN_CONFLICT,
    Outcome.UCN_OTHER_COUNTY: ReviewTag.OTHER_COUNTY,
    Outcome.INTERRUPTED: ReviewTag.INTERRUPTED,
}

STAC_FAILURE_TAGS = {
    StacResult.UNKNOWN: ReviewTag.MAY_BE_SAVED,
    StacResult.FAILED: ReviewTag.STAC_FAILED,
}

# Queued, Processing and Saving: DALYN still has the email. By name, not by
# color: they share red with Filed, and Filed must not come back through.
IN_PROGRESS_TAGS = ReviewTag.in_progress()


def categories_for(rows: list[dict]) -> list[str]:
    """Return the DALYN categories one email should carry, once STAC is done.

    An uploaded email gets the tag for how far STAC got, plus NO_RULE if any
    attachment went in under the review pair. A Manual Review email gets one
    tag per distinct reason, STAC's first: "may be saved" is the one a person
    must act on before anything else. A GONE email gets none: someone else
    has it now, so tag_email clears DALYN's tags off it instead.

    Raises:
        ValueError: If an uploaded email has no STAC result. That is a bug in
            the loop, and tagging it with a guess would hide it.
    """
    decision = rows[0]["email_decision"]

    if decision == EmailDecision.GONE:
        return []

    if decision == EmailDecision.UPLOAD:
        result = rows[0]["stac_result"]
        if result not in UPLOAD_TAGS:
            raise ValueError(f"Uploaded email has STAC result {result!r}, which has no tag")
        tags = [UPLOAD_TAGS[result]]
        if any(row["outcome"] == Outcome.PLS_RVW for row in rows):
            tags.append(ReviewTag.NO_RULE)
        return tags

    tags = [STAC_FAILURE_TAGS[row["stac_result"]] for row in rows if row["stac_result"] in STAC_FAILURE_TAGS]
    tags += [REVIEW_TAGS[row["outcome"]] for row in rows if row["outcome"] in REVIEW_TAGS]
    # One tag per reason, in first-seen order.
    return list(dict.fromkeys(tags))


def merge_categories(existing: list[str], dalyn_tags: list[str]) -> list[str]:
    """Keep every category staff set, and replace whatever DALYN set before.

    Graph's PATCH replaces the whole list, so leaving staff categories out
    here would delete them. Old DALYN tags are dropped so a re-run that
    reaches a different result does not leave both on the email.
    """
    return [category for category in existing if not ReviewTag.is_dalyn(category)] + dalyn_tags


def was_interrupted(message: dict) -> bool:
    """True when the email still carries Saving from an earlier run.

    Read from the categories as listed, before this pass marks the email
    itself. Saving goes on just before the Save click and is replaced by the
    result straight after, so finding it here means a run died around that
    click, and the document may already be on the case. A leftover Queued or
    Processing is not interrupted in this sense: Save was never pressed, so
    the email is simply handled again.
    """
    return ReviewTag.SAVING in (message.get("categories") or [])


def needs_handling(message: dict) -> bool:
    """True when skip_tagged should still let this email through.

    An email with no DALYN category has not been handled. One still carrying
    Queued, Processing or Saving was left mid-way by a run that stopped, and
    has to come through too: to be redone, or for the interrupted check to
    see it. Any other DALYN tag (Filed, or a green reason) means dealt with.
    """
    categories = message.get("categories") or []
    dalyn = [category for category in categories if ReviewTag.is_dalyn(category)]
    return not dalyn or any(category in IN_PROGRESS_TAGS for category in dalyn)


def mark_queued(client: GraphClient, mailbox: str, messages: list[dict]) -> int:
    """Tag the whole batch Queued, before DALYN starts on any of it.

    Staff work the same folder, and this is what tells them which emails to
    leave alone. Updates each message["categories"] to what was written.

    An email carrying Saving is skipped: the interrupted check must still
    see that tag when the loop reaches it. An email that vanished since the
    listing is logged and left for process_message to find GONE.

    Returns:
        How many emails were tagged.

    Raises:
        SystemProblem: Any Graph failure other than the email being gone.
            Not caught: a batch DALYN cannot mark is one staff were not
            warned about.
    """
    queued = 0
    for message in messages:
        if was_interrupted(message):
            continue

        existing = message.get("categories") or []
        merged = merge_categories(existing, [ReviewTag.QUEUED])
        if merged == existing:
            continue

        try:
            client.set_categories(mailbox, message["id"], merged)
        except MessageGone as error:
            logger.info("An email left the folder before it could be queued (%s)", error)
            continue

        message["categories"] = merged
        queued += 1

    logger.info("%s: queued %s email(s)", mailbox, queued)
    return queued


def mark_processing(client: GraphClient, mailbox: str, message: dict, email_number: int) -> None:
    """Tag the email Processing, before anything else is done with it.

    Updates message["categories"] to what was written, so tag_email later
    compares against the email as it now is and replaces Processing with
    the result.

    An email that vanished between listing and now is left for
    process_message to find GONE, which it will, so this just logs it.

    Raises:
        SystemProblem: Any Graph failure other than the email being gone.
            Not caught: an email DALYN cannot mark is one it could not mark
            as done either.
    """
    merged = merge_categories(message.get("categories") or [], [ReviewTag.PROCESSING])

    try:
        client.set_categories(mailbox, message["id"], merged)
    except MessageGone as error:
        logger.info("Email %s: not marked Processing, it is gone (%s)", email_number, error)
        return

    message["categories"] = merged


def mark_saving(
    client: GraphClient, mailbox: str, message: dict, source_folder_id: str, email_number: int
) -> bool:
    """Check the email is still DALYN's, then tag it Saving. Call just before Save.

    Staff share the folder. If someone moved the email while DALYN was
    uploading it, they may be filing it by hand, and pressing Save would put
    it on the case twice. So the email's folder is read fresh from Graph
    first, and Save goes ahead only if it has not moved.

    Returns:
        True when Save may be pressed. False when the email has moved or is
        gone, in which case nothing is tagged and Save must not be pressed.

    Raises:
        SystemProblem: Any other Graph failure. Not caught: if DALYN cannot
            mark Saving, a crash after Save could not be recognized later.
    """
    try:
        if client.get_parent_folder_id(mailbox, message["id"]) != source_folder_id:
            logger.warning("Email %s: moved by someone while DALYN worked on it. Not saving.", email_number)
            return False
        merged = merge_categories(message.get("categories") or [], [ReviewTag.SAVING])
        client.set_categories(mailbox, message["id"], merged)
    except MessageGone as error:
        logger.warning("Email %s: gone while DALYN worked on it, not saving (%s)", email_number, error)
        return False

    message["categories"] = merged
    return True


def tag_email(client: GraphClient, mailbox: str, message: dict, rows: list[dict]) -> bool:
    """Replace the email's DALYN categories with the result, in Outlook.

    Called after STAC, so the tag says what actually happened. A GONE email
    has its DALYN tags removed rather than left alone, so the Processing tag
    does not follow it into whatever folder someone moved it to. Skips the
    write when the categories would not change.

    Returns:
        True when the email's categories were changed, False when there was
        nothing to do or it disappeared first.

    Raises:
        SystemProblem: Any Graph failure other than the email being gone.
            Not caught here on purpose: once Save is on, an email that was
            filed but could not be tagged would be filed again next pass.
    """
    tags = categories_for(rows)
    existing = message.get("categories") or []
    merged = merge_categories(existing, tags)
    if merged == existing:
        return False

    try:
        client.set_categories(mailbox, message["id"], merged)
    except MessageGone as error:
        logger.info("Email %s: not tagged, it left the folder (%s)", rows[0]["email_number"], error)
        return False

    # Only DALYN's own tags are logged. Staff categories are free text and
    # could name a party.
    logger.info("Email %s: tagged %s", rows[0]["email_number"], ", ".join(tags) or "(DALYN tags cleared)")
    return True


def should_move(rows: list[dict]) -> bool:
    """True when this email is finished and should leave the folder.

    Only an email STAC actually filed. One that reached Save or was
    rehearsed stays put: nothing was filed. Mail for a person stays put
    too, tagged green, because staff work it where it is. A GONE email is
    already somewhere else.
    """
    return rows[0]["email_decision"] == EmailDecision.UPLOAD and rows[0]["stac_result"] == StacResult.ENTERED


def move_email(
    client: GraphClient,
    mailbox: str,
    message: dict,
    rows: list[dict],
    done_folder_id: str,
    source_folder_id: str,
) -> bool:
    """Move a filed email to the done folder.

    Called after tag_email, so the email carries its red tag with it. A move
    into the folder the email is already in is skipped, for a config that
    points done_folder at the source.

    Args:
        done_folder_id: From get_move_target_id.
        source_folder_id: The folder the email was listed from.

    Returns:
        True when the email was moved.

    Raises:
        SystemProblem: Any Graph failure other than the email being gone.
            Not caught: the email is already tagged red, so the next pass
            will not file it again, and a person can move it by hand.
    """
    if not should_move(rows) or done_folder_id == source_folder_id:
        return False

    try:
        client.move_message(mailbox, message["id"], done_folder_id)
    except MessageGone as error:
        logger.info("Email %s: not moved, it left the folder (%s)", rows[0]["email_number"], error)
        return False

    logger.info("Email %s: moved to the done folder", rows[0]["email_number"])
    return True
