import sqlite3
import json

conn = sqlite3.connect("database/trading.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cursor.fetchall()]
print("Tables found:", tables)

for t in tables:
    cursor.execute(f"SELECT COUNT(*) FROM {t}")
    cnt = cursor.fetchone()[0]
    cursor.execute(f"PRAGMA table_info({t})")
    cols = [c[1] for c in cursor.fetchall()]
    print(f"Table {t} ({cnt} rows): cols={cols}")

