"""Bluetooth identification: address parsing, vendor/company lookup, address
type classification, and tracker matching.
"""

from __future__ import annotations

import json

from warmap import ble


# --- address normalization -----------------------------------------------

def test_normalize_accepts_the_common_spellings():
    expected = "AA:BB:CC:DD:EE:FF"
    assert ble.normalize_mac("aa:bb:cc:dd:ee:ff") == expected
    assert ble.normalize_mac("AA-BB-CC-DD-EE-FF") == expected
    assert ble.normalize_mac("aabbccddeeff") == expected
    assert ble.normalize_mac("AA BB CC DD EE FF") == expected


def test_normalize_rejects_wrong_length():
    assert ble.normalize_mac("AA:BB:CC") is None
    assert ble.normalize_mac("AA:BB:CC:DD:EE:FF:00") is None
    assert ble.normalize_mac("") is None
    assert ble.normalize_mac(None) is None


def test_normalize_rejects_non_hex():
    assert ble.normalize_mac("ZZ:BB:CC:DD:EE:FF") is None


# --- lookups -------------------------------------------------------------

def test_known_company_ids():
    assert "Apple" in ble.company_name(0x004C)
    assert "Nordic" in ble.company_name(0x0059)


def test_unknown_company_renders_the_raw_value_not_a_guess():
    assert ble.company_name(0xFFFE) == "Unknown company 0xFFFE"


def test_company_name_tolerates_junk():
    assert ble.company_name(None) is None
    assert ble.company_name("not a number") is None


def test_known_service_uuids():
    assert "Tile" in ble.service_name(0xFEED)
    assert "Battery" in ble.service_name(0x180F)


def test_unknown_service_uuid_renders_hex():
    assert ble.service_name(0x1234) == "0x1234"


def test_oui_lookup():
    assert "Raspberry Pi" in ble.oui_vendor("B8:27:EB:11:22:33")
    assert ble.oui_vendor("B8-27-EB-11-22-33") is not None


def test_oui_lookup_unknown_returns_none():
    assert ble.oui_vendor("FE:DC:BA:98:76:54") is None


def test_oui_lookup_never_raises_on_junk():
    assert ble.oui_vendor("nonsense") is None
    assert ble.oui_vendor(None) is None


# --- address type --------------------------------------------------------

def test_registered_oui_reads_as_public():
    assert ble.address_type("B8:27:EB:11:22:33") == "public"


def test_resolvable_private_address():
    """Top two bits 0b01 -> first octet in 0x40-0x7F."""
    assert ble.address_type("4C:11:22:33:44:55") == "random-resolvable"
    assert ble.address_type("7F:11:22:33:44:55") == "random-resolvable"


def test_static_random_address():
    """Top two bits 0b11 -> first octet in 0xC0-0xFF."""
    assert ble.address_type("C0:11:22:33:44:55") == "random-static"
    assert ble.address_type("FF:11:22:33:44:55") == "random-static"


def test_non_resolvable_private_address():
    # Top two bits 0b00 and a prefix that can't be an IEEE assignment (the
    # locally-administered bit is set), so the OUI check doesn't claim it.
    assert ble.address_type("3E:11:22:33:44:55") == "random-non-resolvable"


def test_registered_oui_wins_over_the_address_bit_heuristic():
    """00:11:22 has top bits 0b00, which in isolation reads as a
    non-resolvable private address, but it's a real registered OUI, so it's
    a public address and the vendor lookup has to take precedence."""
    assert ble.oui_vendor("00:11:22:33:44:55") is not None
    assert ble.address_type("00:11:22:33:44:55") == "public"


def test_reserved_bit_pattern_is_unknown_not_a_guess():
    """0b10 is reserved by the spec. Saying "unknown" is correct, inventing
    a category is not."""
    assert ble.address_type("AA:BB:CC:DD:EE:FF") == "unknown"


def test_address_type_on_junk():
    assert ble.address_type("nope") == "unknown"
    assert ble.address_type(None) == "unknown"


def test_locally_administered_bit():
    assert ble.is_locally_administered("02:11:22:33:44:55") is True
    assert ble.is_locally_administered("00:11:22:33:44:55") is False
    assert ble.is_locally_administered("bad") is None


# --- trackers ------------------------------------------------------------

def test_apple_find_my_from_manufacturer_data():
    assert "Find My" in ble.identify_tracker(company_id=0x004C, mfg_data_hex="1219AB")


