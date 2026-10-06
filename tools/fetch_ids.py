"""Refresh the identifier registries warmap.ble layers on top of its
built-in tables: Bluetooth SIG company IDs, Bluetooth SIG 16-bit service
and member UUIDs, and the IEEE MA-L (OUI) prefix registry.

Sources, tried in order, first one that answers wins:

* Bluetooth SIG company IDs and UUIDs: the assigned-numbers YAML in the
  bluetooth-SIG/public Bitbucket repo. Fallback: the JSON mirror in
  NordicSemiconductor/bluetooth-numbers-database on GitHub. The Nordic
  mirror's UUID file only carries a handful of member allocations, so it's
  a materially smaller fallback, and the summary line says so when it's used.
* IEEE OUI registry: standards-oui.ieee.org's own CSV export. No fallback;
  if that's down there's nowhere else authoritative to pull it from.

Everything is downloaded and parsed into memory first. Files on disk are
only touched once all three sources have parsed successfully, and each
write is a temp-file-then-rename so a crash mid-write can never leave a
truncated file where a good one used to be. On any failure, the existing
files are left exactly as they were and the script exits non-zero with the
reason.

Usage:
    python3 tools/fetch_ids.py [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "warmap" / "data"

USER_AGENT = "warmap-fetch-ids/1.0 (+https://github.com/munzzyy/warmap)"
TIMEOUT_SECONDS = 30

COMPANY_ID_URLS = [
    "https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/company_identifiers/company_identifiers.yaml",
    "https://raw.githubusercontent.com/NordicSemiconductor/bluetooth-numbers-database/master/v1/company_ids.json",
]
SERVICE_UUID_URL = "https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/uuids/service_uuids.yaml"
MEMBER_UUID_URL = "https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/uuids/member_uuids.yaml"
UUID_FALLBACK_URL = "https://raw.githubusercontent.com/NordicSemiconductor/bluetooth-numbers-database/master/v1/service_uuids.json"
OUI_CSV_URL = "https://standards-oui.ieee.org/oui/oui.csv"

# Below this many parsed entries, treat the source as broken or changed-shape
# rather than trust it: a schema drift that silently parses to a handful
# of rows is worse than a loud failure.
MIN_COMPANY_ENTRIES = 1000
MIN_UUID_ENTRIES = 50
MIN_OUI_ENTRIES = 10000

OUI_NAME_MAX_LEN = 60
OUI_PLACEHOLDER_NAMES = {"IEEE Registration Authority"}


class FetchError(RuntimeError):
    """A source could not be retrieved or did not parse as expected."""


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def fetch_first(urls: list[str], label: str) -> tuple[str, str]:
    """Try each URL in order; return (text, url) for the first that answers.
    Raises FetchError with every attempt's failure if none do."""
    errors = []
    for url in urls:
        try:
            raw = _http_get(url)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
            continue
        return raw.decode("utf-8"), url
    raise FetchError(f"{label}: no source reachable ({'; '.join(errors)})")


# --- minimal YAML scalar parsing -----------------------------------------
# The Bluetooth SIG assigned-numbers YAML files are a flat list of small
# mappings (`- value: 0xNNNN` / `name: 'foo'`), never nested beyond that.
# A real YAML parser is overkill for one fixed shape and pyyaml isn't a
# dependency of this project, so this is a purpose-built line scanner, not
# a general parser. It only needs to handle what these two files actually
# use: bare scalars, single-quoted (with '' as an escaped quote) and
# double-quoted (with \" and \\ as the only escapes seen in practice).

_KEY_LINE_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _key_line_re(key: str) -> "re.Pattern[str]":
    pat = _KEY_LINE_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf"^\s*-\s+{re.escape(key)}:\s*(\S+)\s*$")
        _KEY_LINE_RE_CACHE[key] = pat
    return pat


_NAME_LINE_RE = re.compile(r"^\s*name:\s*(.+?)\s*$")


def _unescape_yaml_scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        inner = raw[1:-1]
        out = []
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner) and inner[i + 1] in ('"', "\\"):
                out.append(inner[i + 1])
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    return raw


