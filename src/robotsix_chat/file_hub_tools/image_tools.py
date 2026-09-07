"""Generic raster-image transforms (resize, compress, convert, strip metadata).

Pure, side-effect-light image manipulation built on Pillow — the same
imaging runtime already relied on by :mod:`pdf_tools`.  The transform set is
deliberately constrained to a handful of trivial binary-asset operations
(resize, re-encode/compress, format-convert, metadata-strip); there is no
arbitrary-code path.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard ceiling on the source image size we will decode (50 MB).  A larger
# object is rejected before Pillow ever touches it to bound memory use.
MAX_INPUT_BYTES = 52_428_800

# Default JPEG/WebP quality when the caller does not specify one.
DEFAULT_QUALITY = 75

# Map user-facing format tokens to Pillow format names.
_FORMAT_ALIASES: dict[str, str] = {
    "jpeg": "JPEG",
    "jpg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
}

# File extension for each supported output format.
_FORMAT_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


class ImageError(Exception):
    """Base exception for image-transform operations."""


class ImageNotDecodableError(ImageError):
    """The input bytes are not a decodable image."""


class ImageTooLargeError(ImageError):
    """The input image exceeds :data:`MAX_INPUT_BYTES`."""


def _resolve_format(operations: dict[str, Any], source_format: str) -> str:
    """Return the Pillow output format name for *operations*.

    Honours an explicit ``format`` op; otherwise keeps the source format
    when it is one we can re-emit, falling back to PNG (lossless) for any
    other source format.
    """
    requested = operations.get("format")
    if requested:
        out_format = _FORMAT_ALIASES.get(str(requested).strip().lower())
        if out_format is None:
            raise ImageError(
                f"Unsupported output format {requested!r}; "
                "choose one of: jpeg, png, webp."
            )
        return out_format
    return _FORMAT_ALIASES.get(source_format.strip().lower(), "PNG")


def _resolve_quality(operations: dict[str, Any]) -> int:
    """Return a validated 1-100 quality value (default :data:`DEFAULT_QUALITY`)."""
    quality = operations.get("quality", DEFAULT_QUALITY)
    if isinstance(quality, bool) or not isinstance(quality, int):
        raise ImageError("quality must be an integer between 1 and 100.")
    if not 1 <= quality <= 100:
        raise ImageError("quality must be an integer between 1 and 100.")
    return quality


def _resolve_dimension(value: Any, fallback: int, key: str) -> int:
    """Return a validated positive resize dimension.

    ``None`` (the key was omitted) falls back to *fallback* — the source
    dimension.  Any other value must be a positive, non-bool integer;
    strings, floats, negatives and ``0`` raise :class:`ImageError` rather
    than escaping as a raw ``TypeError``/``ValueError``.
    """
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImageError(f"resize {key} must be a positive integer.")
    if value <= 0:
        raise ImageError(f"resize {key} must be a positive integer.")
    return value


def transform_image(
    src_path: Path,
    operations: dict[str, Any],
    output_dir: Path,
    output_filename: str | None = None,
) -> dict[str, Any]:
    """Apply a constrained set of transforms to an image and write the result.

    Args:
        src_path: Path to the source image file.
        operations: Mapping with optional keys:
            - ``resize`` — ``{"max_width": int, "max_height": int}`` (either
              key optional); aspect ratio is preserved and the image is
              never upscaled.
            - ``quality`` — JPEG/WebP quality 1-100 (default 75).
            - ``format`` — output format ``jpeg`` | ``png`` | ``webp``
              (default: keep the source format).
            - ``strip_metadata`` — bool (default ``True``); strip
              EXIF/ICC/other metadata.
        output_dir: Directory to write the transformed file into.
        output_filename: Optional filename for the result (its extension is
            normalised to match the output format).  When omitted, derived
            from the source stem.

    Returns:
        A dict with ``output_path`` (:class:`~pathlib.Path`),
        ``input_bytes``, ``output_bytes``, ``input_width``,
        ``input_height``, ``output_width``, ``output_height`` and
        ``output_format``.

    Raises:
        ImageTooLargeError: The source exceeds :data:`MAX_INPUT_BYTES`.
        ImageNotDecodableError: The source is not a decodable image.
        ImageError: An operation is invalid or saving failed.

    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        raise ImageError(
            "Image tools require the ``Pillow`` package. "
            "Install it with: pip install pillow"
        ) from None

    if not src_path.exists():
        raise ImageError(f"Source image not found: {src_path}")

    data = src_path.read_bytes()
    input_bytes = len(data)
    if input_bytes > MAX_INPUT_BYTES:
        raise ImageTooLargeError(
            f"Image is {input_bytes} bytes, exceeding the "
            f"{MAX_INPUT_BYTES} byte ({MAX_INPUT_BYTES // (1024 * 1024)} MB) limit."
        )

    image: Image.Image
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageNotDecodableError(
            f"Could not decode {src_path.name!r} as an image: {exc}"
        ) from exc

    source_format = (image.format or "").upper()
    input_width, input_height = image.size

    out_format = _resolve_format(operations, source_format)
    quality = _resolve_quality(operations)
    strip_metadata = operations.get("strip_metadata", True)

    # Resize (preserve aspect ratio, never upscale).  ``thumbnail`` only ever
    # shrinks, so an oversized target is a no-op.
    resize = operations.get("resize") or {}
    if resize:
        if not isinstance(resize, dict):
            raise ImageError("resize must be an object with max_width/max_height.")
        max_width = resize.get("max_width")
        max_height = resize.get("max_height")
        if max_width is not None or max_height is not None:
            target_w = _resolve_dimension(max_width, input_width, "max_width")
            target_h = _resolve_dimension(max_height, input_height, "max_height")
            image.thumbnail((target_w, target_h), Image.Resampling.LANCZOS)

    save_kwargs: dict[str, Any] = {}
    if not strip_metadata:
        exif = image.info.get("exif")
        icc = image.info.get("icc_profile")
        if exif:
            save_kwargs["exif"] = exif
        if icc:
            save_kwargs["icc_profile"] = icc

    if out_format == "JPEG":
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        save_kwargs.update(quality=quality, optimize=True)
    elif out_format == "WEBP":
        save_kwargs.update(quality=quality, method=6)
    elif out_format == "PNG":
        save_kwargs.update(optimize=True)

    ext = _FORMAT_EXT[out_format]
    if output_filename:
        name = Path(output_filename).name
        stem = Path(name).stem or src_path.stem
        name = stem + ext
    else:
        name = src_path.stem + "_transformed" + ext
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / name

    try:
        image.save(output_path, format=out_format, **save_kwargs)
    except (OSError, ValueError) as exc:
        raise ImageError(f"Failed to save transformed image: {exc}") from exc

    output_bytes = output_path.stat().st_size
    with Image.open(output_path) as out_image:
        output_width, output_height = out_image.size

    logger.info(
        "Transformed %s (%dx%d, %d B) -> %s (%dx%d, %d B, %s)",
        src_path.name,
        input_width,
        input_height,
        input_bytes,
        output_path.name,
        output_width,
        output_height,
        output_bytes,
        out_format,
    )

    return {
        "output_path": output_path,
        "input_bytes": input_bytes,
        "output_bytes": output_bytes,
        "input_width": input_width,
        "input_height": input_height,
        "output_width": output_width,
        "output_height": output_height,
        "output_format": out_format,
    }
