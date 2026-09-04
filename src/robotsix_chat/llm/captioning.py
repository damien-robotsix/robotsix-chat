"""Image captioning client — one-shot captions from the configured vision model.

When the serving chat model cannot read images (``model_supports_images()``
is ``False``), image-returning tools (``render_pdf_page``, ``render_url``)
currently degrade to a curated :data:`IMAGE_OMITTED_NOTE`.  This module lets
those tools instead send the image to the configured ``vision_model``
(OpenRouter) for a one-shot text caption, so a text-only model still gets the
picture's content.

The vision call is routed through robotsix-llmio's provider factory exactly
like the agent's own ``ask_image`` binding: the configured model id resolves
to an OpenRouter provider, a fresh pydantic-ai agent is built for the call,
and the image travels as a native ``BinaryContent`` message.  The call is
rare and stateless, so a fresh provider + handle is built per caption and
closed deterministically.

Everything here never raises: an unconfigured model, a provider/network
error, or an empty response is logged and yields ``None`` so callers fall
back to the curated omit-note path unchanged.
"""

from __future__ import annotations

import logging

from robotsix_chat.llm.capabilities import IMAGE_OMITTED_NOTE

logger = logging.getLogger(__name__)

#: Default cap on caption length — a verbose vision model must not blow the
#: text-only serving model's context; longer captions are truncated.
MAX_CAPTION_CHARS = 500

#: System prompt for the one-shot captioning call.  Instructs the vision
#: model to transcribe text and describe the image so the caption is useful
#: to a model that never sees the pixels.
_CAPTION_SYSTEM_PROMPT = (
    "You caption a single attached image for a text-only chat model that "
    "cannot see it. Describe what is visibly present — subject, layout, and "
    "any legible text — precisely and concisely in a few sentences. If there "
    "is no discernible content, say so plainly."
)

#: The user-turn prompt for the one-shot captioning call.
_CAPTION_PROMPT = "Caption this image."


def vision_identifier(vision_model: str) -> str:
    """Translate a config ``vision_model`` to llmio's combined provider-model id.

    The config stores OpenRouter model ids in the ``openrouter/<org>/<model>``
    form (matching OpenRouter's public model id), while robotsix-llmio's
    combined identifier uses a hyphen between the provider prefix and the
    model path (``openrouter-<org>/<model>``).  Already-combined ids are
    passed through untouched.
    """
    if vision_model.startswith("openrouter/"):
        return "openrouter-" + vision_model[len("openrouter/") :]
    return vision_model


def vision_model_name(vision_model: str) -> str:
    """Return the bare model path (without any provider prefix) for *vision_model*."""
    for prefix in ("openrouter-", "openrouter/"):
        if vision_model.startswith(prefix):
            return vision_model[len(prefix) :]
    return vision_model


async def caption_image(
    image_bytes: bytes,
    media_type: str,
    *,
    vision_model: str,
    vision_api_key: str | None = None,
    max_caption_chars: int = MAX_CAPTION_CHARS,
) -> str | None:
    """Return a one-shot caption for *image_bytes*, or ``None`` on any failure.

    Args:
        image_bytes: Raw image payload (e.g. PNG bytes).
        media_type: Image MIME type (e.g. ``image/png``).
        vision_model: Configured vision model id (``Settings.vision_model``).
            An empty string means "vision model unconfigured" and returns
            ``None`` without attempting a call.
        vision_api_key: OpenRouter API key for the vision call.  ``None``
            falls back to the provider's own key resolution
            (``OPENROUTER_API_KEY``).
        max_caption_chars: Maximum caption length; longer captions are
            truncated with an ellipsis.

    Returns:
        The caption text (stripped, possibly truncated), or ``None`` when the
        vision model is unconfigured, the call fails, or the model returns an
        empty answer.  Never raises.

    """
    if not vision_model:
        return None
    try:
        from pydantic_ai.messages import BinaryContent
        from robotsix_llmio.core.factory import get_provider_for_identifier
        from robotsix_llmio.core.retry import acall_with_retry

        provider = get_provider_for_identifier(
            vision_identifier(vision_model),
            api_key=vision_api_key or None,
        )
        handle = provider.build_agent(
            system_prompt=_CAPTION_SYSTEM_PROMPT,
            output_type=str,
            name="caption_image",
            model=vision_model_name(vision_model),
        )
        try:
            result = await acall_with_retry(
                lambda: handle.run(
                    [
                        _CAPTION_PROMPT,
                        BinaryContent(data=image_bytes, media_type=media_type),
                    ]
                ),
                what="caption_image",
                is_transient_fn=provider._is_transient,
            )
            caption = str(result.output).strip()
        finally:
            handle.close()
    except Exception as exc:  # never raise into the tool loop
        logger.warning(
            "caption_image failed (%s): %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return None

    if not caption:
        return None
    if len(caption) > max_caption_chars:
        caption = caption[:max_caption_chars].rstrip() + "…"
    return caption


async def caption_or_omit_note(
    metadata: str,
    image_bytes: bytes,
    media_type: str,
    *,
    vision_model: str,
    vision_api_key: str | None = None,
) -> str:
    """Return *metadata* plus an image caption, or the curated omit note.

    When *vision_model* is configured, the image is sent for captioning and
    a ``[Image caption: …]`` note is appended to *metadata*; when it is not
    configured (or the caption call fails/returns nothing), the existing
    curated :data:`IMAGE_OMITTED_NOTE` path applies unchanged.  Never raises.
    """
    caption = await caption_image(
        image_bytes,
        media_type,
        vision_model=vision_model,
        vision_api_key=vision_api_key,
    )
    if caption:
        return f"{metadata}\n[Image caption: {caption}]"
    return f"{metadata}\n{IMAGE_OMITTED_NOTE}"
