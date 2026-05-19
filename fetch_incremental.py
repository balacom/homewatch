"""
Incremental sync: fetches only records newer than the latest month in the DB.
Run monthly after data.gov.sg publishes new HDB resale data.
Full re-fetch: run fetch_data.py instead.
"""
import requests
import sqlite3
import time

DB_PATH = '/opt/homewatch/data.db'
API_KEY = 'v2:3b4eb1f2e8e748682f185fcf0ad667152615a89c11f36f7605362e76aae553eb:SXzlOqWvnV0UPONpOJN6UnBHpTx60uc_'
RESOURCE_ID = 'f1765b54-a209-4718-8d38-a39237f502b3'
HEADERS = {'Authorization': API_KEY}

conn = sqlite3.connect(DB_PATH)

db_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
latest = conn.execute("SELECT MAX(month) FROM transactions").fetchone()[0]
print(f"DB: {db_count:,} records, latest month: {latest}")

r = requests.get('https://data.gov.sg/api/action/datastore_search',
    headers=HEADERS, params={'resource_id': RESOURCE_ID, 'limit': 1}, timeout=30)
api_total = r.json()['result']['total']
gap = api_total - db_count
print(f"API: {api_total:,} records | Gap: {gap}")

if gap <= 0:
    print("DB is fully in sync — nothing to do.")
    conn.close()
    exit(0)

# Fetch from latest month descending — stop once past it (fast path for new months)
print(f"Fetching new records...")
offset = 0
limit = 10000
inserted = 0

while True:
    r = requests.get('https://data.gov.sg/api/action/datastore_search',
        headers=HEADERS,
        params={'resource_id': RESOURCE_ID, 'limit': limit, 'offset': offset, 'sort': 'month desc'},
        timeout=30)
    data = r.json()
    if not data.get('success'):
        print("API error:", data)
        break

    records = data['result']['records']
    if not records:
        break

    batch = []
    stop = False
    for rec in records:
        m = rec.get('month', '')
        if m < latest:
            stop = True
            break
        batch.append((
            m, rec.get('town'), rec.get('flat_type'),
            rec.get('block'), rec.get('street_name'), rec.get('storey_range'),
            float(rec.get('floor_area_sqm') or 0), rec.get('flat_model'),
            rec.get('lease_commence_date'), rec.get('remaining_lease'),
            float(rec.get('resale_price') or 0)
        ))

    if batch:
        cur = conn.executemany("""
            INSERT OR IGNORE INTO transactions
                (month, town, flat_type, block, street_name, storey_range,
                 floor_area_sqm, flat_model, lease_commence_date, remaining_lease, resale_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, batch)
        conn.commit()
        inserted += cur.rowcount

    print(f"  Offset {offset}: {inserted} new rows inserted...")

    if stop or len(records) < limit:
        break
    offset += limit
    time.sleep(0.1)

final = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
new_latest = conn.execute("SELECT MAX(month) FROM transactions").fetchone()[0]
conn.close()

print(f"\nDone. {inserted} new records added.")
print(f"Total rows: {final:,} | Latest month: {new_latest}")
