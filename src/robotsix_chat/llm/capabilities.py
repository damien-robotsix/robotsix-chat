"""Per-attempt model-capability flags shared between the agent and tools.

The agent resolves which provider slot serves a turn only inside llmio's
failover loop, but tools decide their return shape when they run.  A
``BinaryContent`` image block in a tool result 404s the whole turn on
text-only OpenRouter models ("No endpoints found that support image
input" — live incident 2026-09-01, correlation f2bfbf2b), so
image-returning tools must know whether the model that will read their
result can see images at all.

The agent stamps the active slot's capability into a :class:`ContextVar`
right before running the model; tools executed inside that run read it
via :func:`model_supports_images`.  The default is ``True`` — the Claude
transport reads images natively, and an unknown context should not
degrade tool output.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_MODEL_SUPPORTS_IMAGES: ContextVar[bool] = ContextVar(
    "model_supports_images", default=True
)


def model_supports_images() -> bool:
    """Whether the model serving the current agent run can read image blocks."""
    return _MODEL_SUPPORTS_IMAGES.get()


def set_model_supports_images(supported: bool) -> Token[bool]:
    """Stamp the active slot's image capability; returns the reset token."""
    return _MODEL_SUPPORTS_IMAGES.set(supported)


def reset_model_supports_images(token: Token[bool]) -> None:
    """Restore the capability flag to its value before the matching set."""
    _MODEL_SUPPORTS_IMAGES.reset(token)


#: Standard text appended to a tool's textual output in place of an image
#: block when the serving model cannot read images.  Mentioning the reason
#: keeps the model from retrying the same tool expecting pixels.
IMAGE_OMITTED_NOTE = (
    "[Image omitted: the current model cannot view images. Work from the "
    "textual metadata above; do not retry expecting to see the image.]"
)
