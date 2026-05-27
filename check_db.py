import sqlite3
from datetime import datetime, timezone, timedelta

UTC8 = timezone(timedelta(hours=8))
UTC0 = timezone.utc

def ts2dt(ts_ns):
    return datetime.fromtimestamp(ts_ns / 1e9, tz=UTC8)

conn = sqlite3.connect('kbars_cache.db')
cur = conn.cursor()

# Get actual min/max
cur.execute("SELECT MIN(ts), MAX(ts) FROM kbars1m WHERE contract_code LIKE 'TXF%'")
mn, mx = cur.fetchone()
print(f"DB range (TXF): {ts2dt(mn)} ~ {ts2dt(mx)}")
print(f"  (UTC: {datetime.fromtimestamp(mn/1e9, tz=UTC0)} ~ {datetime.fromtimestamp(mx/1e9, tz=UTC0)})")

# Compute correct NS timestamps for 2026-05-15
may15_0000_utc = datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC0)
may15_0845_utc8 = datetime(2026, 5, 15, 8, 45, 0, tzinfo=UTC8)  # market open TW
may15_1345_utc8 = datetime(2026, 5, 15, 13, 45, 0, tzinfo=UTC8)  # market close TW
may16_0000_utc = datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC0)

print(f"\n5/15 market session (UTC):")
print(f"  08:45 UTC+8 = {may15_0845_utc8.astimezone(UTC0)}")
print(f"  13:45 UTC+8 = {may15_1345_utc8.astimezone(UTC0)}")

start_ns = int(may15_0845_utc8.timestamp() * 1e9)
end_ns   = int(may15_1345_utc8.timestamp() * 1e9)

cur.execute("""
    SELECT contract_code, COUNT(*)
    FROM kbars1m
    WHERE ts >= ? AND ts <= ?
    GROUP BY contract_code
""", (start_ns, end_ns))
rows = cur.fetchall()
print(f"\n5/15 白天 K 棒 (08:45~13:45 UTC+8):")
print(f"  結果: {rows if rows else 'NONE - 完全缺失！'}")

# Also show the max ts to confirm
print(f"\nDB 最後一筆: {ts2dt(mx)} = {datetime.fromtimestamp(mx/1e9, tz=UTC0)} UTC")
print(f"5/15 開盤時間: {may15_0845_utc8} = {may15_0845_utc8.astimezone(UTC0)} UTC")
print(f"缺口: {(may15_0845_utc8.timestamp() - mx/1e9)/60:.1f} 分鐘")

# cached_dates
cur.execute("SELECT * FROM cached_dates ORDER BY date DESC LIMIT 5")
print(f"\n最近 cached_dates: {cur.fetchall()}")

conn.close()
