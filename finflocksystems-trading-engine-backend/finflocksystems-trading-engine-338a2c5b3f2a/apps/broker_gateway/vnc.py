VNC_PASSWORD_BYTES = 8


class VNCPasswordConfigurationError(ValueError):
    """Report invalid VNC configuration without retaining the supplied value."""


def normalize_vnc_password(value):
    """Return the eight ASCII bytes used by classic RFB VNC authentication.

    Classic VNC authentication uses only the first eight password bytes.  We
    require an unambiguous ASCII representation and pass exactly those eight
    bytes to both x11vnc and the trusted RFB proxy.
    """
    password = "" if value is None else str(value)
    if not password:
        raise VNCPasswordConfigurationError("VNC password configuration is missing")
    if "\r" in password or "\n" in password:
        raise VNCPasswordConfigurationError("VNC password configuration contains a line break")
    try:
        encoded = password.encode("ascii")
    except UnicodeEncodeError:
        raise VNCPasswordConfigurationError(
            "VNC password configuration must use unambiguous ASCII bytes"
        ) from None
    if len(encoded) < VNC_PASSWORD_BYTES:
        raise VNCPasswordConfigurationError(
            "VNC password configuration must contain at least eight effective bytes"
        )
    return encoded[:VNC_PASSWORD_BYTES].decode("ascii")
