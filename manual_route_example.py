#!/usr/bin/env python3
"""Manual Route API v2 example: turn the CSVs in ``files/`` into payloads and POST them.

Flow (mirrors the sections of "Manual Route API v2 - Public Documentation.md"):

    1. POST /v2/access_token          -> bearer token
    2. GET  /v2/plants                -> pick a plant
    3. GET  /v2/assets?plant_id=...    -> resolve the real numeric asset_id
    4. POST /v2/manual-route/data     -> one call per CSV file

Credentials come from a ``.env`` file next to this script (copy ``.env-example``
and fill it in). Real environment variables win over ``.env``, so CI can inject
secrets without a file. Usage::

    cp .env-example .env && $EDITOR .env

    # See what would be sent, no network calls at all:
    python manual_route_example.py --dry-run

    # Send everything, letting the script pick the plant/asset by name:
    python manual_route_example.py --plant-name "4950 Yonge St" --asset-name "Line1-Machine1-Asset01"

    # Or skip discovery entirely if you already know the asset id:
    python manual_route_example.py --asset-id 112233
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"

# Load .env before anything reads os.environ. override=False means a real
# environment variable always beats the file, so CI can inject secrets directly.
load_dotenv(ENV_FILE, override=False)

API_BASE = os.environ.get("IM_API_BASE") or "https://api.pdmmatrix.assetmatrix.com"
FILES_DIR = HERE / "files"
TIMEOUT = 60

# Line1-Machine1-Asset01_1H_Velocity.csv -> asset label, location, measurement type
FILENAME_RE = re.compile(
    r"^(?P<asset_id>.+)_(?P<location>\d[A-Z])_(?P<type>Velocity|Enveloped_Acc_Band_\d+)\.csv$"
)

# Metadata rows that map straight onto top-level payload fields.
DIRECT_FIELDS = {"Date/Time", "X-axis Units", "Y-axis Units", "Overall", "RPM"}

# Metadata rows that go into payload["meta_data"], with the type to coerce them to.
META_FIELDS = {
    "Unit ID": int,
    "OVD": str,
    "Sensitivity": float,
    "Max Freq / Orders": int,
    "Detection": str,
    "No. of Lines": int,
    "Window Type": str,
}

# The row that separates the metadata section from the spectrum. It is a marker,
# not a data point - never try to float() it.
DATA_MARKER = "X-Axis"


# --------------------------------------------------------------------------- #
# CSV -> payload
# --------------------------------------------------------------------------- #

def normalize_y_axis_unit(raw: str) -> str:
    """``"Velocity (mm/s)"`` -> ``"mm/s"``, ``"gE"`` -> ``"g"``.

    The raw CSV string is never a valid enum value on its own.
    """
    raw = raw.strip()
    if match := re.search(r"\(([^)]+)\)", raw):  # "Velocity (mm/s)" -> "mm/s"
        return match.group(1).strip()
    if raw in ("gE", "g"):  # enveloped acceleration is still reported in g
        return "g"
    return raw


def parse_timestamp(raw: str) -> int:
    """``"01 Jul 2026 14.51.40"`` -> unix seconds (UTC)."""
    naive = datetime.strptime(raw.strip(), "%d %b %Y %H.%M.%S")
    return int(naive.replace(tzinfo=timezone.utc).timestamp())


@dataclass
class Measurement:
    """One CSV file, parsed. ``asset_label`` is cosmetic; ``payload`` needs a real id."""

    path: Path
    asset_label: str
    location: str
    payload: dict = field(repr=False)


def parse_manual_route_csv(path: Path) -> Measurement:
    match = FILENAME_RE.match(path.name)
    if not match:
        raise ValueError(f"{path.name!r} does not match <asset>_<location>_<type>.csv")

    measurement_type = "velocity" if match.group("type") == "Velocity" else "envelope"

    fields: dict[str, str] = {}
    data: list[list[float]] = []
    in_data_section = False

    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            # Rows are ragged and padded with empty cells; blank rows are skipped.
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue
            key, value = (cells + ["", ""])[:2]

            if in_data_section:
                data.append([float(key), float(value)])
                continue

            if key == DATA_MARKER:  # marker row itself, not a measurement
                in_data_section = True
                continue

            if key in DIRECT_FIELDS or key in META_FIELDS:
                fields[key] = value

    missing = (DIRECT_FIELDS | set(META_FIELDS)) - fields.keys()
    if missing:
        raise ValueError(f"{path.name}: missing metadata rows {sorted(missing)}")
    if not data:
        raise ValueError(f"{path.name}: no data rows found after the {DATA_MARKER!r} marker")

    payload = {
        # Placeholder - replaced with the real hierarchy asset id before POSTing.
        "asset_id": None,
        # Straight from the filename: must match the Manual Route sensor's
        # Location exactly (created in the UI, doc section 1). Never overwrite.
        "location": match.group("location"),
        "timestamp": parse_timestamp(fields["Date/Time"]),
        "type": measurement_type,
        "x_axis_unit": fields["X-axis Units"],
        "y_axis_unit": normalize_y_axis_unit(fields["Y-axis Units"]),
        "overall": float(fields["Overall"]),
        "rpm": int(fields["RPM"]),
        "data": data,
        "meta_data": {name: cast(fields[name]) for name, cast in META_FIELDS.items()},
    }

    return Measurement(
        path=path,
        asset_label=match.group("asset_id"),
        location=match.group("location"),
        payload=payload,
    )


def load_measurements(files_dir: Path = FILES_DIR) -> list[Measurement]:
    """Parse every ``<asset>_<location>_<type>.csv`` under ``files_dir``.

    Dropping in a new file extends coverage automatically - nothing else changes.
    """
    paths = sorted(p for p in files_dir.glob("*.csv") if FILENAME_RE.match(p.name))
    if not paths:
        raise SystemExit(f"No manual-route CSVs found in {files_dir}")
    return [parse_manual_route_csv(p) for p in paths]


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #

class ManualRouteClient:
    def __init__(self, api_base: str = API_BASE):
        self.api_base = api_base.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def login(self, api_key: str, api_secret: str) -> str:
        """POST /v2/access_token - doc section 4."""
        response = self.session.post(
            self._url("/v2/access_token"),
            json={"api_key": api_key, "api_secret": api_secret},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        # Every endpoint other than the login itself expects this header.
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def list_plants(self) -> list[dict]:
        """GET /v2/plants - doc section 5."""
        response = self.session.get(self._url("/v2/plants"), timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()

    def list_assets(self, plant_id: str | int) -> list[dict]:
        """GET /v2/assets?plant_id=... - doc section 6. Source of the real asset_id."""
        response = self.session.get(
            self._url("/v2/assets"), params={"plant_id": plant_id}, timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()

    def post_manual_route(self, payload: dict) -> dict:
        """POST /v2/manual-route/data - doc section 7. One call per measurement."""
        response = self.session.post(
            self._url("/v2/manual-route/data"), json=payload, timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()


# --------------------------------------------------------------------------- #
# Discovery helpers
# --------------------------------------------------------------------------- #

def pick_plant(plants: list[dict], name: str | None) -> dict:
    if not plants:
        raise SystemExit("No plants visible to these credentials.")
    if name is None:
        return plants[0]
    for plant in plants:
        if plant.get("plant_name") == name:
            return plant
    available = ", ".join(repr(p.get("plant_name")) for p in plants)
    raise SystemExit(f"No plant named {name!r}. Available: {available}")


def pick_asset(assets: list[dict], name: str | None) -> dict:
    if not assets:
        raise SystemExit("No assets in this plant.")
    if name is None:
        return assets[0]
    # The CSV filename label ("Line1-Machine1-Asset01") is not guaranteed to equal the
    # hierarchy asset name, so match loosely and report what was available.
    for asset in assets:
        if asset.get("asset_name") == name:
            return asset
    for asset in assets:
        if name.lower() in str(asset.get("asset_name", "")).lower():
            return asset
    available = ", ".join(repr(a.get("asset_name")) for a in assets)
    raise SystemExit(f"No asset matching {name!r}. Available: {available}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def summarize(measurement: Measurement) -> str:
    p = measurement.payload
    return (
        f"{measurement.path.name:<48} "
        f"location={p['location']:<3} type={p['type']:<9} "
        f"overall={p['overall']:<12.6g} rpm={p['rpm']:<6} "
        f"points={len(p['data']):<5} unit={p['y_axis_unit']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files-dir", type=Path, default=FILES_DIR)
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument("--plant-name", help="Plant to use; defaults to the first one.")
    parser.add_argument("--asset-name", help="Asset to use; defaults to the first one.")
    parser.add_argument(
        "--asset-id", help="Use this asset_id directly and skip plant/asset discovery."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print payloads without any network call.",
    )
    parser.add_argument(
        "--dump", type=Path, help="Also write the payloads as JSON to this file."
    )
    args = parser.parse_args(argv)

    measurements = load_measurements(args.files_dir)
    print(f"Parsed {len(measurements)} measurement(s) from {args.files_dir}:")
    for measurement in measurements:
        print("  " + summarize(measurement))

    if args.dry_run:
        # Show one full payload so the shape is visible, truncating the spectrum.
        sample = dict(measurements[0].payload)
        sample["asset_id"] = args.asset_id or "<real asset_id from /v2/assets>"
        sample["data"] = sample["data"][:3] + [["...", f"{len(measurements[0].payload['data'])} rows total"]]
        print("\nSample payload:")
        print(json.dumps(sample, indent=2))
        if args.dump:
            args.dump.write_text(
                json.dumps([m.payload for m in measurements], indent=2), encoding="utf-8"
            )
            print(f"\nWrote {len(measurements)} payload(s) to {args.dump}")
        return 0

    # .strip() so the blank placeholders in .env-example read as "not set"
    # rather than as an empty credential the API would reject.
    api_key = os.environ.get("IM_API_KEY", "").strip()
    api_secret = os.environ.get("IM_API_SECRET", "").strip()
    if not (api_key and api_secret):
        hint = (
            f"{ENV_FILE.name} exists but IM_API_KEY / IM_API_SECRET are blank"
            if ENV_FILE.exists()
            else f"no {ENV_FILE.name} found - copy .env-example to .env and fill it in"
        )
        parser.error(
            f"Missing credentials ({hint}). Get them from "
            "app.industrialmatrix.com/profile, or run with --dry-run."
        )

    client = ManualRouteClient(args.api_base)
    client.login(api_key, api_secret)
    print("\nAuthenticated.")

    if args.asset_id:
        asset_id = args.asset_id
    else:
        plant = pick_plant(client.list_plants(), args.plant_name)
        print(f"Plant: {plant.get('plant_name')} (id={plant.get('plant_id')})")
        asset = pick_asset(client.list_assets(plant["plant_id"]), args.asset_name)
        asset_id = asset["asset_id"]
        print(f"Asset: {asset.get('asset_name')} (id={asset_id})")

    if args.dump:
        args.dump.write_text(
            json.dumps(
                [{**m.payload, "asset_id": asset_id} for m in measurements], indent=2
            ),
            encoding="utf-8",
        )
        print(f"Wrote payloads to {args.dump}")

    failures = 0
    print()
    for measurement in measurements:
        payload = measurement.payload
        # The filename's asset label is cosmetic; the API wants the real id.
        # payload["location"] stays exactly as parsed from the filename.
        payload["asset_id"] = asset_id
        try:
            client.post_manual_route(payload)
        except requests.HTTPError as exc:
            failures += 1
            body = exc.response.text[:300] if exc.response is not None else ""
            print(f"FAIL {measurement.path.name}: {exc} {body}")
        else:
            print(f"OK   {measurement.path.name} -> {payload['location']}/{payload['type']}")

    print(f"\n{len(measurements) - failures}/{len(measurements)} submitted.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
