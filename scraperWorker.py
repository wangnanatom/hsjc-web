import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
from datetime import datetime

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
    "totalRecordsSaved": 0
}

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
        print("[WAL] SQLite initialized in WAL mode successfully.")
    except Exception as err:
        print("[WAL Error] Failed to set WAL mode:", err)

def calculateDynamicInterval():
    """根据赛事时间规律自动计算下一次轮询休眠秒数"""
    if scraperConfig["mode"] == "fixed":
        return max(10, int(scraperConfig.get("fixedInterval", 60)))

    now = datetime.now()
    weekday = now.weekday() # 0=周一, 2=周三, 5=周六, 6=周日
    hour = now.hour

    # 1. 跑马地夜赛（周三晚 18:00 - 23:30）
    if weekday == 2 and 18 <= hour <= 23:
        return 15 # 战时高频 15 秒

    # 2. 沙田日赛（周日/周六下午 12:00 - 18:30）
    if (weekday == 6 or weekday == 5) and 12 <= hour <= 18:
        return 15 # 战时高频 15 秒

    # 3. 赛事日早盘（周三白天 10:00-18:00，周日早晨 09:00-12:00）
    if (weekday == 2 and 10 <= hour < 18) or (weekday == 6 and 9 <= hour < 12):
        return 180 # 早盘 3 分钟

    # 4. 非赛马平时
    return 1800 # 30 分钟休眠节能

def fetchHkjcOddsJson(poolType="winplaodds", raceDate=None, venue="ST", raceNo="ALL"):
    """拉取香港赛马会官方公开赔率流"""
    if not raceDate:
        raceDate = datetime.now().strftime("%Y-%m-%d")

    targetUrl = f"https://bet.hkjc.com/racing/getJSON.aspx?type={poolType}&date={raceDate}&venue={venue}&raceno={raceNo}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://bet.hkjc.com/racing/pages/odds_wp.aspx"
    }

    try:
        req = urllib.request.Request(targetUrl, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as res:
            if res.status == 200:
                rawBytes = res.read()
                rawText = rawBytes.decode("utf-8", errors="ignore").strip()
                if rawText.startswith("{") or rawText.startswith("["):
                    return json.loads(rawText)
    except Exception as err:
        pass
    return None

def computeOddsDrop(conn, tableName, raceNo, numberKey, currentOdds, oddsColName):
    """自动查询该马匹/组合在当场的初始早盘赔率，计算跌幅"""
    try:
        cursor = conn.cursor()
        sql = f"SELECT {oddsColName} FROM {tableName} WHERE RaceNo = ? AND Number = ? ORDER BY CollectionDateTime ASC LIMIT 1;"
        cursor.execute(sql, (raceNo, numberKey))
        row = cursor.fetchone()
        if row and row[0] and float(row[0]) > 0:
            baseOdds = float(row[0])
            drop = round(currentOdds - baseOdds, 2)
            return drop
    except Exception:
        pass
    return 0.0

def saveOddsBatch(oddsPayload):
    """解析并批量保存 WIN, PLA, QIN, QPL 赔率"""
    if not oddsPayload or not isinstance(oddsPayload, dict):
        return 0

    savedCount = 0
    collectTime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn = sqlite3.connect(dbPath, timeout=10.0)
        cursor = conn.cursor()

        # 1. 解析 WIN / PLA
        races = oddsPayload.get("RACES") or oddsPayload.get("races") or []
        for raceItem in races:
            raceNo = int(raceItem.get("RACENO") or raceItem.get("raceNo") or 1)
            
            # 解析 WIN
            winList = raceItem.get("WIN") or raceItem.get("win") or []
            for w in winList:
                horseNo = int(w.get("NUM") or w.get("num") or 0)
                if horseNo <= 0:
                    continue
                winOdds = float(w.get("ODDS") or w.get("odds") or 0.0)
                scratched = 1 if str(w.get("SCR", "0")) == "1" else 0
                dropVal = computeOddsDrop(conn, "win", raceNo, horseNo, winOdds, "WinOdds")
                hotVal = 1 if dropVal <= -3.0 or (winOdds > 0 and winOdds <= 3.0) else 0
                willPay = str(int(winOdds * 1000)) if winOdds > 0 else "0"

                cursor.execute("""
                    INSERT INTO win (CollectionDateTime, RaceNo, Number, WinOdds, Scratched, OddsDrop, Hot, WillPay)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (collectTime, raceNo, horseNo, winOdds, scratched, dropVal, hotVal, willPay))
                savedCount += 1

            # 解析 QIN / QPL
            qinList = raceItem.get("QIN") or raceItem.get("qin") or []
            for q in qinList:
                comb = str(q.get("COMB") or q.get("comb") or q.get("NUM") or "")
                if not comb:
                    continue
                qinOdds = float(q.get("ODDS") or q.get("odds") or 0.0)
                dropVal = computeOddsDrop(conn, "qin", raceNo, comb, qinOdds, "QinOdds")
                hotVal = 1 if dropVal <= -5.0 or (qinOdds > 0 and qinOdds <= 6.0) else 0

                cursor.execute("""
                    INSERT INTO qin (CollectionDateTime, RaceNo, Number, QinOdds, Scratched, OddsDrop, Hot, WillPay)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (collectTime, raceNo, comb, qinOdds, 0, dropVal, hotVal, "0"))
                savedCount += 1

        conn.commit()
        conn.close()
    except Exception as err:
        print("[Save Error] Failed to save odds batch:", err)

    return savedCount

wakeUpEvent = threading.Event()

def runScraperCycle():
    """执行一次完整的抓取与计算循环"""
    nowStr = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scraperConfig["lastRunTime"] = nowStr
    scraperConfig["totalRuns"] += 1

    try:
        # 尝试抓取 WIN/PLA
        winData = fetchHkjcOddsJson("winplaodds")
        savedWin = saveOddsBatch(winData)

        # 尝试抓取 QIN
        qinData = fetchHkjcOddsJson("qin")
        savedQin = saveOddsBatch(qinData)

        totalSaved = savedWin + savedQin
        scraperConfig["totalRecordsSaved"] += totalSaved
        if totalSaved > 0:
            scraperConfig["lastStatus"] = f"🟢 實時採集中: 成功入庫 {totalSaved} 條賠率 ({nowStr})"
        else:
            scraperConfig["lastStatus"] = f"🟡 待命中: 馬會彩池未開盤/非賽馬時段，等待開盤 ({nowStr})"
    except Exception as err:
        scraperConfig["lastStatus"] = f"🔴 異常: {str(err)}"

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

def updateScraperConfig(mode=None, interval=None, enabled=None):
    """動態調整採集配置"""
    if mode in ["smart", "fixed"]:
        scraperConfig["mode"] = mode
    if interval is not None and int(interval) >= 5:
        scraperConfig["fixedInterval"] = int(interval)
        if scraperConfig["mode"] == "fixed":
            scraperConfig["currentInterval"] = int(interval)
    if enabled is not None:
        scraperConfig["enabled"] = bool(enabled)
    wakeUpEvent.set() # 立即唤醒应用新配置
    return scraperConfig