def test_apple_proximity_pairing_distinct_from_find_my():
    name = ble.identify_tracker(company_id=0x004C, mfg_data_hex="0719AB")
    assert "proximity" in name.lower()


def test_apple_company_without_a_matching_payload_is_not_a_tracker():
    """Being an Apple device does not make something an AirTag."""
    assert ble.identify_tracker(company_id=0x004C, mfg_data_hex="FFAABB") is None


def test_tile_by_service_uuid():
    assert ble.identify_tracker(service_uuids=[0xFEED]) == "Tile"


def test_samsung_smarttag_by_service_uuid():
    assert "SmartTag" in ble.identify_tracker(service_uuids=[0xFD5A])


def test_dult_tracker_by_16bit_service_data_uuid():
    assert "DULT" in ble.identify_tracker(service_uuids=[0xFCB2])


def test_dult_tracker_by_128bit_anos_uuid():
    # A full BLE-advertisement capture exposes the 128-bit Accessory
    # Non-Owner Service UUID, which the 0xFCB2 16-bit path never sees.
    entry = ble.match_tracker(service_uuids_128=[ble.DULT_ANOS_UUID])
    assert entry is not None and "DULT" in entry["name"]
    # Case-insensitive, since a capture may render the UUID either way.
    assert ble.match_tracker(service_uuids_128=[ble.DULT_ANOS_UUID.upper()]) is not None


def test_describe_names_the_128bit_dult_service():
    out = ble.describe("C7:11:22:33:44:55", {"service_uuids_128": [ble.DULT_ANOS_UUID]})
    assert out.get("tracker") and "DULT" in out["tracker"]
    assert "DULT Accessory Non-Owner Service" in out.get("services", [])


def test_name_match_is_labeled_as_weaker_evidence():
    entry = ble.match_tracker(name="My AirTag")
    assert entry["basis"] == "name"


def test_advertisement_match_beats_name_match():
    """A device named "Tile" that advertises a Find My payload is a Find My
    device; the structural evidence has to win."""
    entry = ble.match_tracker(company_id=0x004C, mfg_data_hex="12AA", name="tile")
    assert entry.get("basis", "advertisement") == "advertisement"
    assert "Find My" in entry["name"]


def test_identify_tracker_never_raises():
    assert ble.identify_tracker() is None
    assert ble.identify_tracker(company_id="junk", service_uuids=["x"], name=None) is None


# --- describe ------------------------------------------------------------

def test_describe_flags_randomized_addresses():
    out = ble.describe("4C:11:22:33:44:55")
    assert out["address_type"] == "random-resolvable"
    assert "Randomized" in out["address_note"]


def test_describe_resolves_vendor_and_company():
    out = ble.describe("B8:27:EB:11:22:33", {"company_id": 0x004C})
    assert "Raspberry Pi" in out["vendor"]
    assert "Apple" in out["company"]


def test_describe_names_services():
    out = ble.describe("B8:27:EB:11:22:33", {"service_uuids": [0x180F]})
    assert any("Battery" in s for s in out["services"])


def test_describe_on_empty_input_does_not_raise():
    assert isinstance(ble.describe(None), dict)


# --- external registry loading -------------------------------------------

def test_load_external_layers_new_entries(tmp_path):
    (tmp_path / "bluetooth-companies.json").write_text(
        json.dumps({"0xABCD": "Test Corp"}), encoding="utf-8"
    )
    added = ble.load_external(tmp_path)
    assert added["companies"] >= 1
    assert ble.COMPANY_IDS[0xABCD] == "Test Corp"
    del ble.COMPANY_IDS[0xABCD]


def test_load_external_ignores_a_missing_directory(tmp_path):
    added = ble.load_external(tmp_path / "does-not-exist")
    assert added == {"companies": 0, "uuids": 0, "ouis": 0}


def test_load_external_ignores_malformed_json(tmp_path):
    (tmp_path / "oui.json").write_text("{not json", encoding="utf-8")
    assert ble.load_external(tmp_path)["ouis"] == 0


def test_bundled_registries_are_actually_loaded():
    """A fresh clone must resolve real vendors offline. If the data files go
    missing this catches it rather than silently degrading to "Unknown"."""
    ble.ensure_loaded()
    assert len(ble.COMPANY_IDS) > 1000
    assert len(ble.OUI_PREFIXES) > 10000
