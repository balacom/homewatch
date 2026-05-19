import sqlite3

conn = sqlite3.connect('/opt/homewatch/data.db')

before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
print(f"Rows before: {before}")

# Dedup first (safety check — index creation fails if dupes exist)
conn.execute("""
    DELETE FROM transactions WHERE id NOT IN (
        SELECT MIN(id) FROM transactions
        GROUP BY month, town, flat_type, block, street_name,
                 storey_range, floor_area_sqm, resale_price
    )
""")
conn.commit()
after = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
print(f"Rows after dedup: {after} ({before - after} removed)")

# Create unique index — makes INSERT OR IGNORE work correctly going forward
conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_tx
    ON transactions(month, town, flat_type, block, street_name,
                    storey_range, floor_area_sqm, resale_price)
""")
conn.commit()
print("UNIQUE index created on transactions.")

# Verify
indexes = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='transactions'").fetchall()
for idx in indexes:
    print(f"  Index: {idx[0]}")

conn.close()
