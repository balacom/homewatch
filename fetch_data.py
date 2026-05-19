"""
Full re-fetch: deletes all records and re-downloads everything from data.gov.sg.
Use for initial setup or disaster recovery. For routine monthly updates use fetch_incremental.py.
"""
import requests
import sqlite3
import time

DB_PATH = '/opt/homewatch/data.db'
API_URL = 'https://data.gov.sg/api/action/datastore_search'
RESOURCE_ID = 'f1765b54-a209-4718-8d38-a39237f502b3'
API_KEY = 'v2:3b4eb1f2e8e748682f185fcf0ad667152615a89c11f36f7605362e76aae553eb:SXzlOqWvnV0UPONpOJN6UnBHpTx60uc_'
HEADERS = {'Authorization': API_KEY}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY,
        month TEXT,
        town TEXT,
        flat_type TEXT,
        block TEXT,
        street_name TEXT,
        storey_range TEXT,
        floor_area_sqm REAL,
        flat_model TEXT,
        lease_commence_date TEXT,
        remaining_lease TEXT,
        resale_price REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_town ON transactions(town)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_block ON transactions(block)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_street ON transactions(street_name)')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_tx
        ON transactions(month, town, flat_type, block, street_name,
                        storey_range, floor_area_sqm, resale_price)''')
    conn.commit()
    conn.close()

def fetch_all():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute('DELETE FROM transactions')
    conn.commit()
    offset = 0
    limit = 10000
    total = 0
    print("Fetching all data from data.gov.sg...")
    while True:
        r = requests.get(API_URL, headers=HEADERS,
            params={'resource_id': RESOURCE_ID, 'limit': limit, 'offset': offset})
        data = r.json()
        records = data['result']['records']
        if not records:
            break
        rows = [(rec.get('month'), rec.get('town'), rec.get('flat_type'),
                 rec.get('block'), rec.get('street_name'), rec.get('storey_range'),
                 float(rec.get('floor_area_sqm') or 0), rec.get('flat_model'),
                 rec.get('lease_commence_date'), rec.get('remaining_lease'),
                 float(rec.get('resale_price') or 0)) for rec in records]
        conn.executemany('''INSERT OR IGNORE INTO transactions
            (month, town, flat_type, block, street_name, storey_range,
             floor_area_sqm, flat_model, lease_commence_date, remaining_lease, resale_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', rows)
        conn.commit()
        total += len(records)
        print(f"  Fetched {total:,} records...")
        offset += limit
        if offset >= data['result']['total']:
            break
        time.sleep(0.1)
    conn.close()
    final = sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"Done. API records: {total:,} | DB rows: {final:,}")

if __name__ == '__main__':
    fetch_all()
