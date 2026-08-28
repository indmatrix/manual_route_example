# Manual Route API v2 — Python example

Reference implementation for the Industrial Matrix **Manual Route API v2**: it reads
vibration-analyzer CSV exports out of [`files/`](files/), turns each one into a
`POST /v2/manual-route/data` payload, and submits them.

This is the sample repository referenced from section 8 of the API documentation
([`Manual Route API v2 - Public Documentation.md`](Manual%20Route%20API%20v2%20-%20Public%20Documentation.md)).

Everything lives in one file — [`manual_route_example.py`](manual_route_example.py) —
so it can be read top to bottom or lifted piecemeal into your own code.

---

## Prerequisite: create the Manual Route location first

**The API rejects a payload whose `(asset_id, location)` pair has no matching
sensor.** Sensors are created in the web UI, not over the API, so this must be done
before your first POST (doc section 1):

1. Find the asset card in the app and click the **Edit Item** (pencil) icon.
2. In the Item panel, open the **Pdm** tab and click **Add Sensor +**.
3. Choose **Manual Route** as the sensor type.
4. Set **Location** to the exact identifier the API will send — e.g. `1H`. It must be
   **unique per asset**. Set **Data Set** to `FFT` (currently the only option).
5. Click **SAVE ITEM**.

Repeat for every location you intend to submit. The CSVs in this repo cover five —
`1H`, `1V`, `4A`, `4H`, `4V` — so a full run of the bundled data needs five sensors on
the target asset. The `location` in each payload comes from the filename and must match
the sensor's Location character for character.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then copy the credential template and fill it in:

```bash
cp .env-example .env
```

`IM_API_KEY` and `IM_API_SECRET` come from **app.industrialmatrix.com/profile → API
tab** (visible to Plant Admin and IT users only). `.env` is gitignored; `.env-example`
is the committable template. Real environment variables take precedence over `.env`, so
CI can inject secrets without writing a file.

| Variable | Required | Purpose |
| --- | --- | --- |
| `IM_API_KEY` | yes | Your public API key. |
| `IM_API_SECRET` | yes | Your secret key. Keep private. |
| `IM_API_BASE` | no | Override the API root (e.g. staging). |

## Usage

Parse the CSVs and print what *would* be sent — no credentials, no network:

```bash
.venv/bin/python manual_route_example.py --dry-run
```

```
Parsed 10 measurement(s) from files:
  Line1-Machine1-Asset01_1H_Enveloped_Acc_Band_3.csv  location=1H  type=envelope  overall=5.58311  rpm=1755  points=1601  unit=g
  Line1-Machine1-Asset01_1H_Velocity.csv              location=1H  type=velocity  overall=3.42728  rpm=1755  points=801   unit=mm/s
  ...
```

Submit everything, letting the script discover the plant and asset by name:

```bash
.venv/bin/python manual_route_example.py --plant-name "4950 Yonge St" --asset-name "Line1-Machine1-Asset01"
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Parse and print only. No network calls, no credentials needed. |
| `--plant-name NAME` | Plant to use. Defaults to the first one returned. |
| `--asset-name NAME` | Asset to use. Exact match first, then substring. Defaults to the first. |
| `--asset-id ID` | Use this `asset_id` directly and skip plant/asset discovery. |
| `--files-dir DIR` | Read CSVs from somewhere other than `files/`. |
| `--api-base URL` | Override the API root for one run. |
| `--dump PATH` | Also write the full payloads to a JSON file. |

Exit code is `0` when every measurement was accepted, `1` if any POST failed. Failures
are reported per file and do not abort the run.

## What it does

| Step | Endpoint | Doc section |
| --- | --- | --- |
| 1. Authenticate | `POST /v2/access_token` | 4 |
| 2. Pick a plant | `GET /v2/plants` | 5 |
| 3. Resolve the real `asset_id` | `GET /v2/assets?plant_id=…` | 6 |
| 4. Submit each measurement | `POST /v2/manual-route/data` | 7 |

Every endpoint except the login requires `Authorization: Bearer <access_token>`; the
client sets that header on the session once, after login.

## CSV → payload mapping

### Filename

```
Line1-Machine1-Asset01_1H_Velocity.csv
└─────────┬──────────┘ └┬┘ └───┬───┘
     asset label    location type
