from __future__ import annotations

import os

from pathlib import Path

from warmap.sdcard import (
    ScanResult,
    default_media_roots,
    scan_removable_media,
)


def test_default_media_roots_uses_given_user():
    roots = default_media_roots(user="cole", platform="linux")
    assert Path("/run/media/cole") in roots
    assert Path("/media/cole") in roots


def test_default_media_roots_always_includes_system_wide_mounts():
    """/media and /mnt (with no per-user path component) are where a card
    lands when it's mounted by hand rather than by an automount daemon.
    That doesn't depend on $USER being resolvable, so they're always in the
    list, not just appended onto the per-user roots."""
    roots = default_media_roots(user="cole", platform="linux")
    assert Path("/media") in roots
    assert Path("/mnt") in roots


def test_default_media_roots_keeps_system_wide_mounts_without_a_user(monkeypatch):
    """A missing $USER/$LOGNAME means the per-user roots can't be built, but
    /media and /mnt are fixed paths regardless of who's logged in. Losing
    them here would silently stop finding a card mounted by hand on a system
    with no desktop automount daemon running."""
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    roots = default_media_roots(user=None, platform="linux")
    assert roots == [Path("/media"), Path("/mnt")]


def test_windows_roots_are_the_removable_drive_letters():
    """Windows has no mount tree to walk. The scan asks the OS which drive
    letters are removable and uses exactly those."""
    from warmap.sdcard import _windows_removable_drives

    types = {"E:\\": 2, "F:\\": 3, "G:\\": 2}
    roots = _windows_removable_drives(drive_type=lambda root: types.get(root, 1))
    assert roots == [Path("E:\\"), Path("G:\\")]


def test_macos_roots_skip_the_boot_volume(tmp_path):
    """/Volumes lists the boot disk alongside real removable media, and
    walking it would mean walking the whole machine."""
    from warmap.sdcard import _macos_volumes

    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    (volumes / "SDCARD").mkdir()
    (volumes / "Macintosh HD").symlink_to("/")
    assert _macos_volumes(volumes) == [volumes / "SDCARD"]


def test_describe_roots_names_each_platform():
    from warmap.sdcard import describe_roots

    assert "/run/media" in describe_roots("linux")
    assert "/Volumes" in describe_roots("darwin")
    assert "removable" in describe_roots("win32")


def test_scan_finds_csv_files_recursively(tmp_path):
    card = tmp_path / "SDCARD"
    (card / "captures").mkdir(parents=True)
    (card / "captures" / "run1.csv").write_text("a,b\n1,2\n")
    (card / "captures" / "run2.CSV").write_text("a,b\n1,2\n")  # uppercase extension
    (card / "notes.txt").write_text("not a capture")

    found = scan_removable_media(roots=[card])
    names = {p.name for p in found}
    assert names == {"run1.csv", "run2.CSV"}


def test_scan_missing_root_returns_empty_not_raise(tmp_path):
    assert scan_removable_media(roots=[tmp_path / "not-mounted"]) == []


def test_scan_multiple_roots(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "one.csv").write_text("x")
    (root_b / "two.csv").write_text("x")
    found = scan_removable_media(roots=[root_a, root_b])
    assert {p.name for p in found} == {"one.csv", "two.csv"}


def test_scan_ignores_non_csv_files(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "readme.txt").write_text("x")
    (root / "photo.jpg").write_bytes(b"\x00")
    assert scan_removable_media(roots=[root]) == []


# --- realistic card layouts -----------------------------------------------

def test_two_cards_mounted_at_once_are_both_found(tmp_path):
    """The normal shape multiple cards actually take: sibling directories
    under one automount root, one per device, not two separate roots."""
    root = tmp_path / "run_media_cole"
    (root / "FLIPPER_SD" / "subghz").mkdir(parents=True)
    (root / "FLIPPER_SD" / "subghz" / "gate.sub").write_text(
        "Filetype: Flipper SubGhz Key File\n"
    )
    # A standalone Marauder's wardrive at the root of its own card, a real
    # wardrive shape, not just an arbitrary .log, so this also proves the
    # ambiguous-extension content sniff fires for a second, separate card.
    (root / "MARAUDER").mkdir(parents=True)
    (root / "MARAUDER" / "wardrive_0.log").write_text(
        "WigleWifi-1.4,appRelease=v1.14.0\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
    )

    found = {p.name for p in scan_removable_media(roots=[root])}
    assert found == {"gate.sub", "wardrive_0.log"}


def test_card_label_with_spaces_is_scanned_normally(tmp_path):
    root = tmp_path / "run_media_cole"
    card = root / "MARAUDER SD 2"
    (card / "apps_data" / "marauder" / "dumps").mkdir(parents=True)
    (card / "apps_data" / "marauder" / "dumps" / "wardrive_0.txt").write_text(
        "WigleWifi-1.4,appRelease=v1.14.0\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
    )
    found = scan_removable_media(roots=[root])
    assert [p.name for p in found] == ["wardrive_0.txt"]


