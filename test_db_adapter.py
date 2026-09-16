import dbAdapter

print("Database status:", dbAdapter.getDatabaseStatus())

# Test timestamps
timestamps = dbAdapter.queryAll("SELECT DISTINCT CollectionDateTime FROM win ORDER BY CollectionDateTime DESC LIMIT 3")
print("Timestamps:", [r["CollectionDateTime"] for r in timestamps])

# Test compare query
sql = "SELECT Number, WinOdds, Scratched, Hot FROM win WHERE CollectionDateTime = ? AND RaceNo = ?"
rows = dbAdapter.queryAll(sql, ["2026-09-16 23:00:00", 1])
print(f"Compare query: got {len(rows)} horses for Race 1 at 23:00:00")
if rows:
    print("Sample horse:", rows[0])

# Test matrix query
matrixRows = dbAdapter.queryAll("SELECT Number, QinOdds, OddsDrop FROM qin WHERE CollectionDateTime = ? AND RaceNo = ?", ["2026-09-16 23:00:00", 1])
print(f"Matrix query: got {len(matrixRows)} combinations for Race 1 QIN")
