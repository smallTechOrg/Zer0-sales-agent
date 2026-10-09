"""
One place that configures logging for this app.

Every module logs through ``logging.getLogger(__name__)`` and never configures
anything itself. The entry points call :func:`configure_logging` once, early:
``app.py`` before it imports the rest of the project, and the ``__main__``
blocks of the scripts that run on their own.

Output goes to stdout, one line per record, with the time, the level and the
module that logged it::

    2026-10-09 17:32:47 INFO     db  Table 'prompts' created/verified

Deployment runs ``flask run ... > flask.log 2>&1`` (see scripts/deploy.sh) and
reads the tail of that file when a deploy fails, so the format stays readable
for a human with ``tail``.

Unlike the rest of the app this module reads the environment directly instead
of importing ``config``: ``config`` logs while it is being imported, so the
level has to be known before that import happens.
"""
import logging
import os
import sys

# Level for this app's own loggers. One of DEBUG, INFO, WARNING, ERROR.
DEFAULT_LEVEL = "INFO"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Libraries that log a line per HTTP request or per connection. At DEBUG they
# bury this app's own messages, and they carry request bodies and headers.
# Raise one of these by hand when you are debugging that library.
NOISY_LOGGERS = ("urllib3", "httpx", "httpcore", "groq", "openai")

_configured = False


def configure_logging(level: str = None, stream=None) -> None:
    """
    Attach one stdout handler to the root logger and set the level.

    Safe to call more than once: only the first call does anything, so an
    entry point that is also imported elsewhere cannot add a second handler
    and log every line twice.

    *level* defaults to the LOG_LEVEL environment variable, then to
    DEFAULT_LEVEL. An unknown name falls back to DEFAULT_LEVEL rather than
    failing: a typo in .env must not stop the app from starting.
    """
    global _configured
    if _configured:
        return

    name = (level or os.getenv("LOG_LEVEL") or DEFAULT_LEVEL).strip().upper()
    resolved = getattr(logging, name, None)
    if not isinstance(resolved, int):
        resolved = getattr(logging, DEFAULT_LEVEL)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    root = logging.getLogger()
    # Flask and Werkzeug add a handler of their own only when the root logger
    # has none, so configuring the root first keeps their lines in this format.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(resolved)

    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(resolved, logging.WARNING))

    _configured = True


def redact_url(url: str) -> str:
    """
    Hide the password in a database URL so it can be logged.

    ``postgresql://user:secret@host:5432/db`` becomes
    ``postgresql://user:***@host:5432/db``. A URL with no credentials is
    returned unchanged.
    """
    if not url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest:
        return url
    credentials, _, host = rest.rpartition("@")
    if ":" not in credentials:
        return url
    user, _, _password = credentials.partition(":")
    return f"{scheme}://{user}:***@{host}"
