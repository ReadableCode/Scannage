"""Photo processing: whatever was uploaded becomes one upright JPEG and a thumbnail.

The pixels are copied onto a new image before saving, so nothing the upload
carried (EXIF, location, comments, colour profile) reaches the stored file.
"""

from __future__ import annotations

import io

from PIL import Image, ImageCms, ImageOps

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_EDGE = 1600
THUMB_EDGE = 320
MAX_PER_OWNER = 12

# Judged by what decodes, never by the Content-Type the client sent.
FORMATS = ("JPEG", "PNG", "WEBP")
JPEG_QUALITY = 85
THUMB_QUALITY = 80


class PhotoError(ValueError):
    """The upload cannot be stored as a photo. The message is safe to show."""


def _to_srgb(image: Image.Image, profile: bytes | None) -> Image.Image:
    """Colours of a wide gamut photo stay right once its profile is gone. Best effort."""
    if not profile:
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        return ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB") or image
    except (ImageCms.PyCMSError, OSError, ValueError):
        return image


def _flatten(image: Image.Image) -> Image.Image:
    """A new RGB image holding only the pixels, with anything see-through laid over white."""
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        image = image.convert("RGBA")
    elif image.mode == "RGB":
        image = _to_srgb(image, image.info.get("icc_profile"))
    else:
        image = image.convert("RGB")
    clean = Image.new("RGB", image.size, (255, 255, 255))
    clean.paste(image, mask=image.getchannel("A") if image.mode == "RGBA" else None)
    return clean


def _jpeg(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _decode(raw: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(raw), formats=FORMATS)
    except Exception as exc:
        raise PhotoError("the upload is not a JPEG, PNG or WebP image") from exc
    width, height = image.size
    # checked on the header, before any pixel is decoded
    if width * height > MAX_PIXELS:
        raise PhotoError(f"the image is larger than {MAX_PIXELS // 1_000_000} megapixels")
    try:
        image.load()
        return _flatten(ImageOps.exif_transpose(image))
    except Exception as exc:
        raise PhotoError("the image could not be read") from exc


def process(raw: bytes) -> dict:
    """Returns {"width", "height", "data", "thumb"}, or raises PhotoError."""
    image = _decode(raw)
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    thumb = image.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    return {
        "width": image.width,
        "height": image.height,
        "data": _jpeg(image, JPEG_QUALITY),
        "thumb": _jpeg(thumb, THUMB_QUALITY),
    }
