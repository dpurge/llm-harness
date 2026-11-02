"""Scrubbing for untrusted text that is placed inside a tagged block.

Used by every tool that wraps outside data (web pages, command output) in a
block followed by a fixed sentence. The cleaning removes what could end the
block early or pass for a harness message; it reduces prompt injection and does
not prevent it.
"""

import re

from llm_harness.display import sanitize_text

# Lines that start like a message the harness writes into the next observation.
# The names of these phrases come from context.py and orchestrator.py.
_HARNESS_LINE = re.compile(
    r"^(?=[ \t]*(?:"
    r"\[observation truncated"
    r"|Your previous reply was rejected"
    r"|You asked the user"
    r"|The user answered"
    r"|tool [a-z][a-z0-9_]* (?:result:|failed:|was not run)"
    r"|## "
    r"))",
    re.IGNORECASE | re.MULTILINE,
)


def scrub_block_text(text: str, tag: re.Pattern[str]) -> str:
    """Make untrusted text safe to place inside a block named by `tag`.

    Drops terminal and bidi control characters, removes every spelling of the
    block tag name (repeatedly, so removal cannot assemble a new one) and quotes
    lines that start like harness messages with `> `.
    """
    text = sanitize_text(text)
    while tag.search(text):
        text = tag.sub("", text)
    return _HARNESS_LINE.sub("> ", text)