def test_arbitrarily_deep_user_nesting_under_a_known_folder_is_walked(tmp_path):
    """Flipper users nest their own subfolders inside subghz/nfc/etc. The
    walk must not stop at some fixed depth under the folders it knows about."""
    card = tmp_path / "card"
    deep = card / "subghz" / "my_stuff" / "2024" / "batch1" / "sub_batch" / "even_deeper"
    deep.mkdir(parents=True)
    (deep / "deep.sub").write_text("Filetype: Flipper SubGhz Key File\n")

    found = scan_removable_media(roots=[card])
    assert [p.name for p in found] == ["deep.sub"]


def test_readonly_mounted_card_is_still_scanned(tmp_path):
    """Scanning only ever reads. A card mounted read-only (or one you have
    deliberately chmod'd down while reviewing it) must not come up empty."""
    card = tmp_path / "readonly_card"
    (card / "subghz").mkdir(parents=True)
    (card / "subghz" / "x.sub").write_text("Filetype: Flipper SubGhz Key File\n")
    os.chmod(card / "subghz", 0o555)
    os.chmod(card, 0o555)
    try:
        found = scan_removable_media(roots=[card])
        assert [p.name for p in found] == ["x.sub"]
    finally:
        os.chmod(card / "subghz", 0o755)
        os.chmod(card, 0o755)


def test_symlinked_mount_root_is_scanned(tmp_path):
    """Some setups mount to a real path and symlink a friendlier name to it
    (or a udev rule does); the root handed to us being a symlink must not
    make the walk come up empty."""
    real_card = tmp_path / "real_target"
    (real_card / "nfc").mkdir(parents=True)
    (real_card / "nfc" / "badge.nfc").write_text("Filetype: Flipper NFC device\n")

    mount_point = tmp_path / "run_media_cole" / "SDCARD"
    mount_point.parent.mkdir(parents=True)
    os.symlink(real_card, mount_point, target_is_directory=True)

    found = scan_removable_media(roots=[mount_point])
    assert [p.name for p in found] == ["badge.nfc"]


def test_a_folder_that_is_not_a_card_yields_nothing(tmp_path):
    folder = tmp_path / "not_a_card"
    (folder / "Documents").mkdir(parents=True)
    (folder / "Documents" / "resume.pdf").write_bytes(b"%PDF-1.4 junk")
    (folder / "Pictures").mkdir(parents=True)
    (folder / "Pictures" / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    assert scan_removable_media(roots=[folder]) == []


# --- bounded walk / truncation signal -------------------------------------

def test_scan_result_is_not_truncated_for_an_ordinary_card(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    (card / "a.csv").write_text("x")
    found = scan_removable_media(roots=[card])
    assert isinstance(found, ScanResult)
    assert found.truncated is False


def test_huge_irrelevant_tree_does_not_stop_a_different_root_being_scanned(tmp_path, monkeypatch):
    """A card that's mostly a large unrelated file collection (e.g. photos)
    must not silently swallow scanning time from a *different* mounted root
    that holds a real, small capture. The per-root visited cap has to reset
    for each root, not accumulate across all of them."""
    import warmap.sdcard as sdcard_module

    monkeypatch.setattr(sdcard_module, "_MAX_SCANNED_PER_ROOT", 50)

    huge_root = tmp_path / "huge"
    huge_root.mkdir()
    for i in range(200):
        (huge_root / f"IMG_{i:04d}.JPG").write_bytes(b"")

    small_root = tmp_path / "small"
    small_root.mkdir()
    (small_root / "wardrive_0.csv").write_text("a,b\n1,2\n")

    found = scan_removable_media(roots=[huge_root, small_root])
    assert [p.name for p in found] == ["wardrive_0.csv"]
    assert found.truncated is True


def test_scan_result_behaves_like_a_plain_list(tmp_path):
    """Every existing caller (mainwindow, the CLI) treats the return value as
    a plain list: slicing, len(), truthiness, iteration all have to keep
    working exactly as before."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "a.csv").write_text("x")
    (card / "b.csv").write_text("x")
    found = scan_removable_media(roots=[card])
    assert len(found) == 2
    assert bool(found) is True
    assert isinstance(found[:1], list)
    assert list(found) == sorted(found)


# --- content sniffing robustness -------------------------------------------

def test_bom_prefixed_wardrive_txt_is_still_recognized(tmp_path):
    """A wardrive touched by a Windows-side tool can pick up a UTF-8 BOM,
    which would otherwise sit in front of "WigleWifi" and make the
    startswith check miss it, which is what utf-8-sig decoding in
    _is_capture_content is for."""
    card = tmp_path / "card"
    card.mkdir()
    content = (
        "WigleWifi-1.4,appRelease=v1.14.0\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,"
        "CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
    )
    (card / "wardrive_0.txt").write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    found = scan_removable_media(roots=[card])
    assert [p.name for p in found] == ["wardrive_0.txt"]
