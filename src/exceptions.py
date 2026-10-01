"""Named error types for DALYN.

Every DALYN error is either a DocumentProblem (the email goes to Manual Review)
or a SystemProblem (DALYN itself is broken, so alert Nick). main.py routes on
which family an error belongs to, not on individual error types.

MessageGone is the one exception to that: the email left the folder DALYN is
reading before DALYN got to it. Nothing is broken and there is nothing to
route, so it belongs to neither family.
"""


class DalynError(Exception):
    """Base class for every DALYN-specific error."""


class DocumentProblem(DalynError):
    """Something is wrong with an email or its PDFs. Route to Manual Review."""


class SystemProblem(DalynError):
    """Something is wrong with DALYN or a service it depends on. Alert Nick."""


class GraphAuthError(SystemProblem):
    """DALYN could not get an access token from Microsoft."""


class MessageGone(DalynError):
    """The email was moved, deleted or purged after it was listed. Log it and skip it."""