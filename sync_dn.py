"""
Sync DN_S (Delivery Note line items, 2025 onward) from Metabase into dn/ as monthly .csv.gz files.
Runs in GitHub Actions (sync_dn.yml). METABASE_URL defaults to SolarSquare's Metabase. Needs either:
  - METABASE_USERNAME and METABASE_PASSWORD (your normal Metabase login), or
  - METABASE_API_KEY (created by a Metabase admin)

    python sync_dn.py          # missing months + refresh the last 2 months
    python sync_dn.py --full   # re-export every month
"""
import csv, gzip, io, json, os, sys, time
from datetime import date, datetime, timezone
from pathlib import Path
import requests

METABASE_URL = (os.environ.get("METABASE_URL") or "https://metabase-lighthouse.solarsquare.in").rstrip("/")
API_KEY = os.environ.get("METABASE_API_KEY", "")
USERNAME = os.environ.get("METABASE_USERNAME", "")
PASSWORD = os.environ.get("METABASE_PASSWORD", "")
DATABASE_ID = 9                     # ERP (MySQL)
START = date(2025, 1, 1)
OUT_DIR = Path("dn")
REFRESH_MONTHS = 2                  # current + previous month (catches cancelled/amended DNs)

# Same SQL as Metabase question DN_S (#5728), limited to one month at a time.
SQL = """
SELECT dni.parent AS DN_NO, dn.posting_date, dn.customer AS Project, dn.workflow_state,
       dni.warehouse AS source_warehouse, dni.target_warehouse AS target_warehouse,
       dni.custom_item_category AS item_category, dni.custom_item_subcategory AS item_subcategory,
       dni.item_code, dni.item_name, dni.qty, dni.uom AS uohm, dni.rate, dni.amount,
       dn.status AS `Delivery Note Status`, dn.docstatus
FROM `tabDelivery Note Item` dni
LEFT JOIN `tabDelivery Note` dn ON dni.parent = dn.name
WHERE dn.docstatus = 1
  AND dn.posting_date >= '{start}' AND dn.posting_date < '{end}'
ORDER BY dn.posting_date DESC
"""

def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        yield first, date(y, m, 1)

def auth_headers():
    """Use the API key if there is one, otherwise log in with email + password."""
    if API_KEY:
        return {"X-API-Key": API_KEY}
    if not (USERNAME and PASSWORD):
        sys.exit("Set METABASE_USERNAME and METABASE_PASSWORD (or METABASE_API_KEY).")
    r = requests.post(f"{METABASE_URL}/api/session",
                      json={"username": USERNAME, "password": PASSWORD}, timeout=60)
    if r.status_code in (400, 401):
        sys.exit("Metabase login failed: check METABASE_USERNAME / METABASE_PASSWORD. "
                 "If you sign in to Metabase with Google, this account has no password; use an API key.")
    r.raise_for_status()
    return {"X-Metabase-Session": r.json()["id"]}

def logout(headers):
    if "X-Metabase-Session" in headers:
        try:
            requests.delete(f"{METABASE_URL}/api/session", headers=headers, timeout=30)
        except Exception:
            pass

HEADERS = {}

def fetch_csv(first, nxt, attempts=3):
    query = {"database": DATABASE_ID, "type": "native",
             "native": {"query": SQL.format(start=first, end=nxt)}}
    for i in range(attempts):
        try:
            r = requests.post(f"{METABASE_URL}/api/dataset/csv",
                              headers=HEADERS,
                              data={"query": json.dumps(query), "format_rows": "false"},
                              timeout=1200)
            r.raise_for_status()
            return r.content.decode("utf-8-sig")
        except Exception as e:
            if i == attempts - 1:
                raise
            print(f"  retrying after error: {e}")
            time.sleep(30)

def summarise(text):
    rows, amount = 0, 0.0
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        rows += 1
        try:
            amount += float(str(row.get("amount") or 0).replace(",", ""))
        except ValueError:
            pass
    return rows, round(amount, 2)

def main():
    HEADERS.update(auth_headers())
    try:
        run()
    finally:
        logout(HEADERS)

def run():
    OUT_DIR.mkdir(exist_ok=True)
    manifest_path = OUT_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"months": {}}
    full = "--full" in sys.argv
    all_months = list(months(START, date.today()))
    recent = {f"{m:%Y-%m}" for m, _ in all_months[-REFRESH_MONTHS:]}

    for first, nxt in all_months:
        key = f"{first:%Y-%m}"
        path = OUT_DIR / f"dn_{key}.csv.gz"
        if not full and path.exists() and key not in recent and key in manifest["months"]:
            continue
        t = time.time()
        text = fetch_csv(first, nxt)
        # mtime=0 keeps the file byte-identical when the data hasn't changed, so git sees no diff
        with open(path, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f,
                                                  mtime=0, compresslevel=9) as gz:
            gz.write(text.encode("utf-8"))
        rows, amount = summarise(text)
        manifest["months"][key] = {"file": path.name, "rows": rows, "amount": amount,
                                   "bytes": path.stat().st_size}
        print(f"{key}: {rows:>7,} rows  {path.stat().st_size/1e6:5.1f} MB  ({time.time()-t:.0f}s)")

    manifest["months"] = dict(sorted(manifest["months"].items()))
    manifest["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["total_rows"] = sum(m["rows"] for m in manifest["months"].values())
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print(f"Done. {manifest['total_rows']:,} rows across {len(manifest['months'])} months.")

if __name__ == "__main__":
    main()