def parse_id_yaml(text: str, id_key: str) -> dict[int, str]:
    """Parse a flat `- <id_key>: 0xNNNN` / `name: foo` list into {int: name}.
    Entries are found by scanning for an id-key line, then the nearest
    following non-blank `name:` line. That survives both the blank-line
    separated style (company_identifiers.yaml) and the packed style
    (service/member_uuids.yaml) without caring which one a given file uses.
    """
    lines = text.splitlines()
    key_re = _key_line_re(id_key)
    out: dict[int, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        m = key_re.match(lines[i])
        if not m:
            i += 1
            continue
        try:
            value = int(m.group(1), 0)
        except ValueError:
            i += 1
            continue
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        name = None
        if j < n:
            nm = _NAME_LINE_RE.match(lines[j])
            if nm:
                name = _unescape_yaml_scalar(nm.group(1))
        if name:
            out[value] = name
            i = j + 1
        else:
            i += 1
    return out


# --- per-source fetch + parse ---------------------------------------------

def fetch_companies() -> tuple[dict[int, str], str]:
    text, url = fetch_first(COMPANY_ID_URLS, "Bluetooth company identifiers")
    if url.endswith(".yaml"):
        table = parse_id_yaml(text, "value")
    else:
        data = json.loads(text)
        table = {int(e["code"]): str(e["name"]) for e in data if "code" in e and "name" in e}
    if len(table) < MIN_COMPANY_ENTRIES:
        raise FetchError(
            f"Bluetooth company identifiers: only parsed {len(table)} entries from {url}, "
            f"expected at least {MIN_COMPANY_ENTRIES}, source shape probably changed"
        )
    return table, url


def fetch_uuids() -> tuple[dict[int, str], str]:
    try:
        service_text, service_url = fetch_first([SERVICE_UUID_URL], "GATT service UUIDs")
        member_text, member_url = fetch_first([MEMBER_UUID_URL], "member/vendor UUIDs")
    except FetchError:
        # Both files live in the same repo; if either one is unreachable
        # fall back to the smaller Nordic mirror rather than half-succeed.
        text, url = fetch_first([UUID_FALLBACK_URL], "16-bit UUIDs (fallback)")
        data = json.loads(text)
        table = {}
        for e in data:
            try:
                table[int(str(e["uuid"]), 16)] = str(e["name"])
            except (KeyError, ValueError, TypeError):
                continue
        if len(table) < MIN_UUID_ENTRIES:
            raise FetchError(
                f"16-bit UUIDs: only parsed {len(table)} entries from fallback {url}, "
                f"expected at least {MIN_UUID_ENTRIES}"
            )
        return table, f"{url} (fallback, GATT services only, no full member allocation list)"

    table = parse_id_yaml(service_text, "uuid")
    table.update(parse_id_yaml(member_text, "uuid"))
    if len(table) < MIN_UUID_ENTRIES:
        raise FetchError(
            f"16-bit UUIDs: only parsed {len(table)} entries from {service_url} + {member_url}, "
            f"expected at least {MIN_UUID_ENTRIES}, source shape probably changed"
        )
    return table, f"{service_url} + {member_url}"


def fetch_oui() -> tuple[dict[str, str], str]:
    text, url = fetch_first([OUI_CSV_URL], "IEEE OUI registry")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    if "Assignment" not in fieldnames or "Organization Name" not in fieldnames:
        raise FetchError(
            f"IEEE OUI registry: unexpected columns {fieldnames} from {url}: schema changed"
        )
    table: dict[str, str] = {}
    for row in reader:
        assignment = (row.get("Assignment") or "").strip().upper()
        name = (row.get("Organization Name") or "").strip()
        if len(assignment) != 6 or any(c not in "0123456789ABCDEF" for c in assignment):
            continue
        if not name or name in OUI_PLACEHOLDER_NAMES:
            continue
        if len(name) > OUI_NAME_MAX_LEN:
            name = name[:OUI_NAME_MAX_LEN].rstrip()
        prefix = ":".join(assignment[i:i + 2] for i in range(0, 6, 2))
        table[prefix] = name
    if len(table) < MIN_OUI_ENTRIES:
        raise FetchError(
            f"IEEE OUI registry: only parsed {len(table)} entries from {url}, "
            f"expected at least {MIN_OUI_ENTRIES}, source shape probably changed"
        )
    return table, url


# --- writing ---------------------------------------------------------------

def _write_json_atomic(path: Path, data: dict, key_fmt) -> int:
    """Write `data` (int or str keys) as sorted JSON, keys rendered by
    `key_fmt`. Writes to a temp file in the same directory and renames over
    the target, so a half-written file is never visible at `path`."""
    encoded = {key_fmt(k): v for k, v in data.items()}
    body = json.dumps(encoded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    os.replace(tmp_path, path)
    return len(body.encode("utf-8"))


def _hex16(n: int) -> str:
    return f"0x{n:04X}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"directory to write the three JSON files into (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args(argv)

    try:
        companies, company_src = fetch_companies()
        uuids, uuid_src = fetch_uuids()
        ouis, oui_src = fetch_oui()
    except FetchError as exc:
        print(f"fetch-ids: {exc}", file=sys.stderr)
        print("fetch-ids: leaving existing data files untouched.", file=sys.stderr)
        return 1

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    written = [
        ("bluetooth-companies.json", companies, _hex16, company_src),
        ("bluetooth-uuids.json", uuids, _hex16, uuid_src),
        ("oui.json", ouis, str, oui_src),
    ]
    for filename, table, key_fmt, source in written:
        size = _write_json_atomic(output_dir / filename, table, key_fmt)
        print(f"{filename}: {len(table)} entries, {size} bytes (source: {source})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
