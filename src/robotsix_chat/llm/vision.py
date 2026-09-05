"""Image captioning via the configured fallback vision model.

Phase 2b of image support: a small, self-contained helper that turns an
attached image into a short text caption using the model named by
:attr:`robotsix_chat.config.settings.Settings.vision_model` (an OpenRouter
model id such as ``openrouter/<vendor>/<model>``).

The chat agent already lets *text-only* OpenRouter slots interrogate images
through llmio's ``ask_image`` tool (see
:mod:`robotsix_llmio.core.image_tool`).  This helper serves the *other*
direction Phase 2c needs: when the active chat model cannot see images at
all, the pipeline substitutes a caption for the image into the message turn.
It therefore builds a one-shot provider from the configured ``vision_model``
and asks it to describe the picture.

Design mirrors :func:`robotsix_llmio.core.image_tool._ask_vision_model`:

* A fresh provider + agent is built per call — an image caption is rare and
  the httpx client must not outlive the request.
* Failures NEVER raise into the caller (the chat path): every error is
  logged with context and the helper returns an empty string, which the
  caller reads as "no caption available" and falls back to its curated
  no-image-support message.

The ``vision_model`` config value uses a ``<provider>/<model-slug>`` shape
(``openrouter/<vendor>/<model>``); llmio's factory expects a combined
``<provider>-<model-name>`` identifier (the provider prefix is split on the
first hyphen).  :func:`_to_llmio_identifier` bridges the two by turning the
first ``/`` into a ``-``.
"""

from __future__ import annotations

import logging

from robotsix_llmio.core.factory import get_provider_for_identifier
from robotsix_llmio.core.identifier import parse_model_identifier

from robotsix_chat.config import Settings

logger = logging.getLogger(__name__)

#: Hard cap on a returned caption — the caller's message turn is the scarce
#: resource, so a vision model rambling about a screenshot must not crowd it
#: out.  Mirrors ``robotsix_llmio.core.image_tool._MAX_ANSWER_CHARS``.
_MAX_CAPTION_CHARS = 2000

#: System prompt handed to the vision model.  Kept terse and factual so the
#: caption is a faithful stand-in for the image rather than commentary.
_CAPTION_SYSTEM_PROMPT = (
    "You caption a single attached image for a text-only assistant that "
    "cannot see it. Describe the image precisely and concisely in a few "
    "sentences. Transcribe any visible text verbatim. Do not add commentary, "
    "opinions, or speculation about intent — only what is actually shown."
)

#: The instruction turn accompanying the image binary.
_CAPTION_PROMPT = "Caption this image."


def _to_llmio_identifier(vision_model: str) -> str:
    """Convert a ``vision_model`` config value to an llmio tier identifier.

    The config uses ``<provider>/<model-slug>`` (e.g.
    ``openrouter/<vendor>/<model>``) while
    :func:`robotsix_llmio.core.factory.get_provider_for_identifier` expects
    ``<provider>-<model-name>`` (the provider prefix is split on the first
    hyphen, so the model name may itself contain hyphens and slashes).  This
    turns the first ``/`` into a ``-``; a value with no ``/`` is returned
    unchanged (already in identifier form).
    """
    provider, sep, model_slug = vision_model.partition("/")
    if not sep:
        return vision_model
    return f"{provider}-{model_slug}"


async def generate_caption(
    media_type: str,
    data: bytes,
    *,
    settings: Settings | None = None,
) -> str:
    """Caption *data* (an image of *media_type*) via the configured vision model.

    Reads :attr:`~robotsix_chat.config.settings.Settings.vision_model` at
    call time (loading config from ``ROBOTSIX_CONFIG_FILE`` when *settings*
    is not supplied) and asks that model for a short textual description
    suitable for substituting into a message turn.

    Never raises into the caller: on any failure — an unconfigured vision
    model, an unreachable provider, invalid image data, or an API error —
    the error is logged with context and an empty string is returned.  The
    caller reads an empty string as "no caption available".

    Args:
        media_type: The image's media type, e.g. ``"image/png"``.
        data: The raw image bytes.
        settings: Optional pre-loaded settings.  When ``None``, settings are
            loaded via :meth:`Settings.load`.

    Returns:
        The caption text (capped at :data:`_MAX_CAPTION_CHARS`), or an empty
        string when captioning was not possible.

    """
    try:
        cfg = settings if settings is not None else Settings.load()
    except Exception as exc:  # config load must never break the chat path
        logger.warning(
            "generate_caption: could not load settings (%s): %s",
            type(exc).__name__,
            exc,
        )
        return ""

    vision_model = cfg.vision_model
    if not vision_model:
        logger.debug("generate_caption: no vision_model configured; skipping caption")
        return ""

    api_key = cfg.openrouter_api_key.get_secret_value()

    try:
        identifier = _to_llmio_identifier(vision_model)
        model_name = parse_model_identifier(identifier).model_name

        provider_kwargs: dict[str, object] = {}
        if api_key:
            provider_kwargs["api_key"] = api_key
        provider = get_provider_for_identifier(identifier, **provider_kwargs)

        handle = provider.build_agent(
            system_prompt=_CAPTION_SYSTEM_PROMPT,
            output_type=str,
            name="generate_caption",
            model=model_name,
        )
        try:
            from pydantic_ai.messages import BinaryContent

            result = await handle.run(
                [_CAPTION_PROMPT, BinaryContent(data=data, media_type=media_type)]
            )
        finally:
            handle.close()
    except Exception as exc:  # never raise into the chat path
        logger.warning(
            "generate_caption: vision model %r failed for %s image (%s): %s",
            vision_model,
            media_type,
            type(exc).__name__,
            exc,
        )
        return ""

    caption = str(result.output or "").strip()
    if len(caption) > _MAX_CAPTION_CHARS:
        caption = caption[:_MAX_CAPTION_CHARS] + " …[truncated]"
    return caption
