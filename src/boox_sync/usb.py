"""Detect the configured BOOX tablet on USB without calling adb."""

import subprocess

VENDOR_ID = 6353


def usb_present(serial: str) -> bool:
    """Return whether IORegistry currently lists this USB serial.

    Args:
        serial: Tablet serial, for example TEST1234.

    Returns:
        True when ioreg shows that serial on the USB plane.
    """
    proc = subprocess.run(
        ["ioreg", "-p", "IOUSB", "-w", "0", "-l"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    text = proc.stdout
    return (
        f'"kUSBSerialNumberString" = "{serial}"' in text
        or f'"USB Serial Number" = "{serial}"' in text
    )
