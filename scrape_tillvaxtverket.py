from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

API_URL = (
    "https://oppnadata.tillvaxtverket.se/api/api/query/"
    "inkvartering/data/GuestNights_Country_Month.cbase"
)

START_YEAR = int(os.getenv("START_YEAR", "2010"))
MUNICIPALITY = os.getenv("MUNICIPALITY", "Luleå")
LEVEL_NAME = os.getenv("LEVEL_NAME", "Kommun")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "100000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "90"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "4"))
REQUIRE_START_YEAR = os.getenv("REQUIRE_START_YEAR", "1") != "0"

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
HISTORY_PATH = DATA_DIR / "history.csv"
LATEST_PATH = DATA_DIR / "latest.csv"
REVISIONS_PATH = DATA_DIR / "revisions.csv"

MONTHS = {
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "april": 4,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

HISTORY_COLUMNS = ["Ar", "Manad", "ArManad", "Kommun", "Hemland", "Gastnatter"]
REVISION_COLUMNS = [
    "Kontrolldatum",
    "Typ",
    "ArManad",
    "Kommun",
    "Hemland",
    "Gammalt_varde",
    "Nytt_varde",
]
KEY_COLUMNS = ["ArManad", "Kommun", "Hemland"]


RAW_COLUMNS = [
    "KOMMUN_NAMN",
    "NIVA_NAMN",
    "AR",
    "MANAD_NAMN_LANG",
    "LAND_NAMN",
    "ANLAGGNINGSTYP_NAMN",
    "ANTAL_GASTNATTER",
]


def api_params(offset: int, mode: str) -> list[tuple[str, str]]:
    """
    Use an ungrouped query and aggregate locally.

    Tillvaxtverket's cBase currently returns HTTP 500 for the original
    grouped query. Region Uppsala's public implementation uses ungrouped
    column queries against the same cBase, so that is the primary approach
    here. We first ask the server to filter Lulea. If that filter is not
    accepted by the service, we fall back to an unfiltered paginated query
    and filter locally.
    """
    params: list[tuple[str, str]] = []

    if mode == "municipality":
        params.append(("filter:KOMMUN_NAMN", MUNICIPALITY))
    elif mode != "all":
        raise ValueError(f"Okant API-lage: {mode}")

    for column in RAW_COLUMNS:
        params.append(("column", column))

    params.extend(
        [
            ("limit", str(PAGE_SIZE)),
            ("offset", str(offset)),
            ("format", "json"),
            ("formatted", "false"),
        ]
    )
    return params


def fetch_page(session: requests.Session, offset: int, mode: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        response = None
        try:
            response = session.get(
                API_URL,
                params=api_params(offset, mode),
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "LuleaRobert2/Tillvaxtverket-inkvartering"},
            )
            if not response.ok:
                body = response.text[:1200].replace("\n", " ")
                raise RuntimeError(
                    f"HTTP {response.status_code} fran API:t. Svar: {body or '<tomt svar>'}"
                )
            payload = response.json()
            if not isinstance(payload, dict) or "rows" not in payload:
                raise RuntimeError("API-svaret saknar faltet 'rows'.")
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = 2 ** (attempt - 1)
            print(
                f"API-forsok {attempt} misslyckades i lage {mode}: {exc}. "
                f"Nytt forsok om {wait}s."
            )
            time.sleep(wait)
    raise RuntimeError(f"Tillvaxtverkets API kunde inte hamtas i lage {mode}: {last_error}")


def fetch_rows_for_mode(mode: str) -> list[dict]:
    session = requests.Session()
    all_rows: list[dict] = []
    offset = 0

    while True:
        payload = fetch_page(session, offset, mode)
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            raise RuntimeError("API-faltet 'rows' hade ovantat format.")

        all_rows.extend(rows)
        print(
            f"Hamtat {len(rows):,} rader i lage {mode} "
            f"(totalt {len(all_rows):,})."
        )

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not all_rows:
        raise RuntimeError(f"API:t returnerade inga rader i lage {mode}.")
    return all_rows


def fetch_all_rows() -> list[dict]:
    errors: list[str] = []

    # Preferred: raw rows, but server-side filtered to Lulea.
    # Fallback: exact query pattern known to be used by Region Uppsala:
    # selected columns + pagination, with filtering performed locally.
    for mode in ("municipality", "all"):
        try:
            print(f"Provar API-lage: {mode}")
            return fetch_rows_for_mode(mode)
        except RuntimeError as exc:
            errors.append(f"{mode}: {exc}")
            if mode == "municipality":
                print(
                    "Kommunfiltret misslyckades. Provar ofiltrerat API-uttag "
                    "och filtrerar Lulea lokalt."
                )

    raise RuntimeError("Alla API-strategier misslyckades. " + " | ".join(errors))


def parse_month(value: object) -> int:
    if pd.isna(value):
        raise ValueError("Tomt månadsnamn i API-data.")
    text = str(value).strip().lower()
    if text.isdigit() and 1 <= int(text) <= 12:
        return int(text)
    if text in MONTHS:
        return MONTHS[text]
    # Be tolerant of labels such as "01 Januari".
    for name, number in MONTHS.items():
        if name in text:
            return number
    raise ValueError(f"Okänt månadsformat från API:t: {value!r}")


def normalize(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    required = {
        "KOMMUN_NAMN",
        "AR",
        "MANAD_NAMN_LANG",
        "LAND_NAMN",
        "ANTAL_GASTNATTER",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"API-svaret saknar kolumner: {sorted(missing)}")

    # Always filter locally as well. This makes the output independent of
    # whether the server-side municipality filter was used.
    municipality = df["KOMMUN_NAMN"].astype("string").str.strip()
    df = df.loc[municipality.eq(MUNICIPALITY)].copy()
    if df.empty:
        sample = sorted(
            pd.Series(rows, dtype="object").astype(str).head(5).tolist()
        )
        raise RuntimeError(
            f"Inga API-rader matchade KOMMUN_NAMN={MUNICIPALITY!r}. "
            f"Kontrollera kommunnamnet i kallan."
        )

    if "NIVA_NAMN" in df.columns:
        levels = df["NIVA_NAMN"].astype("string").str.strip()
        exact = levels.str.casefold().eq(LEVEL_NAME.casefold())
        if exact.any():
            df = df.loc[exact].copy()
        else:
            available = sorted(x for x in levels.dropna().unique().tolist() if x)
            # Some source versions may use e.g. "Kommuner". Accept a unique
            # level beginning with "kommun", otherwise stop rather than mix levels.
            kommun_levels = [x for x in available if str(x).casefold().startswith("kommun")]
            if len(kommun_levels) == 1:
                chosen = kommun_levels[0]
                print(
                    f"Obs: nivan {LEVEL_NAME!r} fanns inte; anvander "
                    f"kallans niva {chosen!r}."
                )
                df = df.loc[levels.eq(chosen)].copy()
            elif available:
                raise RuntimeError(
                    f"Kunde inte entydigt valja kommunniva. NIVA_NAMN for "
                    f"{MUNICIPALITY}: {available}"
                )

    df["AR"] = pd.to_numeric(df["AR"], errors="coerce")
    df = df.loc[df["AR"].notna() & (df["AR"] >= START_YEAR)].copy()
    if df.empty:
        raise RuntimeError(
            f"Inga rader aterstod for {MUNICIPALITY} fran {START_YEAR}."
        )

    out = pd.DataFrame()
    out["Ar"] = df["AR"].astype(int)
    out["Manad"] = df["MANAD_NAMN_LANG"].map(parse_month).astype(int)
    out["ArManad"] = out["Ar"] * 100 + out["Manad"]
    out["Kommun"] = MUNICIPALITY
    out["Hemland"] = df["LAND_NAMN"].astype("string").str.strip()
    out["Hemland"] = out["Hemland"].fillna("Ej angivet").replace("", "Ej angivet")
    out["Gastnatter"] = (
        pd.to_numeric(df["ANTAL_GASTNATTER"], errors="coerce")
        .round()
        .astype("Int64")
    )

    # "Alla varden" for accommodation type in the portal means all underlying
    # accommodation types are selected. Since the API rows are ungrouped,
    # aggregate them here to one value per year/month/country.
    out = (
        out.groupby(
            ["Ar", "Manad", "ArManad", "Kommun", "Hemland"],
            as_index=False,
            dropna=False,
        )["Gastnatter"]
        .sum(min_count=1)
        .sort_values(["ArManad", "Hemland"], kind="stable")
        .reset_index(drop=True)
    )

    if out.empty:
        raise RuntimeError("Ingen anvandbar data aterstod efter normalisering.")
    if out.duplicated(KEY_COLUMNS).any():
        raise RuntimeError(
            "Dubbletter finns kvar efter aggregering; avbryter for att skydda history.csv."
        )

    min_year = int(out["Ar"].min())
    if REQUIRE_START_YEAR and min_year > START_YEAR:
        raise RuntimeError(
            f"API:t borjar forst {min_year} for {MUNICIPALITY}; begard historik "
            f"borjar {START_YEAR}. history.csv skrivs inte sa att en ofullstandig "
            "serie inte ser komplett ut."
        )

    return out[HISTORY_COLUMNS]


def read_history() -> pd.DataFrame | None:
    if not HISTORY_PATH.exists() or HISTORY_PATH.stat().st_size == 0:
        return None
    old = pd.read_csv(HISTORY_PATH, encoding="utf-8-sig")
    missing = set(HISTORY_COLUMNS) - set(old.columns)
    if missing:
        raise RuntimeError(f"Befintlig history.csv saknar kolumner: {sorted(missing)}")
    old = old[HISTORY_COLUMNS].copy()
    old["Gastnatter"] = pd.to_numeric(old["Gastnatter"], errors="coerce").astype("Int64")
    return old


def same_value(a: object, b: object) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return int(a) == int(b)


def guard_against_partial_download(old: pd.DataFrame | None, new: pd.DataFrame) -> None:
    if old is None or old.empty:
        return

    old_max = int(old["ArManad"].max())
    new_max = int(new["ArManad"].max())
    if new_max < old_max:
        raise RuntimeError(
            f"Nya API-uttaget slutar {new_max}, tidigare history.csv slutade {old_max}. "
            "Avbryter för att undvika att ersätta historiken med ett ofullständigt uttag."
        )

    # A small number of removed rows can be a legitimate revision, but a large
    # drop is more likely a partial API response or changed filter semantics.
    if len(new) < 0.90 * len(old):
        raise RuntimeError(
            f"API-uttaget har {len(new):,} rader mot tidigare {len(old):,} (>10 % minskning). "
            "Avbryter som säkerhetskontroll."
        )


def build_revisions(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or old.empty:
        return pd.DataFrame(columns=REVISION_COLUMNS)

    old_i = old.set_index(KEY_COLUMNS)["Gastnatter"]
    new_i = new.set_index(KEY_COLUMNS)["Gastnatter"]
    now = datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%Y-%m-%d %H:%M:%S%z")
    rows: list[dict] = []

    for key in old_i.index.intersection(new_i.index):
        old_v = old_i.loc[key]
        new_v = new_i.loc[key]
        if not same_value(old_v, new_v):
            armanad, kommun, hemland = key
            rows.append(
                {
                    "Kontrolldatum": now,
                    "Typ": "REVIDERAD",
                    "ArManad": armanad,
                    "Kommun": kommun,
                    "Hemland": hemland,
                    "Gammalt_varde": old_v,
                    "Nytt_varde": new_v,
                }
            )

    for key in old_i.index.difference(new_i.index):
        armanad, kommun, hemland = key
        rows.append(
            {
                "Kontrolldatum": now,
                "Typ": "BORTTAGEN_FRAN_KALLAN",
                "ArManad": armanad,
                "Kommun": kommun,
                "Hemland": hemland,
                "Gammalt_varde": old_i.loc[key],
                "Nytt_varde": pd.NA,
            }
        )

    return pd.DataFrame(rows, columns=REVISION_COLUMNS)


def append_revisions(changes: pd.DataFrame) -> None:
    if changes.empty:
        if not REVISIONS_PATH.exists():
            pd.DataFrame(columns=REVISION_COLUMNS).to_csv(
                REVISIONS_PATH, index=False, encoding="utf-8-sig"
            )
        return

    if REVISIONS_PATH.exists() and REVISIONS_PATH.stat().st_size > 0:
        previous = pd.read_csv(REVISIONS_PATH, encoding="utf-8-sig")
        combined = pd.concat([previous, changes], ignore_index=True)
    else:
        combined = changes
    combined.to_csv(REVISIONS_PATH, index=False, encoding="utf-8-sig")


def write_outputs(new: pd.DataFrame, old: pd.DataFrame | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    guard_against_partial_download(old, new)
    revisions = build_revisions(old, new)

    new.to_csv(HISTORY_PATH, index=False, encoding="utf-8-sig")
    max_period = int(new["ArManad"].max())
    latest = new.loc[new["ArManad"] == max_period].copy()
    latest.to_csv(LATEST_PATH, index=False, encoding="utf-8-sig")
    append_revisions(revisions)

    old_keys = set() if old is None else set(map(tuple, old[KEY_COLUMNS].itertuples(index=False, name=None)))
    new_keys = set(map(tuple, new[KEY_COLUMNS].itertuples(index=False, name=None)))
    added = len(new_keys - old_keys) if old is not None else len(new_keys)

    print("\nKlart")
    print(f"  Kommun: {MUNICIPALITY}")
    print(f"  Period: {int(new['ArManad'].min())}–{max_period}")
    print(f"  Rader i history.csv: {len(new):,}")
    print(f"  Länder senaste månaden: {latest['Hemland'].nunique():,}")
    print(f"  Nya nycklar sedan föregående körning: {added:,}")
    print(f"  Reviderade/borttagna värden: {len(revisions):,}")


def main() -> int:
    print(f"Hämtar gästnätter för {MUNICIPALITY} från {START_YEAR} via Tillväxtverkets öppna API.")
    rows = fetch_all_rows()
    new = normalize(rows)
    old = read_history()
    write_outputs(new, old)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nFEL: {exc}", file=sys.stderr)
        raise
