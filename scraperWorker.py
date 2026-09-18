import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
import dbAdapter

def getHkTime():
    """获取标准香港当地时间 (HKT, UTC+8)"""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

baseDir = os.path.dirname(os.path.abspath(__file__))
dbPath = os.path.join(baseDir, "hsjc.db")

scraperConfig = {
    "enabled": True,
    "mode": "smart",        # "smart" (阶梯加速) 或 "fixed" (固定秒数)
    "fixedInterval": 60,    # fixed 模式下的秒数
    "currentInterval": 60,
    "lastRunTime": None,
    "lastStatus": "Initialized",
    "totalRuns": 0,
    "totalRecordsSaved": 0,
    "targetDate": None,
    "targetVenue": None,
    "targetVenueName": None,
    "firstRaceTime": None,
    "meetingClosed": False,
    "currentTargetDate": None
}

def initSqliteWalMode():
    """初始化本地 SQLite WAL 模式，若接入 Turso 云端则由云端自动接管事务"""
    if dbAdapter.isTursoEnabled():
        return
    try:
        conn = sqlite3.connect(dbPath, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 5000;")
        conn.commit()
        conn.close()
        print("[WAL] Local SQLite initialized in WAL mode.")
    except Exception as err:
        print("[WAL Error] Failed to set WAL mode:", err)

def detectNextMeeting():
    """自动嗅探或读取即将举行的赛期与场地"""
    now = getHkTime()
    todayStr = now.strftime("%Y-%m-%d")

    # 1. 优先读取由 fetchRaceCards 生成的 race_meta.json (必须未过期)
    metaPath = os.path.join(baseDir, "data", "race_meta.json")
    if os.path.exists(metaPath):
        try:
            with open(metaPath, "r", encoding="utf-8") as f:
                meta = json.load(f)
                rDate = meta.get("raceDate", "").replace("/", "-")
                venue = meta.get("racecourse", "HV")
                vName = meta.get("venueName", "跑馬地")
                rTime = meta.get("firstRaceTime", "19:10")
                if rDate and rDate >= todayStr:
                    return rDate, venue, vName, rTime
        except Exception:
            pass

    # 2. 从 racecards.json 提取 (必须未过期)
    cardsPath = os.path.join(baseDir, "data", "racecards.json")
    if os.path.exists(cardsPath):
        try:
            with open(cardsPath, "r", encoding="utf-8") as f:
                cards = json.load(f)
                if cards and len(cards) > 0:
                    rDate = cards[0].get("raceDate", "").replace("/", "-")
                    venue = cards[0].get("racecourse", "HV")
                    vName = "跑馬地" if venue == "HV" else "沙田"
                    if rDate and rDate >= todayStr:
                        return rDate, venue, vName, "19:10" if venue == "HV" else "13:00"
        except Exception:
            pass

    # 3. 智能按周推算默认赛期 (周三 HV 夜赛, 周末 ST 日赛)
    weekday = now.weekday()
    if weekday == 2: # 周三比赛日当天
        return now.strftime("%Y-%m-%d"), "HV", "跑馬地", "19:10"
    elif weekday == 6: # 周日比赛日当天
        return now.strftime("%Y-%m-%d"), "ST", "沙田", "13:00"
    elif weekday == 5: # 周六早盘日 -> 锁定明日(周日)沙田赛事
        nextSun = now + timedelta(days=1)
        return nextSun.strftime("%Y-%m-%d"), "ST", "沙田", "13:00"
    elif weekday < 2: # 周一/周二 -> 锁定本周三夜赛
        daysAhead = 2 - weekday
        nextWed = now + timedelta(days=daysAhead)
        return nextWed.strftime("%Y-%m-%d"), "HV", "跑馬地", "19:10"
    else: # 周四/周五 -> 锁定本周末周日日赛
        daysAhead = 6 - weekday
        nextSun = now + timedelta(days=daysAhead)
        return nextSun.strftime("%Y-%m-%d"), "ST", "沙田", "13:00"

def calculateDynamicInterval():
    """根据赛事时间规律自动计算下一次轮询休眠秒数"""
    if scraperConfig["mode"] == "fixed":
        return max(10, int(scraperConfig.get("fixedInterval", 60)))

    now = getHkTime()
    todayStr = now.strftime("%Y-%m-%d")
    hour = now.hour
    minute = now.minute
    targetDate = scraperConfig.get("targetDate") or todayStr
    targetVenue = scraperConfig.get("targetVenue") or "HV"

    isTargetToday = (todayStr == targetDate)

    # 0. 若当天赛事已全数完赛封盘，或深宵时段（23:10 - 08:30），进入深度休眠（1小时），彻底杜绝半夜无意义空转与垃圾数据写入
    if scraperConfig.get("meetingClosed") or (isTargetToday and ((targetVenue == "HV" and (hour >= 23 and minute >= 10)) or (targetVenue == "ST" and (hour >= 18 and minute >= 30)))) or (hour >= 23 or hour < 8):
        return 3600

    # 1. 跑马地夜赛（比赛日当天晚 18:00 - 23:05）
    if isTargetToday and targetVenue == "HV" and 18 <= hour <= 22:
        return 15 # 战时高频 15 秒

    # 2. 沙田日赛（比赛日当天下午 12:00 - 18:20）
    if isTargetToday and targetVenue == "ST" and 12 <= hour <= 17:
        return 15 # 战时高频 15 秒
    if isTargetToday and targetVenue == "ST" and hour == 18 and minute <= 20:
        return 15

    # 3. 赛事日早盘（比赛日当天上午 09:00 - 开赛前夕）
    if isTargetToday and ((targetVenue == "HV" and 9 <= hour < 18) or (targetVenue == "ST" and 9 <= hour < 12)):
        return 120 # 早盘 2 分钟

    # 4. 赛前一日中午早盘初盘窗口 (11:45 - 13:15): 自动加速到 60 秒轮询，精准捕获 12:00 初盘数据
    if 11 <= hour <= 13:
        return 60

    # 5. 非赛马日常规待命（每 30 分钟检查一次）
    return 1800

import gzip

# 马会官方 Whitelisted GraphQL 查询模板（必须保持字符完全一致以通过白名单校验）
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

def normalizeCombination(combString):
    """将马会组合字符串 '07,11' 转换为标准格式 '7-11'"""
    try:
        parts = [int(p.strip()) for p in combString.split(",") if p.strip()]
        parts.sort()
        return f"{parts[0]}-{parts[1]}"
    except Exception:
        return combString.replace(",", "-")

def fetchRaceOddsGql(dateStr, venueCode, raceNo):
    """通过纯标准 HTTP POST 请求官方 GraphQL 接口拉取实盘彩池赔率"""
    payload = {
        "operationName": "racing",
        "query": HKJC_GRAPHQL_QUERY,
        "variables": {
            "date": dateStr,
            "venueCode": venueCode,
            "raceNo": raceNo,
            "oddsTypes": ["WIN", "PLA", "QIN", "QPL"]
        }
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Referer": "https://bet.hkjc.com/",
        "Origin": "https://bet.hkjc.com",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate"
    }
    try:
        req = urllib.request.Request(
            "https://info.cld.hkjc.com/graphql/base/",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=12) as res:
            raw = res.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except Exception as err:
        print(f"[GraphQL Error Race {raceNo}] {err}")
    return None

baseOddsCache = {}

def getBaseOdds(tableName, raceNo, numberKey, oddsColName):
    cacheKey = f"{tableName}_{raceNo}_{numberKey}"
    if cacheKey in baseOddsCache:
        return baseOddsCache[cacheKey]
    try:
        sql = f"SELECT {oddsColName} FROM {tableName} WHERE RaceNo = ? AND Number = ? ORDER BY CollectionDateTime ASC LIMIT 1;"
        row = dbAdapter.queryOne(sql, [raceNo, str(numberKey)])
        if row and row.get(oddsColName) is not None and float(row[oddsColName]) > 0:
            val = float(row[oddsColName])
            baseOddsCache[cacheKey] = val
            return val
    except Exception:
        pass
    return None

def computeOddsDrop(tableName, raceNo, numberKey, currentOdds, oddsColName):
    """自动查询该马匹/组合在当场的初始早盘赔率，计算跌幅（内存高效缓存）"""
    baseOdds = getBaseOdds(tableName, raceNo, numberKey, oddsColName)
    if baseOdds and baseOdds > 0:
        return round(currentOdds - baseOdds, 2)
    return 0.0

wakeUpEvent = threading.Event()

def runScraperCycle():
    """执行一次完整的抓取与入库计算循环 (纯 HTTP 极速模式，适用于云端容器)"""
    nowStr = getHkTime().strftime("%Y-%m-%d %H:%M:%S")
    scraperConfig["lastRunTime"] = nowStr
    scraperConfig["totalRuns"] += 1

    # 自动嗅探目标赛期与场地
    autoDate, autoVenue, autoVName, autoTime = detectNextMeeting()
    targetDate = scraperConfig.get("targetDate") or autoDate
    targetVenue = scraperConfig.get("targetVenue") or autoVenue
    scraperConfig["targetDate"] = targetDate
    scraperConfig["targetVenue"] = targetVenue
    scraperConfig["targetVenueName"] = autoVName
    scraperConfig["firstRaceTime"] = autoTime

    # 若目标赛期更换，重置完赛闭环标记并清空基准缓存
    if targetDate != scraperConfig.get("currentTargetDate"):
        scraperConfig["meetingClosed"] = False
        scraperConfig["currentTargetDate"] = targetDate
        baseOddsCache.clear()

    # 若当天赛事已被判定为全数完赛，直接跳过写库，保护历史时序
    if scraperConfig.get("meetingClosed"):
        scraperConfig["lastStatus"] = f"🏁 完賽封盤: 賽期 [{targetDate} {targetVenue}] 已全數完賽派彩，停止寫庫 ({nowStr})"
        print(f"[{nowStr}] 賽期 [{targetDate} {targetVenue}] 已全數完賽封盤，跳過抓取，保持休眠。")
        return

    print(f"[{nowStr}] 云端采集器启动轮询: 赛期 {targetDate} {targetVenue}...")

    initSqliteWalMode()

    winRows = []
    qinRows = []
    qplRows = []
    anyPoolsFound = False
    allPoolsClosed = True

    try:
        # 抓取 1 至 8 场彩池数据
        for raceNo in range(1, 9):
            res = fetchRaceOddsGql(targetDate, targetVenue, raceNo)
            if not res:
                allPoolsClosed = False
                continue
            rms = res.get("data", {}).get("raceMeetings") or []
            if not rms:
                allPoolsClosed = False
                continue
            pools = rms[0].get("pmPools") or []
            if not pools:
                allPoolsClosed = False
                continue

            anyPoolsFound = True

            for pool in pools:
                sellStatus = str(pool.get("sellStatus") or "")
                status = str(pool.get("status") or "")
                if sellStatus != "STOP_SELL" and status not in ["PAYOUT", "FINAL"]:
                    allPoolsClosed = False

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
                            dropVal = computeOddsDrop("win", raceNo, horseNo, oddsVal, "WinOdds")
                            if dropVal <= -3.0:
                                isHot = 1
                            winRows.append((nowStr, raceNo, horseNo, oddsVal, 0, dropVal, isHot, willPay))
                        except Exception:
                            pass

                elif oddsType == "QIN":
                    for node in oddsNodes:
                        try:
                            combStr = normalizeCombination(node.get("combString", ""))
                            oddsVal = float(node.get("oddsValue") or 0.0)
                            isHot = 1 if node.get("hotFavourite") else 0
                            willPay = str(int(oddsVal * 1000)) if oddsVal > 0 else "0"
                            dropVal = computeOddsDrop("qin", raceNo, combStr, oddsVal, "QinOdds")
                            qinRows.append((nowStr, raceNo, combStr, oddsVal, 0, dropVal, isHot, willPay))
                        except Exception:
                            pass

                elif oddsType == "QPL":
                    for node in oddsNodes:
                        try:
                            combStr = normalizeCombination(node.get("combString", ""))
                            oddsVal = float(node.get("oddsValue") or 0.0)
                            isHot = 1 if node.get("hotFavourite") else 0
                            willPay = str(int(oddsVal * 1000)) if oddsVal > 0 else "0"
                            dropVal = computeOddsDrop("qpl", raceNo, combStr, oddsVal, "QplOdds")
                            qplRows.append((nowStr, raceNo, combStr, oddsVal, 0, dropVal, isHot, willPay))
                        except Exception:
                            pass

        # 完赛检测与终盘去重守护
        if anyPoolsFound and allPoolsClosed:
            scraperConfig["meetingClosed"] = True
            closingTimeStr = f"{targetDate} 23:00:00" if targetVenue == "HV" else f"{targetDate} 18:15:00"
            r = dbAdapter.queryOne("SELECT 1 as found FROM win WHERE CollectionDateTime = ? LIMIT 1;", [closingTimeStr])
            if r and r.get("found"):
                print(f"[{nowStr}] 賽期 [{targetDate} {targetVenue}] 全天賽事已全數完賽派彩，終盤已封存在庫，停止寫入。")
                scraperConfig["lastStatus"] = f"🏁 完賽封盤: 全天賽事已完結派彩，停止入庫 [{targetDate}] ({nowStr})"
                return
            else:
                nowStr = closingTimeStr
                winRows = [(nowStr, r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in winRows]
                qinRows = [(nowStr, r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in qinRows]
                qplRows = [(nowStr, r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in qplRows]
                print(f"[{nowStr}] 偵測到全天賽事已完賽派彩，入庫官方終盤收官記錄: {closingTimeStr}")

        if winRows:
            dbAdapter.executeMany("""
                INSERT INTO win (CollectionDateTime, RaceNo, Number, WinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, winRows)
        if qinRows:
            dbAdapter.executeMany("""
                INSERT INTO qin (CollectionDateTime, RaceNo, Number, QinOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, qinRows)
        if qplRows:
            dbAdapter.executeMany("""
                INSERT INTO qpl (CollectionDateTime, RaceNo, Number, QplOdds, Scratched, OddsDrop, Hot, WillPay)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, qplRows)
        totalSaved = len(winRows) + len(qinRows) + len(qplRows)
        scraperConfig["totalRecordsSaved"] += totalSaved

        if totalSaved > 0:
            scraperConfig["lastStatus"] = f"🟢 實時採集中: 成功入庫 {totalSaved} 條賠率 [{targetDate} {targetVenue}] ({nowStr})"
            print(f"[{nowStr}] 入库成功: {totalSaved} 条 (WIN:{len(winRows)}, QIN:{len(qinRows)}, QPL:{len(qplRows)})")
        else:
            scraperConfig["lastStatus"] = f"🟡 待命中: 目標賽期 [{targetDate} {autoVName}] 彩池尚未開盤/非賽馬時段 ({nowStr})"
            print(f"[{nowStr}] 彩池暂未开盘或等待中。")
    except Exception as err:
        scraperConfig["lastStatus"] = f"🔴 異常: {str(err)}"
        print(f"[Scraper Error] {err}")
        conn.rollback()
    finally:
        conn.close()

def autoScraperLoop():
    """主抓取守护循环"""
    print("[Scraper Daemon] Auto-scraper background thread started.")
    time.sleep(3) # 启动缓冲 3 秒

    while True:
        try:
            if scraperConfig.get("enabled", True):
                runScraperCycle()
        except Exception as loopErr:
            scraperConfig["lastStatus"] = f"Exception: {str(loopErr)}"

        nextInterval = calculateDynamicInterval()
        scraperConfig["currentInterval"] = nextInterval
        # 使用 Event.wait 支持即时唤醒
        wakeUpEvent.wait(timeout=nextInterval)
        wakeUpEvent.clear()

def keepAliveLoop():
    """Render 容器 7×24 小时防休眠保活守护循环"""
    print("[KeepAlive Daemon] Anti-sleep ping thread started.")
    time.sleep(30) # 启动等待 30 秒

    while True:
        try:
            port = int(os.environ.get("PORT", 5000))
            pingUrl = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(pingUrl, headers={"User-Agent": "hsjc-KeepAlive/2.0"})
            with urllib.request.urlopen(req, timeout=5) as res:
                pass
        except Exception:
            pass
        time.sleep(600) # 每 10 分钟 ping 一次，保持容器常驻热备

def startBackgroundWorkers():
    """在 Web 主服务启动时拉起后台双守护线程"""
    initSqliteWalMode()

    scraperThread = threading.Thread(target=autoScraperLoop, daemon=True, name="HsjcScraperWorker")
    scraperThread.start()

    keepAliveThread = threading.Thread(target=keepAliveLoop, daemon=True, name="HsjcKeepAliveWorker")
    keepAliveThread.start()

    print("[Background Workers] Scraper & KeepAlive threads launched successfully.")

def getScraperStatus():
    """获取当前採集器運行指標"""
    return scraperConfig

def updateScraperConfig(mode=None, interval=None, enabled=None, targetDate=None, targetVenue=None):
    """動態調整採集配置"""
    if mode in ["smart", "fixed"]:
        scraperConfig["mode"] = mode
    if interval is not None and int(interval) >= 5:
        scraperConfig["fixedInterval"] = int(interval)
        if scraperConfig["mode"] == "fixed":
            scraperConfig["currentInterval"] = int(interval)
    if enabled is not None:
        scraperConfig["enabled"] = bool(enabled)
    if targetDate:
        scraperConfig["targetDate"] = targetDate
    if targetVenue:
        scraperConfig["targetVenue"] = targetVenue
    wakeUpEvent.set() # 立即唤醒应用新配置
    return scraperConfig
