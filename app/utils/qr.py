from io import BytesIO

import qrcode


def qr_png(value: str) -> bytes:
    """Return a compact PNG QR image for a Telegram deep link."""
    image = qrcode.make(value)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
