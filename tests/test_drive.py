from secret.core import drive
from secret.core.drive import (
    BUS_SD,
    BUS_USB,
    DRIVE_FIXED,
    DRIVE_REMOTE,
    DRIVE_REMOVABLE,
    classify,
)

BUS_SATA, BUS_NVME = 11, 17


def test_usb_stick_and_external_disks():
    assert classify(DRIVE_REMOVABLE, BUS_USB, None) is drive.USB
    assert classify(DRIVE_FIXED, BUS_USB, True) is drive.EXTERNAL_HDD
    assert classify(DRIVE_FIXED, BUS_USB, False) is drive.EXTERNAL_SSD


def test_usb_case_that_hides_rotation_uses_trim():
    assert classify(DRIVE_FIXED, BUS_USB, None, trim=False) is drive.EXTERNAL_HDD
    assert classify(DRIVE_FIXED, BUS_USB, None, trim=True) is drive.EXTERNAL_SSD
    assert classify(DRIVE_FIXED, BUS_USB, None, trim=None) is drive.EXTERNAL


def test_internal_sd_network_and_unknown():
    assert classify(DRIVE_FIXED, BUS_SATA, False, trim=True) is drive.INTERNAL_SSD
    assert classify(DRIVE_FIXED, BUS_SATA, True) is drive.INTERNAL_HDD
    assert classify(DRIVE_FIXED, BUS_NVME, None, trim=True) is drive.INTERNAL_SSD
    assert classify(DRIVE_REMOVABLE, BUS_SD, None) is drive.SD_CARD
    assert classify(DRIVE_REMOTE, None, None) is drive.NETWORK
    assert classify(None, None, None) is drive.USB


def test_every_name_ends_in_a_vowel_for_the_particles():
    kinds = [v for v in vars(drive).values() if isinstance(v, drive.DriveKind)]
    last_letters = {k.name[-1] for k in kinds}
    for ch in last_letters:
        if "가" <= ch <= "힣":
            assert (ord(ch) - 0xAC00) % 28 == 0, ch
        else:
            assert ch in "BD", ch
