import os
import json
import sqlite3
import random
from itertools import combinations

baseDir = r"d:\Company\HSJC\hsjc-cloud-web"
dbPath = os.path.join(baseDir, "hsjc.db")
racecardsPath = os.path.join(baseDir, "data", "racecards.json")

def generateDualTimeOdds():
    print("[Generator] Loading racecards.json...")
    with open(racecardsPath, "r", encoding="utf-8") as f:
        cards = json.load(f)

    # Group by raceNo
    races = {}
    for c in cards:
        rNo = int(c["raceNo"])
        races.setdefault(rNo, []).append(c)

    time1 = "2026-09-15 12:00:00" # 昨日周二开盘初盘基准
    time2 = "2026-09-16 12:20:00" # 今日周三中午临场大户砸盘

    conn = sqlite3.connect(dbPath)
    cursor = conn.cursor()

    # Clear any previous 2026-09-15 or 2026-09-16 dummy entries if any
    cursor.execute("DELETE FROM win WHERE CollectionDateTime LIKE '2026-09-15%' OR CollectionDateTime LIKE '2026-09-16%';")
    cursor.execute("DELETE FROM qin WHERE CollectionDateTime LIKE '2026-09-15%' OR CollectionDateTime LIKE '2026-09-16%';")
    cursor.execute("DELETE FROM qpl WHERE CollectionDateTime LIKE '2026-09-15%' OR CollectionDateTime LIKE '2026-09-16%';")
    conn.commit()

    winCount = 0
    qinCount = 0
    qplCount = 0

    random.seed(42) # Reproducible realistic odds

    for rNo in sorted(races.keys()):
        hList = races[rNo]
        hList.sort(key=lambda x: int(x["horseNo"]))
        horseNums = [int(h["horseNo"]) for h in hList]

        # 1. Generate WIN Odds
        # Score based on rating and jockey
        scores = {}
        for h in hList:
            hNo = int(h["horseNo"])
            rating = float(h["rating"]) if h.get("rating") and h["rating"].isdigit() else 30.0
            jockey = h.get("jockey", "")
            jBonus = 5.0 if "潘頓" in jockey else (4.0 if "何澤堯" in jockey else (3.0 if "田泰安" in jockey else 0.0))
            scores[hNo] = rating + jBonus + random.uniform(-2, 2)

        # Invert score to baseline win odds
        maxScore = max(scores.values()) + 5.0
        baseOddsT1 = {}
        for hNo, sc in scores.items():
            diff = maxScore - sc
            odds = round(max(2.2, 2.0 + diff * 1.5 + random.uniform(-0.5, 0.5)), 1)
            baseOddsT1[hNo] = odds

        # Time 2 WIN Odds (inject big smart money moves for top 2 horses)
        baseOddsT2 = {}
        # Pick 1 or 2 horses to be heavily backed by syndicates (狂砸大户)
        topHorses = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        backedHorse1 = topHorses[0]
        backedHorse2 = topHorses[1] if len(topHorses) > 1 else topHorses[0]

        for hNo in horseNums:
            o1 = baseOddsT1[hNo]
            if hNo == backedHorse1:
                # Big drop (30%~45% drop)
                o2 = round(max(1.8, o1 * random.uniform(0.55, 0.65)), 1)
            elif hNo == backedHorse2:
                # Moderate drop (15%~25% drop)
                o2 = round(max(2.5, o1 * random.uniform(0.75, 0.85)), 1)
            else:
                # Drifting out (回飞)
                o2 = round(o1 * random.uniform(1.05, 1.25), 1)
            baseOddsT2[hNo] = o2

        # Insert Time 1 WIN
        for hNo in horseNums:
            o1 = baseOddsT1[hNo]
            willPay1 = str(int(o1 * 1000))
            cursor.execute("""
                INSERT INTO win (CollectionDateTime, RaceNo, Number, WinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, 0.0, 0, ?);
            """, (time1, rNo, hNo, o1, willPay1))
            winCount += 1

        # Insert Time 2 WIN
        for hNo in horseNums:
            o1 = baseOddsT1[hNo]
            o2 = baseOddsT2[hNo]
            dropVal = round(o2 - o1, 1)
            hot = 1 if dropVal <= -2.0 or (o2 <= 3.0 and o2 > 0) else 0
            willPay2 = str(int(o2 * 1000))
            cursor.execute("""
                INSERT INTO win (CollectionDateTime, RaceNo, Number, WinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?);
            """, (time2, rNo, hNo, o2, dropVal, hot, willPay2))
            winCount += 1

        # 2. Generate QIN (连赢) & QPL (位置Q) Pairs
        pairs = list(combinations(horseNums, 2))
        for hA, hB in pairs:
            combKey = f"{min(hA, hB)}-{max(hA, hB)}"
            
            # Theoretical QIN odds
            o1A = baseOddsT1[hA]
            o1B = baseOddsT1[hB]
            o2A = baseOddsT2[hA]
            o2B = baseOddsT2[hB]

            qin1 = round(max(3.5, (o1A * o1B) ** 0.52 * 1.8), 1)
            # If both backed horses in combination, big syndicate drop!
            if (hA in (backedHorse1, backedHorse2)) and (hB in (backedHorse1, backedHorse2)):
                qin2 = round(max(2.8, qin1 * 0.58), 1)
            elif (hA in (backedHorse1, backedHorse2)) or (hB in (backedHorse1, backedHorse2)):
                qin2 = round(max(3.2, qin1 * random.uniform(0.78, 0.90)), 1)
            else:
                qin2 = round(qin1 * random.uniform(1.05, 1.25), 1)

            qDrop = round(qin2 - qin1, 1)
            qHot = 1 if qDrop <= -5.0 or (qin2 <= 6.0 and qin2 > 0) else 0

            # Insert QIN T1
            cursor.execute("""
                INSERT INTO qin (CollectionDateTime, RaceNo, Number, QinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, 0.0, 0, '0');
            """, (time1, rNo, combKey, qin1))
            # Insert QIN T2
            cursor.execute("""
                INSERT INTO qin (CollectionDateTime, RaceNo, Number, QinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, ?, ?, '0');
            """, (time2, rNo, combKey, qin2, qDrop, qHot))
            qinCount += 2

            # Theoretical QPL odds (~ 1/3 of QIN)
            qpl1 = round(max(1.8, qin1 * 0.38), 1)
            qpl2 = round(max(1.5, qin2 * 0.38), 1)
            qplDrop = round(qpl2 - qpl1, 1)
            qplHot = 1 if qplDrop <= -2.0 or (qpl2 <= 2.5) else 0

            # Insert QPL T1
            cursor.execute("""
                INSERT INTO qpl (CollectionDateTime, RaceNo, Number, QplOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, 0.0, 0, '0');
            """, (time1, rNo, combKey, qpl1))
            # Insert QPL T2
            cursor.execute("""
                INSERT INTO qpl (CollectionDateTime, RaceNo, Number, QplOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, 0, ?, ?, '0');
            """, (time2, rNo, combKey, qpl2, qplDrop, qplHot))
            qplCount += 2

    conn.commit()
    conn.close()

    print(f"[Generator] Successfully injected 09-16 odds:")
    print(f"  - WIN records: {winCount}")
    print(f"  - QIN records: {qinCount}")
    print(f"  - QPL records: {qplCount}")
    print(f"  - Timestamps: {time1} (初盤基準) -> {time2} (今日中午臨場)")

if __name__ == "__main__":
    generateDualTimeOdds()
