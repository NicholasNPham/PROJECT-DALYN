"""Decides which DALYN categories an email gets, and puts them on it in Outlook.

Kept out of main.py so the decision can be tested without importing the
script that reads live mail. main.py calls tag_email once per email, after
STAC has finished with it.
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
}

STAC_FAILURE_TAGS = {
    StacResult.UNKNOWN: ReviewTag.MAY_BE_SAVED,
    StacResult.FAILED: ReviewTag.STAC_FAILED,
}


def categories_for(rows: list[dict]) -> list[str]:
    """Return the DALYN categories one email should carry, once STAC is done.

    An uploaded email gets the tag for how far STAC got, plus NO_RULE if any
    attachment went in under the review pair. A Manual Review email gets one
    tag per distinct reason, STAC's first: "may be saved" is the one a person
    must act on before anything else. A GONE email gets none, since it is no
    longer in the folder to tag.

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


def tag_email(client: GraphClient, mailbox: str, message: dict, rows: list[dict]) -> bool:
    """Put this email's DALYN categories on it in Outlook.

    Called after STAC, so the tag says what actually happened. Skips the
    write when the categories would not change, which is the normal case on
    a dry run that re-reads the same mail every pass.

    Returns:
        True when the email was tagged, False when there was nothing to do or
        it disappeared first.

    Raises:
        SystemProblem: Any Graph failure other than the email being gone.
            Not caught here on purpose: once Save is on, an email that was
            filed but could not be tagged would be filed again next pass.
    """
    tags = categories_for(rows)
    if not tags:
        return False

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
    logger.info("Email %s: tagged %s", rows[0]["email_number"], ", ".join(tags))
    return True
