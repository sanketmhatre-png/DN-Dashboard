"""
Sync BOM_S (BOM line items, BOMs created 2025 onward) from Metabase into bom/ as monthly .csv.gz files.
Runs in GitHub Actions (sync_bom.yml). METABASE_URL defaults to SolarSquare's Metabase. Needs either:
  - METABASE_USERNAME and METABASE_PASSWORD (your normal Metabase login), or
  - METABASE_API_KEY (created by a Metabase admin)

    python sync_bom.py          # missing months + refresh the last 2 months
    python sync_bom.py --full   # re-export every month
"""
import csv, gzip, io, json, os, re, sys, time
from datetime import date, datetime, timezone
from pathlib import Path
import requests

METABASE_URL = (os.environ.get("METABASE_URL") or "https://metabase-lighthouse.solarsquare.in").rstrip("/")
API_KEY = os.environ.get("METABASE_API_KEY", "")
USERNAME = os.environ.get("METABASE_USERNAME", "")
PASSWORD = os.environ.get("METABASE_PASSWORD", "")
DATABASE_ID = 9                     # ERP (MySQL)
START = date(2025, 1, 1)
OUT_DIR = Path("bom")
REFRESH_MONTHS = 2                  # current + previous month (catches BOMs cancelled or completed since)

# Same columns as Metabase question BOM_S (#5883), plus the BOM creation date and the item category,
# one month (by BOM creation date) at a time.
SQL = """
SELECT b.name AS bom_id, DATE(b.creation) AS creation_date, b.project, b.item, b.item_name, b.quantity,
       b.warehouse, bi.name AS bom_item_name, it.custom_item_category AS item_category, bi.item_code,
       bi.item_name AS bom_item_item_name, bi.qty, bi.rate, bi.amount, b.workflow_state, b.docstatus
FROM tabBOM b
JOIN `tabBOM Item` bi ON b.name = bi.parent
LEFT JOIN tabItem it ON it.name = bi.item_code
WHERE b.creation >= '{start}' AND b.creation < '{end}'
ORDER BY b.creation DESC
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


# ---------- All-time summary for the dashboard (dn/summary.json) ----------
STATES = {"MH": "Maharashtra", "UP": "Uttar Pradesh", "MP": "Madhya Pradesh", "TN": "Tamil Nadu",
          "RJ": "Rajasthan", "GJ": "Gujarat", "DL": "Delhi", "TL": "Telangana", "TS": "Telangana",
          "KA": "Karnataka", "HR": "Haryana", "AP": "Andhra Pradesh", "CG": "Chhattisgarh",
          "PB": "Punjab", "WB": "West Bengal", "KL": "Kerala", "OR": "Odisha", "OD": "Odisha",
          "BR": "Bihar", "UK": "Uttarakhand", "JH": "Jharkhand", "GA": "Goa", "AS": "Assam"}
NO_STATE = "Not state-coded"
# Keep these rules identical to customerInfo() in bom.html
def customer_info(c):
    c = (c or "").strip()
    m = re.match(r"^(RSSE|SSE)([A-Z]{2})", c)
    if m:
        return f"{m.group(1)} projects", STATES.get(m.group(2), m.group(2))
    u = c.upper()
    if u.startswith("SSHS"): return "SSHS projects", NO_STATE
    if u.startswith("CCNI"): return "CCNI projects", NO_STATE
    if u.startswith("SOLARSQUARE ENERGY"): return "Internal transfers", NO_STATE
    if c.endswith(" Customer"): return "Service customers", NO_STATE
    return "Other customers", NO_STATE

def norm(v, empty):
    v = " ".join((v or "").split())
    return v or empty

def num(v):
    try: return float(str(v).replace(",", "") or 0)
    except ValueError: return 0.0

def build_summary(manifest):
    dims = {k: [] for k in ("warehouse", "category", "group", "state")}
    index = {k: {} for k in dims}
    def idx(k, v):
        d = index[k]
        if v not in d:
            d[v] = len(dims[k]); dims[k].append(v)
        return d[v]
    months = sorted(manifest["months"])
    cube, dn_cube = {}, {}
    cust_cache = {}
    first_date, last_date = None, None
    for mi, key in enumerate(months):
        path = OUT_DIR / manifest["months"][key]["file"]
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                cust = row.get("project", "")
                if cust not in cust_cache:
                    cust_cache[cust] = customer_info(cust)
                st = cust_cache[cust][1]
                g = norm(row.get("workflow_state"), "No status")      # BOM status is the "group" here
                w = idx("warehouse", norm(row.get("warehouse"), "No warehouse"))
                c = idx("category", norm(row.get("item_category"), "Uncategorised"))
                gi, si = idx("group", g), idx("state", st)
                dn = row.get("bom_id", "")
                k = (mi, w, c, gi, si)
                cell = cube.get(k)
                if cell is None:
                    cell = cube[k] = [0, 0.0, set()]
                cell[0] += 1
                cell[1] += num(row.get("amount"))
                cell[2].add(dn)
                dn_cube.setdefault((mi, w, gi, si), set()).add(dn)
                d = (row.get("creation_date") or "")[:10]
                if d:
                    first_date = d if not first_date or d < first_date else first_date
                    last_date = d if not last_date or d > last_date else last_date
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_date": first_date, "last_date": last_date,
        "months": months, "dims": dims,
        # cube rows: month, warehouse, category, status, state, line items, amount, BOMs
        "cube": [[*k, v[0], round(v[1]), len(v[2])] for k, v in cube.items()],
        # BOMs without category split (exact BOM counts when no category filter)
        "dn_cube": [[*k, len(v)] for k, v in dn_cube.items()],
    }

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
    t = time.time()
    summary = build_summary(manifest)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, separators=(",", ":")))
    print(f"Summary: {len(summary['cube']):,} cells ({time.time()-t:.0f}s)")
    print(f"Done. {manifest['total_rows']:,} rows across {len(manifest['months'])} months.")

if __name__ == "__main__":
    main()
