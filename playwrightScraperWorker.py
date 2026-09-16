import os
import sys
import time
import json
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# 确保控制台 UTF-8 输出且行缓冲实时刷新
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

baseDir = os.path.dirname(os.path.abspath(__file__))
dbPath = os.path.join(baseDir, "hsjc.db")

def getHkTime():
    """获取标准香港当地时间 (HKT, UTC+8)"""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def initSqliteWalMode():
    """初始化 SQLite WAL 模式，确保并发读写无锁冲突"""
    try:
        conn = sqlite3.connect(dbPath, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        conn.commit()
        conn.close()
    except Exception as err:
        print(f"[WAL Error] Failed to set WAL mode: {err}")

def detectNextMeeting():
    """读取赛期与场地"""
    metaPath = os.path.join(baseDir, "data", "race_meta.json")
    if os.path.exists(metaPath):
        try:
            with open(metaPath, "r", encoding="utf-8") as f:
                meta = json.load(f)
                rDate = meta.get("raceDate", "").replace("/", "-")
                venue = meta.get("racecourse", "HV")
                vName = meta.get("venueName", "跑馬地")
                rTime = meta.get("firstRaceTime", "19:10")
                if rDate:
                    return rDate, venue, vName, rTime
        except Exception:
            pass

    cardsPath = os.path.join(baseDir, "data", "racecards.json")
    if os.path.exists(cardsPath):
        try:
            with open(cardsPath, "r", encoding="utf-8") as f:
                cards = json.load(f)
                if cards and len(cards) > 0:
                    rDate = cards[0].get("raceDate", "").replace("/", "-")
                    venue = cards[0].get("racecourse", "HV")
                    vName = "跑馬地" if venue == "HV" else "沙田"
                    return rDate, venue, vName, "19:10" if venue == "HV" else "13:00"
        except Exception:
            pass

    now = getHkTime()
    return now.strftime("%Y-%m-%d"), "HV", "跑馬地", "19:10"

# 马会官方 Whitelisted GraphQL 查询模板（必须保持字符完全一致以绕过 WAF 白名单）
HKJC_GRAPHQL_QUERY = """query racing($date: String, $venueCode: String, $oddsTypes: [OddsType], $raceNo: Int) {
  raceMeetings(date: $date, venueCode: $venueCode) {
    pmPools(oddsTypes: $oddsTypes, raceNo: $raceNo) {
      id
      status
      sellStatus
      oddsType
      lastUpdateTime
      guarantee
      minTicketCost
      name_en
      name_ch
      leg {
        number
        races
      }
      cWinSelections {
        composite
        name_ch
        name_en
        starters
      }
      oddsNodes {
        combString
        oddsValue
        hotFavourite
        oddsDropValue
        bankerOdds {
          combString
          oddsValue
        }
      }
    }
  }
}"""

def computeOddsDrop(conn, tableName, raceNo, numberKey, currentOdds, oddsColName):
    """计算相对于当日该马匹/组合最初记录的赔率跌幅"""
    try:
        cursor = conn.cursor()
        sql = f"SELECT {oddsColName} FROM {tableName} WHERE RaceNo = ? AND Number = ? ORDER BY CollectionDateTime ASC LIMIT 1;"
        cursor.execute(sql, (raceNo, str(numberKey)))
        row = cursor.fetchone()
        if row and row[0] and float(row[0]) > 0:
            baseOdds = float(row[0])
            return round(currentOdds - baseOdds, 2)
    except Exception:
        pass
    return 0.0

def normalizeCombination(combString):
    """将马会组合字符串 '07,11' 转换为标准格式 '7-11'"""
    try:
        parts = [int(p.strip()) for p in combString.split(",") if p.strip()]
        parts.sort()
        return f"{parts[0]}-{parts[1]}"
    except Exception:
        return combString.replace(",", "-")

def scrapeLiveOddsOnce(targetDate=None, targetVenue=None, maxRaces=8):
    """
    使用 Playwright 无头浏览器执行一次全量实盘赔率抓取并直接入库
    返回: (成功抓取的总记录数, 抓取耗时秒数, 采集时间戳)
    """
    startTime = time.time()
    if not targetDate or not targetVenue:
        detectedDate, detectedVenue, _, _ = detectNextMeeting()
        targetDate = targetDate or detectedDate
        targetVenue = targetVenue or detectedVenue

    collectTime = getHkTime().strftime("%Y-%m-%d %H:%M:%S")
    totalRecords = 0

    initSqliteWalMode()
    conn = sqlite3.connect(dbPath, timeout=15.0)
    cursor = conn.cursor()

    print(f"[{collectTime}] 启动 Playwright 无头浏览器采集器... 目标: {targetDate} {targetVenue} (1-{maxRaces}场)")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 访问马会网页完成安全指纹握手
        page.goto("https://bet.hkjc.com/ch/racing/wp/", timeout=25000, wait_until="networkidle")

        jsExecutor = """
        async ([queryStr, dateStr, venueStr, rNo]) => {
            const resp = await fetch('https://info.cld.hkjc.com/graphql/base/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    operationName: 'racing',
                    query: queryStr,
                    variables: {
                        date: dateStr,
                        venueCode: venueStr,
                        raceNo: rNo,
                        oddsTypes: ['WIN', 'PLA', 'QIN', 'QPL']
                    }
                })
            });
            return await resp.json();
        }
        """

        winRows = []
        qinRows = []
        qplRows = []

        for raceNo in range(1, maxRaces + 1):
            try:
                result = page.evaluate(jsExecutor, [HKJC_GRAPHQL_QUERY, targetDate, targetVenue, raceNo])
                meetings = result.get("data", {}).get("raceMeetings") or []
                if not meetings:
                    continue
                pools = meetings[0].get("pmPools") or []

                for pool in pools:
                    oddsType = pool.get("oddsType")
                    oddsNodes = pool.get("oddsNodes") or []

                    if oddsType == "WIN":
                        for node in oddsNodes:
                            try:
                                horseNo = int(node.get("combString", "0"))
                                if horseNo <= 0:
                                    continue
                                oddsVal = float(node.get("oddsValue") or 0.0)
                                isHot = 1 if node.get("hotFavourite") or (0 < oddsVal <= 3.0) else 0
                                willPay = str(int(oddsVal * 1000)) if oddsVal > 0 else "0"
                                dropVal = computeOddsDrop(conn, "win", raceNo, horseNo, oddsVal, "WinOdds")
                                if dropVal <= -3.0:
                                    isHot = 1
                                winRows.append((collectTime, raceNo, horseNo, oddsVal, 0, dropVal, isHot, willPay))
                            except Exception:
                                pass

                    elif oddsType == "QIN":
                        for node in oddsNodes:
                            try:
                                combStr = normalizeCombination(node.get("combString", ""))
                                oddsVal = float(node.get("oddsValue") or 0.0)
                                isHot = 1 if node.get("hotFavourite") else 0
                                willPay = str(int(oddsVal * 1000)) if oddsVal > 0 else "0"
                                dropVal = computeOddsDrop(conn, "qin", raceNo, combStr, oddsVal, "QinOdds")
                                qinRows.append((collectTime, raceNo, combStr, oddsVal, 0, dropVal, isHot, willPay))
                            except Exception:
                                pass

                    elif oddsType == "QPL":
                        for node in oddsNodes:
                            try:
                                combStr = normalizeCombination(node.get("combString", ""))
                                oddsVal = float(node.get("oddsValue") or 0.0)
                                isHot = 1 if node.get("hotFavourite") else 0
                                willPay = str(int(oddsVal * 1000)) if oddsVal > 0 else "0"
                                dropVal = computeOddsDrop(conn, "qpl", raceNo, combStr, oddsVal, "QplOdds")
                                qplRows.append((collectTime, raceNo, combStr, oddsVal, 0, dropVal, isHot, willPay))
                            except Exception:
                                pass

                print(f"  [Race {raceNo}] 成功抓取: WIN={len([r for r in winRows if r[1]==raceNo])}, QIN={len([r for r in qinRows if r[1]==raceNo])}, QPL={len([r for r in qplRows if r[1]==raceNo])}")

            except Exception as err:
                print(f"  [Race {raceNo}] 抓取异常: {err}")

        browser.close()

    # 批量事务写入 SQLite
    try:
        if winRows:
            cursor.executemany("""
                INSERT INTO win (CollectionDateTime, RaceNo, Number, WinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, winRows)
        if qinRows:
            cursor.executemany("""
                INSERT INTO qin (CollectionDateTime, RaceNo, Number, QinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, qinRows)
        if qplRows:
            cursor.executemany("""
                INSERT INTO qpl (CollectionDateTime, RaceNo, Number, QplOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, qplRows)

        conn.commit()
        totalRecords = len(winRows) + len(qinRows) + len(qplRows)
        print(f"[{collectTime}] 批量写入完成: 共写入 {totalRecords} 条实盘赔率记录 (WIN:{len(winRows)}, QIN:{len(qinRows)}, QPL:{len(qplRows)})")
    except Exception as dbErr:
        print(f"[DB Error] 写入数据库失败: {dbErr}")
        conn.rollback()
    finally:
        conn.close()

    elapsed = time.time() - startTime
    print(f"[{collectTime}] 采集周期结束，耗时: {elapsed:.2f} 秒")
    return totalRecords, elapsed, collectTime

def autoSyncToGithub():
    """将采集到的最新数据库定期推送至 GitHub 触发 Render 自动同步"""
    import subprocess
    try:
        cmd = 'git add hsjc.db; git commit -m "data: periodic live odds auto-sync"; git push origin main'
        res = subprocess.run(["powershell", "-Command", cmd], cwd=baseDir, capture_output=True, text=True, timeout=40)
        if res.returncode == 0:
            print("[Git AutoSync] 成功将最新实盘数据同步推送到 GitHub 远程仓库！")
        else:
            errOut = (res.stderr or res.stdout or "").strip()
            if "nothing to commit" not in errOut:
                print(f"[Git AutoSync] 同步反馈: {errOut[:200]}")
    except Exception as e:
        print(f"[Git AutoSync Exception] {e}")

def runContinuousWorker():
    """常驻后台轮询采集主循环"""
    print("[Playwright Worker] 启动常驻轮询服务...")
    cycleCount = 0
    while True:
        try:
            nowHk = getHkTime()
            hour = nowHk.hour
            weekday = nowHk.weekday()

            # 计算智能休眠间隔
            # 夜赛（周三） 18:00-23:00 -> 15秒高频
            # 日赛（周日/周六） 12:00-18:30 -> 15秒高频
            # 早盘阶段 -> 120秒
            # 其他时段 -> 600秒
            sleepInterval = 600
            isRaceDay = (weekday == 2) or (weekday in [5, 6])
            if isRaceDay:
                if (weekday == 2 and 18 <= hour <= 23) or (weekday in [5, 6] and 12 <= hour <= 18):
                    sleepInterval = 15
                elif 9 <= hour <= 18:
                    sleepInterval = 120

            totalRec, _, _ = scrapeLiveOddsOnce()
            cycleCount += 1

            # 每 3 个周期 (约 6 分钟) 自动推送 GitHub 一次，确保云端 Render 也是最新
            if cycleCount % 3 == 0 and totalRec > 0:
                autoSyncToGithub()

            print(f"[Playwright Worker] 下次采集将在 {sleepInterval} 秒后执行...\n")
            time.sleep(sleepInterval)
        except KeyboardInterrupt:
            print("[Playwright Worker] 用户中止退出。")
            break
        except Exception as err:
            print(f"[Playwright Worker Error] 轮询异常: {err}")
            time.sleep(30)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HKJC Playwright Live Odds Scraper")
    parser.add_argument("--once", action="store_true", help="仅单次抓取入库后退出")
    parser.add_argument("--races", type=int, default=8, help="抓取场次数目 (默认 8)")
    args = parser.parse_args()

    if args.once:
        scrapeLiveOddsOnce(maxRaces=args.races)
    else:
        runContinuousWorker()
