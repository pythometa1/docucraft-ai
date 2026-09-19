"""What an error is allowed to tell the person who caused it.

Exception text is written for whoever reads the server log: it names files on
this host, the libraries we read documents with, the model vendor, and the
settings that configure them. None of that belongs in an HTTP response, a stored
column a response returns, or a note in a generated archive -- the people who
see those are customers and recipients, and to them it is a map of how the
product is built.

So an unexpected exception is logged here, in full, and replaced by a sentence
written for the reader. The only exceptions passed through are the ones a caller
names as user-facing: domain refusals whose message was written for users to act
on. Deciding that is per call site, because it depends on what the exception
carries, not on its type in general.
"""

from __future__ import annotations

import logging

log = logging.getLogger("app.errors")


def public_message(
    exc: BaseException,
    fallback: str,
    *,
    user_facing: tuple[type[BaseException], ...] = (),
) -> str:
    """The text a client may see for `exc`: its own message if it is one of
    `user_facing`, otherwise `fallback` -- with the original logged."""
    if user_facing and isinstance(exc, user_facing):
        return str(exc)
    log.error("%s (%s: %s)", fallback, type(exc).__name__, exc, exc_info=exc)
    return fallback