```

- **asset label** — the export tool's own name for the asset. **Cosmetic.** It is *not*
  the `asset_id` the API wants; that comes from `GET /v2/assets`.
- **location** — one digit + one uppercase letter (`1H`, `4V`). Becomes `payload["location"]`
  verbatim and must match a Manual Route sensor's Location.
- **type** — `Velocity` → `"velocity"`; `Enveloped_Acc_Band_<N>` → `"envelope"`.

Adding a new `<asset>_<location>_<type>.csv` to `files/` extends coverage
automatically; no code changes needed.

### Metadata rows

Each file has a metadata section of ragged `key,value,,,` rows, then a spectrum.

| CSV key | Payload field | Conversion |
| --- | --- | --- |
| `Date/Time` | `timestamp` | `"01 Jul 2026 14.52.40"` → unix seconds, parsed as UTC |
| `X-axis Units` | `x_axis_unit` | verbatim (`Hz`) |
| `Y-axis Units` | `y_axis_unit` | normalized — see below |
| `Overall` | `overall` | `float` |
| `RPM` | `rpm` | `int` |
| `Unit ID` | `meta_data["Unit ID"]` | `int` |
| `OVD` | `meta_data["OVD"]` | `str` |
| `Sensitivity` | `meta_data["Sensitivity"]` | `float` |
| `Max Freq / Orders` | `meta_data["Max Freq / Orders"]` | `int` |
| `Detection` | `meta_data["Detection"]` | `str` (`RMS`, `TruePkPk`) |
| `No. of Lines` | `meta_data["No. of Lines"]` | `int` |
| `Window Type` | `meta_data["Window Type"]` | `str` (`Hanning`) |

`y_axis_unit` needs normalizing — the raw string is never a valid value on its own:
`"Velocity (mm/s)"` → `mm/s` (parenthesized unit wins), `"gE"` → `g`.

Other rows in the files (`Application`, `CH1 Units`, `CH1 Y-Axis`, `Sensor Type`) are
redundant with the above and not part of the payload schema.

### Data section

The row `X-Axis,Y-Axis,,,` **is a section marker, not a data point.** Every row after it
becomes `[float(x), float(y)]` in `data`. Blank and ragged trailing rows are skipped.

Velocity files carry 801 points (800 lines, max 1000 Hz); envelope files carry 1601
(1600 lines, max 2000 Hz).

## Gotchas

- **The filename's asset label and the payload's `asset_id` are different things** — a
  display string versus a real foreign key. The parser keeps the label in
  `Measurement.asset_label` and never lets it reach the payload.
- **`location` must come from the filename** — never hardcoded, never overwritten. Each
  file carries a different one on purpose, and it has to match the sensor exactly.
- **Don't parse the `X-Axis,Y-Axis` marker row** as `[float("X-Axis"), float("Y-Axis")]`.
- **A blank `.env` is treated as unset.** Values are stripped, so a freshly copied
  template produces a clear error instead of sending empty credentials.

## Known documentation discrepancies

Worth knowing if you are reading the spec alongside this code:

- **Section 7.2 (POST response structure)** shows the *login* response — `user` plus
  `access_token`. That cannot be what an ingestion endpoint returns. This client only
  checks for a 2xx status and ignores the body.
- **Base URL.** Section 2 gives `https://api.industrialmatrix.com/`, while this repo
  defaults to `https://api.pdmmatrix.assetmatrix.com`. Set `IM_API_BASE` to whichever is
  correct for your deployment.
