import http.server
import socketserver
import json
import sqlite3
import os
import sys
import threading
from collections import deque
import urllib.parse
from datetime import datetime

# 确保控制台 UTF-8 输出且行缓冲实时刷新
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
from scraperWorker import startBackgroundWorkers, getScraperStatus, updateScraperConfig, runScraperCycle
import dbAdapter

baseDir = os.path.dirname(os.path.abspath(__file__))
dbPath = os.path.join(baseDir, "hsjc.db")
visitorLogPath = os.path.join(baseDir, "data", "visitor_analytics.json")
portNumber = int(os.environ.get("PORT", 5050))

class VisitorTracker:
    def __init__(self, dataFilePath=None):
        self.lock = threading.Lock()
        self.dataFilePath = dataFilePath
        self.sessions = {}
        self.logs = deque(maxlen=500)
        self.todayDate = datetime.now().strftime("%Y-%m-%d")
        self.loadFromFile()

    def loadFromFile(self):
        if self.dataFilePath and os.path.exists(self.dataFilePath):
            try:
                with open(self.dataFilePath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.sessions = data.get("sessions", {})
                    for ip, s in self.sessions.items():
                        try:
                            s["lastSeenDt"] = datetime.strptime(s["lastSeen"], "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            s["lastSeenDt"] = datetime.now()
                    rawLogs = data.get("logs", [])
                    self.logs = deque(rawLogs[:500], maxlen=500)
            except Exception as e:
                print("[VisitorTracker] 加載歷史訪客日誌異常:", e)

    def saveToFile(self):
        if not self.dataFilePath:
            return
        try:
            serializableSessions = {}
            for ip, s in self.sessions.items():
                copyS = dict(s)
                copyS.pop("lastSeenDt", None)
                serializableSessions[ip] = copyS
            payload = {
                "sessions": serializableSessions,
                "logs": list(self.logs)[:200]
            }
            tmpPath = self.dataFilePath + ".tmp"
            with open(tmpPath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(self.dataFilePath):
                os.remove(self.dataFilePath)
            os.rename(tmpPath, self.dataFilePath)
        except Exception:
            pass

    def parseDevice(self, userAgent):
        ua = (userAgent or "").lower()
        if "iphone" in ua:
            return "iPhone (iOS)"
        elif "ipad" in ua:
            return "iPad (iOS)"
        elif "android" in ua:
            return "Android 手機"
        elif "windows" in ua:
            return "Windows PC"
        elif "macintosh" in ua or "mac os" in ua:
            return "Mac 電腦"
        elif "linux" in ua:
            return "Linux 設備"
        elif any(k in ua for k in ["curl", "python", "render", "uptime", "bot", "spider"]):
            return "探針 / 爬蟲"
        return "通用瀏覽器"

    def recordVisit(self, clientIp, userAgent, path, queryParams):
        if path in ["/health", "/favicon.ico", "/api/analytics/stats"]:
            return

        now = datetime.now()
        nowStr = now.strftime("%Y-%m-%d %H:%M:%S")
        todayStr = now.strftime("%Y-%m-%d")
        device = self.parseDevice(userAgent)

        action = ""
        isBackgroundPoll = False
        if path in ["/", "/index.html"]:
            action = "打開戰術看板首頁"
        elif path == "/api/compare":
            pool = queryParams.get("pool", ["WIN"])[0]
            raceNo = queryParams.get("raceNo", ["1"])[0]
            action = f"雙時刻落飛比對 第{raceNo}場 [{pool}]"
        elif path == "/api/matrix":
            pool = queryParams.get("pool", ["QIN"])[0]
            raceNo = queryParams.get("raceNo", ["1"])[0]
            action = f"查閱2D組合矩陣 第{raceNo}場 [{pool}]"
        elif path == "/api/results":
            action = "查看賽事歷史復盤"
        elif path == "/api/racecards":
            action = "查看最新排位表"
        elif path == "/api/timestamps":
            isBackgroundPoll = True
            action = "定時盤口同步"
        elif path.startswith("/api/scraper"):
            action = f"採集器控制: {path.replace('/api/scraper/', '')}"
        else:
            action = f"訪問 {path}"

        with self.lock:
            if clientIp not in self.sessions:
                self.sessions[clientIp] = {
                    "ip": clientIp,
                    "device": device,
                    "firstSeen": nowStr,
                    "lastSeen": nowStr,
                    "lastSeenDt": now,
                    "hits": 1,
                    "lastAction": action
                }
            else:
                s = self.sessions[clientIp]
                s["lastSeen"] = nowStr
                s["lastSeenDt"] = now
                s["hits"] = s.get("hits", 0) + 1
                if not isBackgroundPoll or s.get("lastAction") == "定時盤口同步":
                    s["lastAction"] = action
                if s.get("device") in ["通用瀏覽器", "未知"] and device not in ["通用瀏覽器", "未知"]:
                    s["device"] = device

            if not isBackgroundPoll:
                self.logs.appendleft({
                    "timestamp": nowStr,
                    "ip": clientIp,
                    "device": device,
                    "action": action,
                    "path": path
                })
                self.saveToFile()

    def getStats(self):
        now = datetime.now()
        nowStr = now.strftime("%Y-%m-%d %H:%M:%S")
        todayStr = now.strftime("%Y-%m-%d")

        with self.lock:
            activeCount = 0
            todayUvCount = 0
            todayPvCount = 0
            visitorList = []

            for ip, s in self.sessions.items():
                lastSeenDt = s.get("lastSeenDt")
                if not isinstance(lastSeenDt, datetime):
                    try:
                        lastSeenDt = datetime.strptime(s.get("lastSeen", ""), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        lastSeenDt = now
                    s["lastSeenDt"] = lastSeenDt

                diffSec = (now - lastSeenDt).total_seconds()
                isActive = (diffSec <= 300)
                if isActive:
                    activeCount += 1

                if s.get("lastSeen", "").startswith(todayStr):
                    todayUvCount += 1
                    todayPvCount += s.get("hits", 0)

                visitorList.append({
                    "ip": ip,
                    "device": s.get("device", "未知"),
                    "firstSeen": s.get("firstSeen", nowStr),
                    "lastSeen": s.get("lastSeen", nowStr),
                    "hits": s.get("hits", 0),
                    "isActive": isActive,
                    "lastAction": s.get("lastAction", "-")
                })

            visitorList.sort(key=lambda x: x["lastSeen"], reverse=True)
            logList = list(self.logs)[:100]

            return {
                "serverTime": nowStr,
                "activeUsersCount": activeCount,
                "todayUniqueVisitors": todayUvCount,
                "todayPageViews": todayPvCount,
                "totalTrackedNodes": len(self.sessions),
                "visitors": visitorList,
                "recentLogs": logList
            }

visitorTracker = VisitorTracker(visitorLogPath)

def queryDistinctTimestamps():
    try:
        rows = dbAdapter.queryAll("SELECT DISTINCT CollectionDateTime FROM win ORDER BY CollectionDateTime DESC LIMIT 500;")
        return [r["CollectionDateTime"] for r in rows if r.get("CollectionDateTime")]
    except Exception as err:
        print("Error querying timestamps:", err)
        return []

def queryCompareData(pool="WIN", raceNo=1, time1=None, time2=None):
    compareList = []
    topDrops = []
    tableName = "win"
    oddsCol = "WinOdds"
    
    if pool.upper() == "PLA":
        tableName = "pla"
        oddsCol = "WinOdds"
    elif pool.upper() == "QIN":
        tableName = "qin"
        oddsCol = "QinOdds"
    elif pool.upper() == "QPL":
        tableName = "qpl"
        oddsCol = "QplOdds"

    try:
        time1 = (time1 or "").strip()
        time2 = (time2 or "").strip()
        if not time1 or not time2 or time1 == "undefined" or time2 == "undefined":
            recentRows = dbAdapter.queryAll(f"SELECT DISTINCT CollectionDateTime FROM {tableName} ORDER BY CollectionDateTime DESC LIMIT 2;")
            if len(recentRows) >= 2:
                time2 = recentRows[0]["CollectionDateTime"]
                time1 = recentRows[1]["CollectionDateTime"]
            elif len(recentRows) == 1:
                time2 = recentRows[0]["CollectionDateTime"]
                time1 = recentRows[0]["CollectionDateTime"]
            else:
                time1 = "2026-09-06 11:30:00"
                time2 = "2026-09-06 18:00:00"
        
        # Query Time 1
        sql1 = f"SELECT Number, {oddsCol}, Scratched, Hot FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
        rows1 = dbAdapter.queryAll(sql1, [time1, raceNo])
        t1Dict = {str(r["Number"]): {"odds": float(r[oddsCol]) if r.get(oddsCol) is not None else 0.0, "scratched": r.get("Scratched", 0), "hot": r.get("Hot", 0)} for r in rows1}

        # Query Time 2
        sql2 = f"SELECT Number, {oddsCol}, Scratched, Hot FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
        rows2 = dbAdapter.queryAll(sql2, [time2, raceNo])
        t2Dict = {str(r["Number"]): {"odds": float(r[oddsCol]) if r.get(oddsCol) is not None else 0.0, "scratched": r.get("Scratched", 0), "hot": r.get("Hot", 0)} for r in rows2}

        # Merge and compute differences
        allKeys = sorted(list(set(t1Dict.keys()) | set(t2Dict.keys())), key=lambda x: int(x.split('-')[0]) if '-' in x or x.isdigit() else x)
        for k in allKeys:
            o1 = t1Dict.get(k, {}).get("odds", 0.0)
            o2 = t2Dict.get(k, {}).get("odds", 0.0)
            diff = round(o2 - o1, 2)
            percentChange = round(((o2 - o1) / o1 * 100), 1) if o1 > 0 else 0.0
            scratched = t2Dict.get(k, {}).get("scratched", 0)
            hot = t2Dict.get(k, {}).get("hot", 0)

            status = "平穩"
            if percentChange <= -15:
                status = "大戶狂砸 (暴跌)"
            elif percentChange < -5:
                status = "資金流入 (落飛)"
            elif percentChange >= 15:
                status = "大戶撤資 (暴升)"
            elif percentChange > 5:
                status = "資金流出 (回飛)"

            item = {
                "number": k,
                "time1Odds": o1,
                "time2Odds": o2,
                "diff": diff,
                "percentChange": percentChange,
                "status": status,
                "scratched": scratched,
                "hot": hot
            }
            compareList.append(item)
            if percentChange < 0 and o1 > 0:
                topDrops.append(item)

        topDrops.sort(key=lambda x: x["percentChange"])
    except Exception as err:
        print("Error in queryCompareData:", err)

    return {
        "pool": pool,
        "raceNo": raceNo,
        "time1": time1,
        "time2": time2,
        "items": compareList,
        "topDrops": topDrops[:5]
    }

def queryMatrixData(pool="QIN", raceNo=1, timeStr=None):
    tableName = "qin" if pool.upper() == "QIN" else "qpl"
    oddsCol = "QinOdds" if pool.upper() == "QIN" else "QplOdds"
    matrixData = {}
    try:
        timeStr = (timeStr or "").strip()
        if not timeStr or timeStr == "undefined":
            r = dbAdapter.queryOne(f"SELECT DISTINCT CollectionDateTime FROM {tableName} ORDER BY CollectionDateTime DESC LIMIT 1;")
            if r and r.get("CollectionDateTime"):
                timeStr = r["CollectionDateTime"]

        if timeStr:
            sql = f"SELECT Number, {oddsCol}, OddsDrop FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
            rows = dbAdapter.queryAll(sql, [timeStr, raceNo])
            for row in rows:
                numStr = str(row.get("Number"))
                oddsVal = float(row[oddsCol]) if row.get(oddsCol) is not None else 0.0
                matrixData[numStr] = {
                    "odds": oddsVal,
                    "oddsDrop": float(row["OddsDrop"]) if row.get("OddsDrop") is not None else 0.0
                }
    except Exception as err:
        print("Error querying matrix data:", err)
    return {"time": timeStr, "raceNo": raceNo, "pool": pool, "matrix": matrixData}

def querySeasonResults():
    jsonPath = os.path.join(baseDir, "data", "results.json")
    if os.path.exists(jsonPath):
        with open(jsonPath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def queryRaceCards():
    jsonPath = os.path.join(baseDir, "data", "racecards.json")
    if os.path.exists(jsonPath):
        with open(jsonPath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

htmlTemplate = """<!DOCTYPE html>
<html lang="zh-HK">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>hsjc 2.0 賽馬實戰雲端戰術看板 (官方旗艦版)</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
        .matrix-cell { transition: all 0.15s ease-in-out; }
        .matrix-cell:hover { background-color: #2563eb !important; color: white !important; font-weight: bold; transform: scale(1.08); z-index: 20; cursor: pointer; }
        .badge-drop { background-color: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
        .badge-rise { background-color: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    </style>
</head>
<body class="min-h-screen flex flex-col">
    <header class="bg-slate-900/90 backdrop-blur border-b border-slate-800 px-6 py-3.5 flex flex-wrap justify-between items-center sticky top-0 z-50">
        <div class="flex items-center space-x-3">
            <div class="w-10 h-10 bg-gradient-to-br from-amber-500 via-red-500 to-rose-600 rounded-xl flex items-center justify-center font-black text-xl text-white shadow-lg shadow-rose-950/40">🏇</div>
            <div>
                <div class="flex items-center space-x-2">
                    <h1 class="text-xl font-black tracking-wider text-white">hsjc 2.0</h1>
                    <span class="text-[11px] font-bold px-2 py-0.5 bg-emerald-950 text-emerald-400 border border-emerald-700/60 rounded-full flex items-center">
                        <span class="w-1.5 h-1.5 bg-emerald-400 rounded-full animate-pulse mr-1.5"></span>雲端專線 24/7 在線
                    </span>
                    <span id="dbStatusBadge" class="text-[11px] font-bold px-2 py-0.5 bg-slate-800 text-slate-400 border border-slate-700 rounded-full flex items-center">
                        <span class="w-1.5 h-1.5 bg-slate-500 rounded-full mr-1.5"></span>資料庫偵測中...
                    </span>
                    <button onclick="openAnalyticsModal()" id="visitorStatsBadge" class="text-[11px] font-bold px-2.5 py-0.5 bg-indigo-950/80 text-indigo-300 border border-indigo-700/60 rounded-full flex items-center hover:bg-indigo-900 transition shadow-sm cursor-pointer" title="點擊查看訪客審計與在線監控">
                        <span class="w-1.5 h-1.5 bg-indigo-400 rounded-full animate-pulse mr-1.5"></span>
                        <span id="visitorCountText">訪客: 載入中...</span>
                    </button>
                </div>
                <p class="text-xs text-slate-400">香港職業賽馬大戶暗盤資金流向 · 2D 矩陣深度監控終端</p>
            </div>
        </div>

        <div class="flex items-center space-x-2 mt-2 sm:mt-0">
            <button onclick="switchTab('compareTab')" id="btnCompare" class="tab-btn px-4 py-2 rounded-lg text-sm font-bold bg-blue-600 text-white shadow-md shadow-blue-900/40">雙時刻落飛比對 (ResultForm)</button>
            <button onclick="switchTab('matrixTab')" id="btnMatrix" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">2D 組合矩陣 (CompareForm)</button>
            <button onclick="switchTab('resultsTab')" id="btnResults" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">賽事結果復盤</button>
            <button onclick="switchTab('racecardTab')" id="btnRacecard" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">最新排位表 (RaceCard)</button>
            <button onclick="openScraperModal()" class="px-3.5 py-2 rounded-lg text-sm font-bold bg-gradient-to-r from-amber-600 via-orange-600 to-rose-600 hover:from-amber-500 hover:to-rose-500 text-white shadow-md shadow-rose-950/40 flex items-center transition cursor-pointer">
                <span class="mr-1.5">⚙️</span>採集控制 & 即時抓取
            </button>
        </div>
    </header>

    <main class="flex-1 p-4 sm:p-6 max-w-7xl mx-auto w-full space-y-6">
        <section id="compareTab" class="tab-content">
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-xl mb-4">
                <div class="flex items-center space-x-3">
                    <div id="liveStatusBadge" class="text-xs bg-emerald-950/60 border border-emerald-500/40 text-emerald-300 px-3 py-1 rounded-full flex items-center shadow-sm">
                        <span class="w-2 h-2 bg-emerald-400 rounded-full animate-pulse mr-2"></span>
                        🟢 實盤全自動同步中 · 15秒輪詢
                    </div>
                </div>
                <div class="flex flex-wrap items-center justify-between gap-3 mt-3 pt-3 border-t border-slate-800/80">
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="text-xs font-bold text-slate-400 mr-1 flex items-center">
                            <svg class="w-3.5 h-3.5 text-amber-400 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                            戰術快捷比對:
                        </span>
                        <button type="button" onclick="applyTacticPreset('5min')" class="preset-btn px-2.5 py-1 bg-slate-800 hover:bg-emerald-950/60 hover:border-emerald-500/50 border border-slate-700 rounded-lg text-xs font-bold text-emerald-400 transition shadow-sm flex items-center">
                            🔥 5分鐘衝刺 (最新 vs 5分前)
                        </button>
                        <button type="button" onclick="applyTacticPreset('15min')" class="preset-btn px-2.5 py-1 bg-slate-800 hover:bg-emerald-950/60 hover:border-emerald-500/50 border border-slate-700 rounded-lg text-xs font-bold text-emerald-400 transition shadow-sm flex items-center">
                            🚨 15分鐘大戶入場 (最新 vs 15分前)
                        </button>
                        <button type="button" onclick="applyTacticPreset('early')" class="preset-btn px-2.5 py-1 bg-slate-800 hover:bg-blue-950/60 hover:border-blue-500/50 border border-slate-700 rounded-lg text-xs font-bold text-blue-400 transition shadow-sm flex items-center">
                            📊 今日全盤走勢 (最新 vs 今日早盤)
                        </button>
                        <button type="button" onclick="applyTacticPreset('settle')" class="preset-btn px-2.5 py-1 bg-slate-800 hover:bg-purple-950/60 hover:border-purple-500/50 border border-slate-700 rounded-lg text-xs font-bold text-purple-300 transition shadow-sm flex items-center">
                            🏁 終盤收官復盤 (封盤終點 vs 開盤初盤)
                        </button>
                    </div>
                </div>
                <div class="flex flex-wrap items-center justify-between gap-4 mt-3 pt-3 border-t border-slate-800/80">
                    <div class="flex flex-wrap items-center gap-3">
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">賽事日期 (Date)</label>
                            <select id="compDateSelect" onchange="onDateChange()" class="bg-slate-800 border border-slate-700 text-sm font-bold text-emerald-400 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                            </select>
                        </div>
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">玩法選擇</label>
                            <select id="compPool" onchange="loadCompareData()" class="bg-slate-800 border border-slate-700 text-sm font-bold text-amber-400 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                                <option value="WIN">獨贏 (WIN)</option>
                                <option value="PLA">位置 (PLA)</option>
                                <option value="QIN">連贏 (QIN)</option>
                                <option value="QPL">位置Q (QPL)</option>
                            </select>
                        </div>
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">場次 (Race)</label>
                            <select id="compRaceNo" onchange="onRaceChange()" class="bg-slate-800 border border-slate-700 text-sm font-bold text-white rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                                <option value="1">第 1 場</option>
                                <option value="2">第 2 場</option>
                                <option value="3">第 3 場</option>
                                <option value="4">第 4 場</option>
                                <option value="5">第 5 場</option>
                                <option value="6">第 6 場</option>
                                <option value="7">第 7 場</option>
                                <option value="8">第 8 場</option>
                                <option value="9">第 9 場</option>
                                <option value="10">第 10 場</option>
                                <option value="11">第 11 場</option>
                                <option value="12">第 12 場</option>
                            </select>
                        </div>
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">基準時刻 (Time 1)</label>
                            <select id="compTime1" onchange="loadCompareData()" class="bg-slate-800 border border-slate-700 text-xs text-slate-300 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500 max-w-[230px]">
                            </select>
                        </div>
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">臨場時刻 (Time 2)</label>
                            <select id="compTime2" onchange="loadCompareData()" class="bg-slate-800 border border-slate-700 text-xs text-slate-300 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500 max-w-[230px]">
                            </select>
                        </div>
                    </div>
                    <button onclick="loadCompareData()" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-bold shadow-lg shadow-blue-900/40 flex items-center">
                        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                        立即刷新比對
                    </button>
                </div>
            </div>

            <div id="topDropSection" class="mb-4">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4" id="topDropCards">
                </div>
            </div>

            <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-xl">
                <div class="flex justify-between items-center mb-3">
                    <h2 class="text-base font-bold text-white flex items-center">
                        <span class="w-2.5 h-2.5 bg-emerald-500 rounded-full mr-2"></span>
                        雙時刻時序賠率差值與資金異動分析表
                    </h2>
                    <span class="text-xs text-slate-400">
                        <span class="inline-block w-2.5 h-2.5 rounded-full border border-emerald-500 bg-emerald-500/20 mr-1"></span>綠燈: 大戶砸盤落飛 (暴跌)
                        <span class="inline-block w-2.5 h-2.5 rounded-full border border-red-500 bg-red-500/20 ml-3 mr-1"></span>紅燈: 資金撤出回飛 (暴升)
                    </span>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm font-mono">
                        <thead class="bg-slate-800/80 text-slate-300 text-xs font-sans">
                            <tr>
                                <th class="py-2.5 px-3">馬號 / 組合</th>
                                <th class="py-2.5 px-3">熱門狀態</th>
                                <th class="py-2.5 px-3">基準賠率 (T1)</th>
                                <th class="py-2.5 px-3">臨場賠率 (T2)</th>
                                <th class="py-2.5 px-3">賠率差值 (DIFF)</th>
                                <th class="py-2.5 px-3">漲跌百分比 (%)</th>
                                <th class="py-2.5 px-3 text-right">大戶資金動向狀態</th>
                            </tr>
                        </thead>
                        <tbody id="compareTableBody" class="divide-y divide-slate-800">
                        </tbody>
                    </table>
                </div>
            </div>
        </section>

        <section id="matrixTab" class="tab-content hidden">
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-xl">
                <div class="flex flex-wrap justify-between items-center mb-4 pb-3 border-b border-slate-800">
                    <div class="flex items-center space-x-3">
                        <h2 class="text-lg font-bold text-white flex items-center">
                            <span class="w-3 h-3 bg-amber-500 rounded-full mr-2"></span>
                            2D 組合玩法交互式二維網格矩陣
                        </h2>
                        <span id="matrixCountBadge" class="text-xs px-2.5 py-0.5 rounded-full bg-slate-800 border border-slate-700 text-emerald-400 font-semibold">
                            自適應網格展開 · 全量組合在庫
                        </span>
                    </div>
                    <div class="flex flex-wrap items-center gap-3 mt-2 sm:mt-0">
                        <select id="matrixPool" onchange="loadMatrixData()" class="bg-slate-800 border border-slate-700 text-xs font-bold text-amber-400 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                            <option value="QIN">連贏 (QIN) 矩陣</option>
                            <option value="QPL">位置Q (QPL) 矩陣</option>
                        </select>
                        <select id="matrixRaceNo" onchange="loadMatrixData()" class="bg-slate-800 border border-slate-700 text-xs font-bold text-white rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                            <option value="1">第 1 場 (14匹·91組)</option>
                            <option value="2">第 2 場 (11匹·55組)</option>
                            <option value="3">第 3 場 (特首盃 6匹·15組)</option>
                            <option value="4">第 4 場 (14匹·91組)</option>
                            <option value="5">第 5 場 (14匹·91組)</option>
                            <option value="6">第 6 場 (14匹·91組)</option>
                            <option value="7">第 7 場 (9匹·36組)</option>
                            <option value="8">第 8 場 (14匹·91組)</option>
                            <option value="9">第 9 場</option>
                            <option value="10">第 10 場</option>
                            <option value="11">第 11 場</option>
                            <option value="12">第 12 場</option>
                        </select>
                        <select id="matrixTime" onchange="loadMatrixData()" class="bg-slate-800 border border-slate-700 text-xs text-slate-300 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                            <option value="2026-09-06 18:00:00">2026-09-06 18:00 (臨場終盤 / 狂砸落飛)</option>
                            <option value="2026-09-06 11:30:00">2026-09-06 11:30 (早盤初盤基準)</option>
                            <option value="2022-09-18 04:15:00">2022-09-18 04:15 (歷史真實大戶庫)</option>
                        </select>
                        <button onclick="loadMatrixData()" class="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-bold shadow-md shadow-blue-900/30">刷新矩陣</button>
                    </div>
                </div>

                <div class="overflow-x-auto">
                    <div class="inline-block min-w-full">
                        <table id="matrixTable" class="w-full border-collapse text-xs text-center">
                        </table>
                    </div>
                </div>

                <div id="matrixDetailCard" class="mt-4 p-4 rounded-xl bg-slate-800/80 border border-slate-700 flex justify-between items-center hidden">
                    <div class="flex items-center space-x-3">
                        <span class="text-xl">🎯</span>
                        <div>
                            <h4 id="detailComboTitle" class="font-bold text-white text-sm">已選組合: --</h4>
                            <p id="detailComboDesc" class="text-xs text-slate-400">點擊任意矩陣網格即可查看詳細注碼與賠率變動趨勢</p>
                        </div>
                    </div>
                    <span id="detailComboOdds" class="text-2xl font-black font-mono text-emerald-400">--</span>
                </div>
            </div>
        </section>

        <section id="resultsTab" class="tab-content hidden">
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-xl">
                <div class="flex flex-wrap justify-between items-center mb-4 pb-3 border-b border-slate-800 gap-3">
                    <h2 class="text-lg font-bold text-white flex items-center">
                        <span class="w-2.5 h-2.5 bg-emerald-500 rounded-full mr-2"></span>
                        <span id="resultsTitle">2026/27 賽季官方全場賽果與終盤賠率復盤</span>
                    </h2>
                    <div class="flex flex-wrap items-center gap-2">
                        <select id="resultDateSelect" onchange="onResultDateChange()" class="bg-slate-800 border border-slate-700 text-sm rounded-lg px-3 py-1.5 text-emerald-400 font-bold focus:outline-none">
                        </select>
                        <select id="raceSelect" onchange="filterResults()" class="bg-slate-800 border border-slate-700 text-sm rounded-lg px-3 py-1.5 text-white focus:outline-none">
                            <option value="ALL">全部場次</option>
                        </select>
                    </div>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-800/60 text-slate-400 text-xs uppercase font-semibold">
                            <tr>
                                <th class="py-3 px-3">場次</th>
                                <th class="py-3 px-3">名次</th>
                                <th class="py-3 px-3">馬號</th>
                                <th class="py-3 px-3">馬名及烙號</th>
                                <th class="py-3 px-3">騎師</th>
                                <th class="py-3 px-3">練馬師</th>
                                <th class="py-3 px-3">檔位</th>
                                <th class="py-3 px-3">負磅</th>
                                <th class="py-3 px-3">完賽時間</th>
                                <th class="py-3 px-3 text-right">獨贏賠率 (Win Odds)</th>
                            </tr>
                        </thead>
                        <tbody id="resultsTableBody" class="divide-y divide-slate-800">
                        </tbody>
                    </table>
                </div>
            </div>
        </section>

        <section id="racecardTab" class="tab-content hidden">
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-xl">
                <div class="flex justify-between items-center mb-4 pb-3 border-b border-slate-800">
                    <div>
                        <h2 class="text-lg font-bold text-white flex items-center">
                            <span class="w-2.5 h-2.5 bg-blue-500 rounded-full mr-2"></span>
                            <span id="raceCardTitle">跑馬地夜賽（2026/09/16）官方最新排位表</span>
                        </h2>
                        <p id="raceCardSubtitle" class="text-xs text-slate-400 mt-1">共 8 場賽事 · 已成功抓取 96 匹出賽賽駒排位、評分、負磅與檔位 · 頭場預計開跑 19:10</p>
                    </div>
                    <select id="cardRaceSelect" onchange="filterCards()" class="bg-slate-800 border border-slate-700 text-sm rounded-lg px-3 py-1.5 text-white">
                        <option value="ALL">全部場次</option>
                    </select>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-800/60 text-slate-400 text-xs uppercase font-semibold">
                            <tr>
                                <th class="py-3 px-3">場次</th>
                                <th class="py-3 px-3">馬號</th>
                                <th class="py-3 px-3">馬名</th>
                                <th class="py-3 px-3">烙號</th>
                                <th class="py-3 px-3">騎師</th>
                                <th class="py-3 px-3">練馬師</th>
                                <th class="py-3 px-3">檔位</th>
                                <th class="py-3 px-3">負磅</th>
                                <th class="py-3 px-3">官方評分</th>
                                <th class="py-3 px-3">排位體重</th>
                            </tr>
                        </thead>
                        <tbody id="cardsTableBody" class="divide-y divide-slate-800">
                        </tbody>
                    </table>
                </div>
            </div>
    </main>

    <!-- 訪客統計與訪問審計模態框 -->
    <div id="analyticsModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-[100] hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl w-full max-w-4xl max-h-[88vh] flex flex-col shadow-2xl overflow-hidden">
            <!-- 頂部標題 -->
            <div class="p-4 border-b border-slate-800 flex justify-between items-center bg-slate-950/80">
                <div class="flex items-center space-x-3">
                    <div class="w-9 h-9 rounded-xl bg-indigo-600/20 text-indigo-400 flex items-center justify-center font-bold text-lg">👥</div>
                    <div>
                        <h3 class="text-base font-bold text-white flex items-center gap-2">
                            訪客審計與在線監控日誌
                            <span class="text-xs font-normal text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded-full border border-emerald-800/60">實時監控</span>
                        </h3>
                        <p class="text-xs text-slate-400">已自動穿透雲端代理，精準識別訪客真實公網 IP、設備終端及看盤操作流水</p>
                    </div>
                </div>
                <button onclick="closeAnalyticsModal()" class="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800 transition text-lg">✕</button>
            </div>

            <!-- 數據指標卡片 -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 p-4 bg-slate-950/40 border-b border-slate-800">
                <div class="bg-slate-800/60 p-3 rounded-xl border border-slate-700/60">
                    <div class="text-xs text-slate-400 font-medium">當前實時在線 (5分內)</div>
                    <div id="statActiveUsers" class="text-2xl font-black text-emerald-400 mt-1">0</div>
                </div>
                <div class="bg-slate-800/60 p-3 rounded-xl border border-slate-700/60">
                    <div class="text-xs text-slate-400 font-medium">今日獨立訪客 (UV)</div>
                    <div id="statTodayUV" class="text-2xl font-black text-indigo-400 mt-1">0</div>
                </div>
                <div class="bg-slate-800/60 p-3 rounded-xl border border-slate-700/60">
                    <div class="text-xs text-slate-400 font-medium">今日訪問請求 (PV)</div>
                    <div id="statTodayPV" class="text-2xl font-black text-amber-400 mt-1">0</div>
                </div>
                <div class="bg-slate-800/60 p-3 rounded-xl border border-slate-700/60">
                    <div class="text-xs text-slate-400 font-medium">累計獨立終端節點</div>
                    <div id="statTotalNodes" class="text-2xl font-black text-rose-400 mt-1">0</div>
                </div>
            </div>

            <!-- 子標籤切換 -->
            <div class="flex border-b border-slate-800 px-4 pt-2 gap-4 bg-slate-900">
                <button id="btnTabVisitors" onclick="switchAnalyticsSubTab('visitors')" class="pb-2 text-sm font-bold border-b-2 border-indigo-500 text-white transition">獨立訪客終端 (<span id="userCountLabel">0</span>)</button>
                <button id="btnTabLogs" onclick="switchAnalyticsSubTab('logs')" class="pb-2 text-sm font-medium text-slate-400 hover:text-white transition">操作流水日誌 (<span id="logCountLabel">0</span>)</button>
            </div>

            <!-- 內容滾動區 -->
            <div class="p-4 flex-1 overflow-y-auto">
                <!-- 訪客列表 -->
                <div id="subTabVisitors" class="space-y-2">
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs border-collapse">
                            <thead>
                                <tr class="border-b border-slate-800 text-slate-400">
                                    <th class="p-2">在線狀態</th>
                                    <th class="p-2">真實 IP</th>
                                    <th class="p-2">設備終端</th>
                                    <th class="p-2">首次訪問</th>
                                    <th class="p-2">最近活躍時間</th>
                                    <th class="p-2">操作次數</th>
                                    <th class="p-2">最近行為</th>
                                </tr>
                            </thead>
                            <tbody id="visitorTableBody" class="divide-y divide-slate-800/60 font-mono">
                                <tr><td colspan="7" class="p-4 text-center text-slate-500">暫無訪客記錄</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- 流水日誌 -->
                <div id="subTabLogs" class="space-y-1.5 hidden">
                    <div id="activityLogsContainer" class="space-y-1 text-xs font-mono max-h-[360px] overflow-y-auto pr-1">
                        <div class="text-center text-slate-500 p-4">暫無操作日誌</div>
                    </div>
                </div>
            </div>

            <!-- 底部操作列 -->
            <div class="p-3 border-t border-slate-800 bg-slate-950/80 flex flex-wrap justify-between items-center text-xs text-slate-400 gap-2">
                <span>💡 已自動過濾 Render 雲端保活與無效心跳，數據為真實用戶看盤操作。</span>
                <div class="flex items-center space-x-2">
                    <span id="analyticsUpdateTime" class="font-mono text-slate-500"></span>
                    <button onclick="refreshAnalyticsModal()" class="px-3 py-1 bg-slate-800 hover:bg-indigo-600 hover:text-white text-slate-300 rounded font-medium transition">🔄 立即刷新</button>
                </div>
            </div>
    <!-- 實盤數據採集控制台模態框 -->
    <div id="scraperModal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-[100] hidden flex items-center justify-center p-4">
        <div class="bg-slate-900 border border-slate-700 rounded-2xl w-full max-w-2xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
            <!-- 頂部標題 -->
            <div class="p-4 border-b border-slate-800 flex justify-between items-center bg-slate-950/80">
                <div class="flex items-center space-x-3">
                    <div class="w-9 h-9 rounded-xl bg-amber-600/20 text-amber-400 flex items-center justify-center font-bold text-lg">⚙️</div>
                    <div>
                        <h3 class="text-base font-bold text-white flex items-center gap-2">
                            實盤數據採集控制台 (Scraper Control)
                            <span id="scraperModalBadge" class="text-xs font-normal text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded-full border border-emerald-800/60">載入中...</span>
                        </h3>
                        <p class="text-xs text-slate-400">自主設定賽事日期、場次範圍 (1~11場)、輪詢頻率並支援一鍵手動強制抓取</p>
                    </div>
                </div>
                <button onclick="closeScraperModal()" class="text-slate-400 hover:text-white p-1.5 rounded-lg hover:bg-slate-800 transition text-lg cursor-pointer">✕</button>
            </div>

            <!-- 主體內容 -->
            <div class="p-5 space-y-5 overflow-y-auto max-h-[calc(90vh-140px)]">
                <!-- 第一區塊：一鍵手動立即抓取 -->
                <div class="bg-gradient-to-r from-slate-800/90 to-slate-850 p-4 rounded-xl border border-slate-700/80 shadow-lg">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                        <div>
                            <h4 class="text-sm font-bold text-white flex items-center">
                                <span class="text-amber-400 mr-1.5">⚡</span> 手動即時抓取 (Manual Trigger)
                            </h4>
                            <p class="text-xs text-slate-400 mt-0.5">無需等待後台計時器，立即向馬會 GraphQL 接口拉取全場次最新實盤賠率</p>
                        </div>
                        <button id="btnTriggerScraper" onclick="triggerManualScrape()" class="px-5 py-2.5 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white font-bold text-sm rounded-xl shadow-lg shadow-emerald-950/50 flex items-center justify-center transition cursor-pointer shrink-0">
                            <span id="btnTriggerIcon" class="mr-2">🚀</span>
                            <span id="btnTriggerText">立即抓取一次</span>
                        </button>
                    </div>

                    <!-- 抓取結果反饋橫幅 -->
                    <div id="scrapeResultAlert" class="mt-3 p-3 rounded-lg text-xs font-mono hidden"></div>
                </div>

                <!-- 第二區塊：自主設定時間、場次與參數 -->
                <div class="bg-slate-800/50 p-4 rounded-xl border border-slate-700/60 space-y-4">
                    <h4 class="text-xs font-bold text-slate-300 uppercase tracking-wider flex items-center">
                        <span class="w-2 h-2 bg-blue-400 rounded-full mr-2"></span>
                        採集參數與時間設定
                    </h4>

                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <!-- 賽事日期 -->
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">目標賽事日期 (Target Date)</label>
                            <div class="flex items-center space-x-2">
                                <input type="text" id="cfgTargetDate" placeholder="YYYY-MM-DD (例如 2026-10-02)" class="w-full bg-slate-900 border border-slate-700 text-sm font-mono text-emerald-400 rounded-lg px-3 py-2 focus:outline-none focus:border-blue-500">
                                <button type="button" onclick="setScraperDateToday()" class="px-2.5 py-2 bg-slate-700 hover:bg-slate-600 text-xs font-semibold text-slate-200 rounded-lg whitespace-nowrap transition cursor-pointer">今日</button>
                            </div>
                            <span class="text-[11px] text-slate-500 mt-1 block">留空將自動按週推算 (週三HV夜賽 / 週日ST日賽)</span>
                        </div>

                        <!-- 賽事場地 -->
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">賽事場地 (Venue)</label>
                            <select id="cfgTargetVenue" class="w-full bg-slate-900 border border-slate-700 text-sm font-bold text-white rounded-lg px-3 py-2 focus:outline-none focus:border-blue-500">
                                <option value="ST">沙田 (ST - Shatin)</option>
                                <option value="HV">跑馬地 (HV - Happy Valley)</option>
                            </select>
                        </div>

                        <!-- 抓取場次範圍 -->
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">抓取場次上限 (Max Races)</label>
                            <select id="cfgMaxRaces" class="w-full bg-slate-900 border border-slate-700 text-sm font-bold text-amber-400 rounded-lg px-3 py-2 focus:outline-none focus:border-blue-500">
                                <option value="11" selected>第 1 ~ 11 場 (沙田常規/大賽日 推薦)</option>
                                <option value="10">第 1 ~ 10 場 (沙田常規 10 場)</option>
                                <option value="9">第 1 ~ 9 場 (跑馬地滿編 9 場)</option>
                                <option value="8">第 1 ~ 8 場 (跑馬地常規 8 場)</option>
                                <option value="12">第 1 ~ 12 場 (特別賽事/海外賽事)</option>
                            </select>
                            <span class="text-[11px] text-slate-500 mt-1 block">徹底解決 11 场日赛仅抓取 8 场的限制</span>
                        </div>

                        <!-- 採集頻率模式 -->
                        <div>
                            <label class="text-xs font-semibold text-slate-400 block mb-1">採集模式與頻率 (Interval)</label>
                            <select id="cfgMode" onchange="toggleIntervalInput()" class="w-full bg-slate-900 border border-slate-700 text-sm font-bold text-white rounded-lg px-3 py-2 focus:outline-none focus:border-blue-500 mb-2">
                                <option value="smart">智能時段自適應 (Smart Mode 推薦)</option>
                                <option value="fixed">固定秒數輪詢 (Fixed Interval)</option>
                            </select>
                            <div id="fixedIntervalBox" class="hidden">
                                <select id="cfgFixedInterval" class="w-full bg-slate-900 border border-slate-700 text-xs font-mono text-slate-300 rounded-lg px-3 py-1.5 focus:outline-none focus:border-blue-500">
                                    <option value="15">15 秒 (臨場高頻衝刺)</option>
                                    <option value="30">30 秒 (高頻實戰)</option>
                                    <option value="60">60 秒 (1分鐘 中盤常規)</option>
                                    <option value="120">120 秒 (2分鐘 早盤)</option>
                                    <option value="300">300 秒 (5分鐘 低頻待命)</option>
                                </select>
                            </div>
                        </div>
                    </div>

                    <!-- 採集開關與保存操作 -->
                    <div class="pt-3 border-t border-slate-700/60 flex flex-wrap justify-between items-center gap-3">
                        <div class="flex items-center space-x-2">
                            <label class="text-xs font-semibold text-slate-400">後台自動輪詢狀態:</label>
                            <select id="cfgEnabled" class="bg-slate-900 border border-slate-700 text-xs font-bold rounded-lg px-2.5 py-1 text-white">
                                <option value="1">🟢 啟用自動輪詢</option>
                                <option value="0">⏸️ 暫停自動輪詢</option>
                            </select>
                        </div>
                        <button onclick="saveScraperConfig()" class="px-5 py-2 bg-blue-600 hover:bg-blue-500 text-white font-bold text-xs rounded-xl shadow-md shadow-blue-900/40 transition cursor-pointer">
                            💾 保存設定並立即生效
                        </button>
                    </div>
                </div>

                <!-- 第三區塊：當前採集器指標 -->
                <div class="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-2">
                    <div class="text-xs font-bold text-slate-400 flex items-center justify-between">
                        <span>📊 當前採集器實時運行指標</span>
                        <button onclick="fetchScraperStatusModal()" class="text-slate-400 hover:text-white text-[11px] underline cursor-pointer">刷新狀態</button>
                    </div>
                    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono pt-1">
                        <div class="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                            <span class="text-slate-500 block text-[10px]">最後運行時間</span>
                            <span id="statLastRunTime" class="text-slate-300 font-bold">--</span>
                        </div>
                        <div class="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                            <span class="text-slate-500 block text-[10px]">當前輪詢間隔</span>
                            <span id="statCurrentInterval" class="text-amber-400 font-bold">--</span>
                        </div>
                        <div class="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                            <span class="text-slate-500 block text-[10px]">累計抓取輪次</span>
                            <span id="statTotalRuns" class="text-blue-400 font-bold">--</span>
                        </div>
                        <div class="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                            <span class="text-slate-500 block text-[10px]">累計入庫筆數</span>
                            <span id="statTotalSaved" class="text-emerald-400 font-bold">--</span>
                        </div>
                    </div>
                    <div class="text-[11px] text-slate-400 pt-1">
                        <span class="text-slate-500">最新狀態備註: </span>
                        <span id="statLastStatus" class="font-mono text-slate-300">--</span>
                    </div>
                </div>
            </div>

            <!-- 底部關閉按鈕 -->
            <div class="p-3 border-t border-slate-800 bg-slate-950/80 flex justify-end">
                <button onclick="closeScraperModal()" class="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-medium transition cursor-pointer">關閉窗口</button>
            </div>
        </div>
    </div>

    <footer class="bg-slate-950 border-t border-slate-800 py-4 text-center text-xs text-slate-500">
        hsjc 2.0 Web SaaS · 香港職業賽馬團隊專屬定制 · 完美還原並超越原版 WinForms ResultForm & CompareForm
    </footer>

    <script>
        let allResults = [];
        let allCards = [];
        let allTimestamps = [];

        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.remove('bg-blue-600', 'text-white');
                btn.classList.add('bg-slate-800', 'text-slate-300');
            });
            
            document.getElementById(tabId).classList.remove('hidden');
            if (tabId === 'compareTab') {
                document.getElementById('btnCompare').classList.add('bg-blue-600', 'text-white');
                document.getElementById('btnCompare').classList.remove('bg-slate-800', 'text-slate-300');
            } else if (tabId === 'matrixTab') {
                document.getElementById('btnMatrix').classList.add('bg-blue-600', 'text-white');
                document.getElementById('btnMatrix').classList.remove('bg-slate-800', 'text-slate-300');
                loadMatrixData();
            } else if (tabId === 'resultsTab') {
                document.getElementById('btnResults').classList.add('bg-blue-600', 'text-white');
                document.getElementById('btnResults').classList.remove('bg-slate-800', 'text-slate-300');
            } else if (tabId === 'racecardTab') {
                document.getElementById('btnRacecard').classList.add('bg-blue-600', 'text-white');
                document.getElementById('btnRacecard').classList.remove('bg-slate-800', 'text-slate-300');
            }
        }

        const RACE_SCHEDULE_HV = {
            1: "19:10", 2: "19:40", 3: "20:10", 4: "20:40",
            5: "21:10", 6: "21:45", 7: "22:15", 8: "22:50", 9: "23:20"
        };
        const RACE_SCHEDULE_ST = {
            1: "13:00", 2: "13:30", 3: "14:00", 4: "14:30", 5: "15:00",
            6: "15:35", 7: "16:05", 8: "16:40", 9: "17:15", 10: "17:50", 11: "18:25"
        };

        function formatDateLabel(dateStr, timestamps) {
            if (dateStr === '2026-09-16') return `${dateStr} (谷草夜賽 · 8場全量)`;
            if (dateStr === '2026-09-06') return `${dateStr} (沙田日賽 · 開鑼日)`;
            if (dateStr === '2022-09-18') return `${dateStr} (歷史大戶庫)`;
            const hasNight = (timestamps || []).some(t => t.startsWith(dateStr) && parseInt((t.split(' ')[1]||'').split(':')[0], 10) >= 18);
            const label = hasNight ? '谷草夜賽' : '沙田日賽/早盤';
            return `${dateStr} (${label})`;
        }

        function getRacePostTime(raceNo, dateStr, timestampsForDate) {
            const hasNight = (timestampsForDate || []).some(t => {
                const hour = parseInt((t.split(' ')[1] || '').split(':')[0], 10);
                return hour >= 18;
            });
            const schedule = hasNight ? RACE_SCHEDULE_HV : RACE_SCHEDULE_ST;
            const timeStr = schedule[raceNo] || (hasNight ? '20:00' : '14:00');
            return `${dateStr} ${timeStr}:00`;
        }

        function findClosestTimestamp(targetMs, timestamps) {
            if (!timestamps || timestamps.length === 0) return null;
            let bestTs = timestamps[0];
            let minDiff = Infinity;
            for (const ts of timestamps) {
                const tsMs = new Date(ts.replace(/-/g, '/')).getTime();
                const diff = Math.abs(tsMs - targetMs);
                if (diff < minDiff) {
                    minDiff = diff;
                    bestTs = ts;
                }
            }
            return bestTs;
        }

        function buildGroupedTimeOptions(timestamps, selectedDate, selectedRaceNo) {
            if (!timestamps || timestamps.length === 0) return '<option value="">(無切片記錄)</option>';
            const filtered = selectedDate ? timestamps.filter(t => t.startsWith(selectedDate)) : timestamps;
            if (filtered.length === 0) return '<option value="">(當前日期暫無切片)</option>';

            const postTimeStr = selectedDate && selectedRaceNo ? getRacePostTime(selectedRaceNo, selectedDate, filtered) : null;
            const postTimeMs = postTimeStr ? new Date(postTimeStr.replace(/-/g, '/')).getTime() : null;

            let anchorPost = null;
            let anchor5m = null;
            let anchor15m = null;
            let anchor30m = null;
            const earliestOfDay = filtered[filtered.length - 1];
            const latestOfDay = filtered[0];

            if (postTimeMs) {
                anchorPost = findClosestTimestamp(postTimeMs, filtered);
                anchor5m = findClosestTimestamp(postTimeMs - 5 * 60 * 1000, filtered);
                anchor15m = findClosestTimestamp(postTimeMs - 15 * 60 * 1000, filtered);
                anchor30m = findClosestTimestamp(postTimeMs - 30 * 60 * 1000, filtered);
            }

            const groups = {
                settle: { label: '🏁 終盤收官 / 即時最新切片', items: [] },
                inplay: { label: '🔴 臨場戰時高頻時段 (18:00 - 23:59)', items: [] },
                early: { label: '🟡 賽事日早盤時段 (10:00 - 18:00)', items: [] },
                history: { label: '⚪ 歷史切片 / 隔日初盤', items: [] }
            };

            filtered.forEach((ts, idx) => {
                const timePart = ts.split(' ')[1] || '';
                const hour = parseInt(timePart.split(':')[0], 10);
                let badge = '';

                if (selectedRaceNo && postTimeMs) {
                    if (ts === anchorPost) badge = ` 🏁 [第${selectedRaceNo}場 開跑終盤]`;
                    else if (ts === anchor5m) badge = ` ⚡ [第${selectedRaceNo}場 臨場5分]`;
                    else if (ts === anchor15m) badge = ` 🚨 [第${selectedRaceNo}場 大戶15分]`;
                    else if (ts === anchor30m) badge = ` ⏱️ [第${selectedRaceNo}場 賽前30分]`;
                }

                if (!badge) {
                    if (ts === latestOfDay) badge = ' 🔥 (最新)';
                    else if (ts === earliestOfDay) badge = ' 📍 (早盤基準)';
                }

                const item = { val: ts, text: `${ts}${badge}` };
                if (idx === 0 || (ts.includes('23:') && idx < 5)) {
                    groups.settle.items.push(item);
                } else if (hour >= 18 && hour <= 23) {
                    groups.inplay.items.push(item);
                } else if (hour >= 10 && hour < 18) {
                    groups.early.items.push(item);
                } else {
                    groups.history.items.push(item);
                }
            });

            let html = '';
            for (const key of ['settle', 'inplay', 'early', 'history']) {
                const g = groups[key];
                if (g.items.length > 0) {
                    html += `<optgroup label="${g.label}">`;
                    html += g.items.map(it => `<option value="${it.val}">${it.text}</option>`).join('');
                    html += `</optgroup>`;
                }
            }
            return html;
        }

        function populateDateSelector(timestamps) {
            const dateSelect = document.getElementById('compDateSelect');
            if (!dateSelect) return;
            const dates = Array.from(new Set((timestamps || []).map(t => t.split(' ')[0]))).filter(Boolean);
            dates.sort((a, b) => b.localeCompare(a));
            
            dateSelect.innerHTML = dates.map(d => `<option value="${d}">${formatDateLabel(d, timestamps)}</option>`).join('');
            if (dates.includes('2026-09-16')) {
                dateSelect.value = '2026-09-16';
            } else if (dates.length > 0) {
                dateSelect.value = dates[0];
            }
        }

        function updateTimeSelectors(selectedDate, selectedRaceNo, preserveSelection = false) {
            const t1Select = document.getElementById('compTime1');
            const t2Select = document.getElementById('compTime2');
            const matrixTimeSelect = document.getElementById('matrixTime');
            if (!t1Select || !t2Select) return;

            const curT1 = t1Select.value;
            const curT2 = t2Select.value;
            const filtered = selectedDate ? allTimestamps.filter(t => t.startsWith(selectedDate)) : allTimestamps;
            const groupedHtml = buildGroupedTimeOptions(allTimestamps, selectedDate, selectedRaceNo);

            t1Select.innerHTML = groupedHtml;
            t2Select.innerHTML = groupedHtml;
            if (matrixTimeSelect) {
                matrixTimeSelect.innerHTML = groupedHtml;
            }

            if (preserveSelection && filtered.includes(curT1) && filtered.includes(curT2)) {
                t1Select.value = curT1;
                t2Select.value = curT2;
            } else if (filtered.length > 0) {
                t2Select.value = filtered[0];
                t1Select.value = filtered.length > 1 ? filtered[1] : filtered[0];
                if (matrixTimeSelect) {
                    matrixTimeSelect.value = filtered[0];
                }
            }
        }

        function onDateChange() {
            const selectedDate = document.getElementById('compDateSelect').value;
            const selectedRaceNo = parseInt(document.getElementById('compRaceNo').value, 10) || 1;
            updateTimeSelectors(selectedDate, selectedRaceNo, false);
            loadCompareData();
            if (!document.getElementById('matrixTab').classList.contains('hidden')) {
                loadMatrixData();
            }
        }

        function onRaceChange() {
            const selectedDate = document.getElementById('compDateSelect').value;
            const selectedRaceNo = parseInt(document.getElementById('compRaceNo').value, 10) || 1;
            updateTimeSelectors(selectedDate, selectedRaceNo, true);
            loadCompareData();
            if (!document.getElementById('matrixTab').classList.contains('hidden')) {
                loadMatrixData();
            }
        }

        function applyTacticPreset(presetType) {
            if (!allTimestamps || allTimestamps.length === 0) return;
            const selectedDate = document.getElementById('compDateSelect') ? document.getElementById('compDateSelect').value : '';
            const selectedRaceNo = parseInt(document.getElementById('compRaceNo').value, 10) || 1;
            const filtered = selectedDate ? allTimestamps.filter(t => t.startsWith(selectedDate)) : allTimestamps;
            if (filtered.length === 0) return;

            const t1Select = document.getElementById('compTime1');
            const t2Select = document.getElementById('compTime2');

            document.querySelectorAll('.preset-btn').forEach(btn => {
                btn.classList.remove('ring-2', 'ring-emerald-400', 'bg-emerald-900/60');
            });

            const postTimeStr = getRacePostTime(selectedRaceNo, selectedDate, filtered);
            const postTimeMs = new Date(postTimeStr.replace(/-/g, '/')).getTime();
            const latestOfDayMs = new Date(filtered[0].replace(/-/g, '/')).getTime();
            const isHistoricalRace = latestOfDayMs > postTimeMs + 5 * 60 * 1000;

            if (presetType === '5min') {
                if (isHistoricalRace) {
                    t2Select.value = findClosestTimestamp(postTimeMs - 2 * 60 * 1000, filtered);
                    t1Select.value = findClosestTimestamp(postTimeMs - 7 * 60 * 1000, filtered);
                } else {
                    t2Select.value = filtered[0];
                    t1Select.value = findClosestTimestamp(latestOfDayMs - 5 * 60 * 1000, filtered);
                }
            } else if (presetType === '15min') {
                if (isHistoricalRace) {
                    t2Select.value = findClosestTimestamp(postTimeMs - 2 * 60 * 1000, filtered);
                    t1Select.value = findClosestTimestamp(postTimeMs - 17 * 60 * 1000, filtered);
                } else {
                    t2Select.value = filtered[0];
                    t1Select.value = findClosestTimestamp(latestOfDayMs - 15 * 60 * 1000, filtered);
                }
            } else if (presetType === 'early') {
                if (isHistoricalRace) {
                    t2Select.value = findClosestTimestamp(postTimeMs - 2 * 60 * 1000, filtered);
                } else {
                    t2Select.value = filtered[0];
                }
                t1Select.value = filtered[filtered.length - 1];
            } else if (presetType === 'settle') {
                if (isHistoricalRace) {
                    t2Select.value = findClosestTimestamp(postTimeMs, filtered);
                } else {
                    t2Select.value = filtered[0];
                }
                t1Select.value = filtered[filtered.length - 1];
            }

            if (window.event && window.event.currentTarget) {
                window.event.currentTarget.classList.add('ring-2', 'ring-emerald-400', 'bg-emerald-900/60');
            }

            loadCompareData();
        }

        async function initPage() {
            try {
                const tsRes = await fetch('/api/timestamps');
                allTimestamps = await tsRes.json();
                
                if (allTimestamps.length > 0) {
                    populateDateSelector(allTimestamps);
                    const selectedDate = document.getElementById('compDateSelect').value;
                    const selectedRaceNo = parseInt(document.getElementById('compRaceNo').value, 10) || 1;
                    updateTimeSelectors(selectedDate, selectedRaceNo, false);
                }

                await loadCompareData();

                const resRes = await fetch('/api/results');
                allResults = await resRes.json();
                renderResults(allResults);

                const cardRes = await fetch('/api/racecards');
                allCards = await cardRes.json();
                renderCards(allCards);

                // 載入資料庫連線狀態標籤
                try {
                    const dbRes = await fetch('/api/db/status');
                    const dbInfo = await dbRes.json();
                    const dbBadge = document.getElementById('dbStatusBadge');
                    if (dbBadge && dbInfo) {
                        if (dbInfo.type && dbInfo.type.includes("Turso")) {
                            dbBadge.className = "text-[11px] font-bold px-2 py-0.5 bg-indigo-950 text-indigo-300 border border-indigo-700/60 rounded-full flex items-center shadow-sm";
                            dbBadge.innerHTML = `<span class="w-1.5 h-1.5 bg-indigo-400 rounded-full mr-1.5"></span>☁️ Turso 雲端庫 (已連通)`;
                        } else {
                            dbBadge.className = "text-[11px] font-bold px-2 py-0.5 bg-slate-800 text-slate-300 border border-slate-700 rounded-full flex items-center";
                            dbBadge.innerHTML = `<span class="w-1.5 h-1.5 bg-emerald-400 rounded-full mr-1.5"></span>💾 本地 SQLite (已連通)`;
                        }
                    }
                } catch (e) {
                    console.warn("無法取得資料庫狀態:", e);
                }

            } catch (err) {
                console.error("初始化載入失敗:", err);
            }
        }

        async function loadCompareData() {
            const pool = document.getElementById('compPool').value;
            const raceNo = document.getElementById('compRaceNo').value;
            const t1 = document.getElementById('compTime1').value;
            const t2 = document.getElementById('compTime2').value;

            try {
                const res = await fetch(`/api/compare?pool=${pool}&raceNo=${raceNo}&time1=${encodeURIComponent(t1)}&time2=${encodeURIComponent(t2)}`);
                const data = await res.json();
                renderTopDrops(data.topDrops || []);
                renderCompareTable(data.items || []);
            } catch (err) {
                console.error("獲取比對數據失敗:", err);
            }
        }

        function renderTopDrops(drops) {
            const container = document.getElementById('topDropCards');
            if (!drops || drops.length === 0) {
                container.innerHTML = `<div class="col-span-3 p-3 bg-slate-800/40 rounded-lg text-xs text-slate-400 text-center">當前比對區間暫無大於 15% 的極端異動</div>`;
                return;
            }

            container.innerHTML = drops.slice(0, 3).map((item, idx) => `
                <div class="bg-gradient-to-r from-emerald-950/40 to-slate-900 border border-emerald-500/30 p-3.5 rounded-xl shadow-lg relative overflow-hidden">
                    <div class="flex justify-between items-start">
                        <div>
                            <span class="text-[10px] font-bold uppercase tracking-wider text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded border border-emerald-800/50">🚨 大戶落飛警告 #${idx + 1}</span>
                            <h3 class="text-lg font-black text-white mt-1.5"><span class="text-emerald-400 text-xl font-mono">${item.number}</span> 號馬匹/組合</h3>
                            <p class="text-xs text-slate-400 font-mono mt-0.5">賠率: <span class="line-through text-slate-500">${item.time1Odds}</span> &rarr; <span class="text-emerald-400 font-bold text-sm">${item.time2Odds}</span></p>
                        </div>
                        <div class="text-right">
                            <span class="text-2xl font-black font-mono text-emerald-400">${item.percentChange}%</span>
                            <div class="text-[11px] text-emerald-500 font-semibold mt-1">差值: ${item.diff}</div>
                        </div>
                    </div>
                </div>
            `).join('');
        }

        function renderCompareTable(items) {
            const tbody = document.getElementById('compareTableBody');
            tbody.innerHTML = items.map(item => {
                const isDrop = item.diff < 0;
                const isHeavyDrop = item.percentChange <= -15;
                const isRise = item.diff > 0;
                
                let rowBg = "hover:bg-slate-800/30 transition";
                let statusBadge = `<span class="px-2 py-0.5 rounded text-xs text-slate-400 bg-slate-800">${item.status}</span>`;

                if (isHeavyDrop) {
                    rowBg = "bg-emerald-950/25 hover:bg-emerald-950/40 transition font-bold";
                    statusBadge = `<span class="px-2.5 py-0.5 rounded-full text-xs font-bold badge-drop">🔥 ${item.status}</span>`;
                } else if (isDrop) {
                    statusBadge = `<span class="px-2 py-0.5 rounded text-xs text-emerald-400 bg-emerald-950/30">${item.status}</span>`;
                } else if (isRise) {
                    statusBadge = `<span class="px-2 py-0.5 rounded text-xs badge-rise">${item.status}</span>`;
                }

                const diffColor = isDrop ? "text-emerald-400" : (isRise ? "text-rose-400" : "text-slate-400");
                const hotIcon = item.hot === 1 ? "🔥 焦點" : (item.scratched === 1 ? "❌ 退出" : "-");

                return `
                    <tr class="${rowBg}">
                        <td class="py-2.5 px-3 font-bold text-amber-400">${item.number}</td>
                        <td class="py-2.5 px-3 text-xs text-slate-300 font-sans">${hotIcon}</td>
                        <td class="py-2.5 px-3 text-slate-400">${item.time1Odds.toFixed(1)}</td>
                        <td class="py-2.5 px-3 text-white font-bold">${item.time2Odds.toFixed(1)}</td>
                        <td class="py-2.5 px-3 ${diffColor} font-bold">${item.diff > 0 ? '+' + item.diff : item.diff}</td>
                        <td class="py-2.5 px-3 ${diffColor} font-bold">${item.percentChange > 0 ? '+' + item.percentChange + '%' : item.percentChange + '%'}</td>
                        <td class="py-2.5 px-3 text-right font-sans">${statusBadge}</td>
                    </tr>
                `;
            }).join('');
        }

        async function loadMatrixData() {
            const pool = document.getElementById('matrixPool').value;
            const raceNo = document.getElementById('matrixRaceNo').value;
            const timeVal = document.getElementById('matrixTime') ? document.getElementById('matrixTime').value : '';
            try {
                let url = `/api/matrix?pool=${pool}&raceNo=${raceNo}`;
                if (timeVal) {
                    url += `&time=${encodeURIComponent(timeVal)}`;
                }
                const res = await fetch(url);
                const data = await res.json();
                renderRealMatrix(data.matrix || {}, raceNo, pool, data.time);
            } catch (err) {
                console.error("載入矩陣失敗:", err);
            }
        }

        function renderRealMatrix(matrixDict, raceNo, pool, timeStr) {
            const table = document.getElementById('matrixTable');
            const horseSet = new Set();
            for (const k in matrixDict) {
                const parts = k.split('-');
                if (parts.length === 2 && !isNaN(parts[0]) && !isNaN(parts[1])) {
                    horseSet.add(parseInt(parts[0]));
                    horseSet.add(parseInt(parts[1]));
                }
            }
            let horses = Array.from(horseSet).sort((a, b) => a - b);
            if (horses.length === 0) {
                horses = Array.from({length: 14}, (_, i) => i + 1);
            }

            const badge = document.getElementById('matrixCountBadge');
            if (badge) {
                badge.innerText = `${horses.length}x${horses.length} 網格 · 共 ${Object.keys(matrixDict).length} 組賠率 · 實時切片: ${timeStr || '最新'}`;
            }

            let html = '<thead><tr class="bg-slate-800 text-slate-300"><th class="p-2 border border-slate-700">馬號</th>';
            horses.forEach(c => {
                html += `<th class="p-2 border border-slate-700 text-amber-400 font-bold font-mono">${c}號</th>`;
            });
            html += '</tr></thead><tbody>';

            horses.forEach(r => {
                html += `<tr><td class="p-2 bg-slate-800 border border-slate-700 font-bold text-amber-400 font-mono">${r}號</td>`;
                horses.forEach(c => {
                    if (r === c) {
                        html += '<td class="p-2 border border-slate-800 bg-slate-950/70 text-slate-600 font-mono select-none">-</td>';
                    } else if (r < c) {
                        const key1 = `${r}-${c}`;
                        const key2 = `${c}-${r}`;
                        const item = matrixDict[key1] || matrixDict[key2];
                        let oddsDisplay = "--";
                        let dropBadge = "";
                        let cellBg = "bg-slate-900/90 text-slate-200 border-slate-800 hover:bg-blue-600";
                        if (item) {
                            oddsDisplay = item.odds.toFixed(1);
                            const drop = item.oddsDrop;
                            if (drop <= -8.0) {
                                cellBg = "bg-emerald-950 text-emerald-300 font-black border-emerald-500/80 shadow-md ring-1 ring-emerald-400/40";
                                dropBadge = `<div class="text-[10px] text-emerald-400 font-sans leading-none mt-0.5">↓${Math.abs(drop).toFixed(1)}</div>`;
                            } else if (drop < 0) {
                                cellBg = "bg-emerald-950/50 text-emerald-400 font-bold border-emerald-700/50";
                                dropBadge = `<div class="text-[10px] text-emerald-400/80 font-sans leading-none mt-0.5">↓${Math.abs(drop).toFixed(1)}</div>`;
                            } else if (drop > 5.0) {
                                cellBg = "bg-rose-950/40 text-rose-300 border-rose-800/40";
                                dropBadge = `<div class="text-[10px] text-rose-400 font-sans leading-none mt-0.5">↑${drop.toFixed(1)}</div>`;
                            }
                        }
                        const dropParam = item ? item.oddsDrop : 0;
                        html += `<td onclick="showMatrixDetail('${key1}', '${oddsDisplay}', ${dropParam}, '${pool}')" class="p-2 border matrix-cell font-mono cursor-pointer transition ${cellBg}" title="${key1} 組合賠率: ${oddsDisplay}">${oddsDisplay}${dropBadge}</td>`;
                    } else {
                        html += '<td class="p-2 border border-slate-800 bg-slate-950/40 text-slate-600 text-[10px] font-sans select-none">對稱</td>';
                    }
                });
                html += '</tr>';
            });
            html += '</tbody>';
            table.innerHTML = html;
        }

        function showMatrixDetail(combo, odds, dropVal, pool) {
            const card = document.getElementById('matrixDetailCard');
            card.classList.remove('hidden');
            document.getElementById('detailComboTitle').innerText = `已選組合: ${combo} (${pool || '連贏'})`;
            const drop = parseFloat(dropVal) || 0;
            let dropDesc = "賠率平穩";
            if (drop < -5) {
                dropDesc = `🔥 大戶狂砸重注！臨場賠率暴跌 ${Math.abs(drop).toFixed(1)} 點`;
            } else if (drop < 0) {
                dropDesc = `資金顯著流入，落飛 ${Math.abs(drop).toFixed(1)} 點`;
            } else if (drop > 0) {
                dropDesc = `冷門資金流出，回飛 ${drop.toFixed(1)} 點`;
            }
            document.getElementById('detailComboDesc').innerText = `${dropDesc} · 點擊任意其他網格即時比對`;
            document.getElementById('detailComboOdds').innerText = odds;
        }

        function renderResults(list) {
            if (!list || list.length === 0) return;
            const dateSelect = document.getElementById('resultDateSelect');
            if (dateSelect) {
                const dates = [...new Set(list.map(i => i.raceDate.replace(/\//g, '-')))].sort((a, b) => b.localeCompare(a));
                dateSelect.innerHTML = dates.map(d => {
                    const label = d === '2026-09-16' ? `${d} (跑馬地夜賽 · 8場頭馬全量)` : `${d} (沙田日賽 · 開鑼日)`;
                    return `<option value="${d}">${label}</option>`;
                }).join('');
                if (dates.includes('2026-09-16')) {
                    dateSelect.value = '2026-09-16';
                }
            }
            onResultDateChange();
        }

        function onResultDateChange() {
            const dateSelect = document.getElementById('resultDateSelect');
            const dateVal = dateSelect ? dateSelect.value : 'ALL';
            const filteredByDate = (dateVal === 'ALL' || !dateVal) 
                ? allResults 
                : allResults.filter(i => i.raceDate.replace(/\//g, '-') === dateVal.replace(/\//g, '-'));

            const distinctRaces = [...new Set(filteredByDate.map(i => i.raceNo))].sort((a, b) => a - b);
            const selectEl = document.getElementById('raceSelect');
            if (selectEl) {
                selectEl.innerHTML = `<option value="ALL">全部 ${distinctRaces.length} 場賽事</option>` +
                    distinctRaces.map(r => `<option value="${r}">第 ${r} 場</option>`).join('');
            }
            filterResults();
        }

        function filterResults() {
            const dateSelect = document.getElementById('resultDateSelect');
            const dateVal = dateSelect ? dateSelect.value : 'ALL';
            const raceVal = document.getElementById('raceSelect') ? document.getElementById('raceSelect').value : 'ALL';

            let list = allResults;
            if (dateVal && dateVal !== 'ALL') {
                list = list.filter(i => i.raceDate.replace(/\//g, '-') === dateVal.replace(/\//g, '-'));
            }
            if (raceVal && raceVal !== 'ALL') {
                list = list.filter(i => String(i.raceNo) === raceVal);
            }
            renderResultsTable(list);
        }

        function renderResultsTable(list) {
            const tbody = document.getElementById('resultsTableBody');
            if (!tbody) return;
            tbody.innerHTML = list.map(item => {
                const isWinner = item.placing === '1';
                const oddsBadge = isWinner 
                    ? `<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold border border-emerald-500/30">${item.winOdds} (頭馬)</span>`
                    : `<span class="text-slate-300 font-mono">${item.winOdds}</span>`;

                return `
                    <tr class="hover:bg-slate-800/40 transition ${isWinner ? 'bg-emerald-950/20' : ''}">
                        <td class="py-2.5 px-3 font-semibold text-amber-400 font-mono">R${item.raceNo}</td>
                        <td class="py-2.5 px-3 ${isWinner ? 'font-bold text-emerald-400' : 'text-slate-400'} font-mono">${item.placing}</td>
                        <td class="py-2.5 px-3 font-mono text-slate-300">${item.horseNo}</td>
                        <td class="py-2.5 px-3 font-medium text-white">${item.horseName}</td>
                        <td class="py-2.5 px-3 text-slate-300">${item.jockey}</td>
                        <td class="py-2.5 px-3 text-slate-400">${item.trainer}</td>
                        <td class="py-2.5 px-3 text-slate-400 font-mono">${item.draw}</td>
                        <td class="py-2.5 px-3 text-slate-400 font-mono">${item.actualWeight}</td>
                        <td class="py-2.5 px-3 font-mono text-xs text-slate-400">${item.finishTime}</td>
                        <td class="py-2.5 px-3 text-right">${oddsBadge}</td>
                    </tr>
                `;
            }).join('');
        }

        function filterCards() {
            const val = document.getElementById('cardRaceSelect').value;
            if (val === 'ALL') {
                renderCardsTable(allCards);
            } else {
                renderCardsTable(allCards.filter(i => String(i.raceNo) === val));
            }
        }

        function renderCardsTable(list) {
            const tbody = document.getElementById('cardsTableBody');
            tbody.innerHTML = list.map(item => `
                <tr class="hover:bg-slate-800/40 transition">
                    <td class="py-2.5 px-3 font-semibold text-blue-400 font-mono">R${item.raceNo}</td>
                    <td class="py-2.5 px-3 font-mono text-slate-300">${item.horseNo}</td>
                    <td class="py-2.5 px-3 font-medium text-white">${item.horseName}</td>
                    <td class="py-2.5 px-3 font-mono text-xs text-slate-400">${item.brandNo}</td>
                    <td class="py-2.5 px-3 text-slate-300">${item.jockey}</td>
                    <td class="py-2.5 px-3 text-slate-400">${item.trainer}</td>
                    <td class="py-2.5 px-3 text-slate-400 font-mono">${item.draw}</td>
                    <td class="py-2.5 px-3 text-slate-400 font-mono">${item.actualWeight}</td>
                    <td class="py-2.5 px-3 font-mono text-amber-400 font-bold">${item.rating}</td>
                    <td class="py-2.5 px-3 text-slate-400 font-mono text-xs">${item.horseWeight}</td>
                </tr>
            `).join('');
        }

        function renderCards(list) {
            if (!list || list.length === 0) return;
            const first = list[0];
            const venueName = first.racecourse === 'HV' ? '跑馬地' : (first.racecourse === 'ST' ? '沙田' : first.racecourse);
            const meetingType = first.racecourse === 'HV' ? '夜賽' : '日賽';
            const distinctRaces = [...new Set(list.map(i => i.raceNo))].sort((a, b) => a - b);

            const titleEl = document.getElementById('raceCardTitle');
            if (titleEl) {
                titleEl.textContent = `${venueName}${meetingType}（${first.raceDate}）官方最新排位表`;
            }
            const subEl = document.getElementById('raceCardSubtitle');
            if (subEl) {
                subEl.textContent = `共 ${distinctRaces.length} 場賽事 · 已成功抓取 ${list.length} 匹出賽賽駒排位、評分、負磅與檔位 · 頭場預計開跑 19:10`;
            }

            const selectEl = document.getElementById('cardRaceSelect');
            if (selectEl && selectEl.options.length <= 1) {
                selectEl.innerHTML = `<option value="ALL">全部 ${distinctRaces.length} 場賽事</option>` +
                    distinctRaces.map(r => `<option value="${r}">第 ${r} 場</option>`).join('');
            }

            renderCardsTable(list);
        }

        async function fetchAnalyticsStats() {
            try {
                const res = await fetch('/api/analytics/stats?_t=' + Date.now());
                if (res.ok) {
                    const stats = await res.json();
                    const badgeText = document.getElementById('visitorCountText');
                    if (badgeText) {
                        badgeText.textContent = `在線: ${stats.activeUsersCount} 人 | 今日: ${stats.todayUniqueVisitors} 人`;
                    }
                    return stats;
                }
            } catch (e) {
                console.warn("[Analytics] 讀取訪客統計失敗:", e);
            }
            return null;
        }

        function openAnalyticsModal() {
            const modal = document.getElementById('analyticsModal');
            if (modal) {
                modal.classList.remove('hidden');
                refreshAnalyticsModal();
            }
        }

        function closeAnalyticsModal() {
            const modal = document.getElementById('analyticsModal');
            if (modal) {
                modal.classList.add('hidden');
            }
        }

        function switchAnalyticsSubTab(tabName) {
            const tabVisitors = document.getElementById('subTabVisitors');
            const tabLogs = document.getElementById('subTabLogs');
            const btnVisitors = document.getElementById('btnTabVisitors');
            const btnLogs = document.getElementById('btnTabLogs');

            if (tabName === 'visitors') {
                tabVisitors.classList.remove('hidden');
                tabLogs.classList.add('hidden');
                btnVisitors.className = "pb-2 text-sm font-bold border-b-2 border-indigo-500 text-white transition";
                btnLogs.className = "pb-2 text-sm font-medium text-slate-400 hover:text-white transition";
            } else {
                tabVisitors.classList.add('hidden');
                tabLogs.classList.remove('hidden');
                btnLogs.className = "pb-2 text-sm font-bold border-b-2 border-indigo-500 text-white transition";
                btnVisitors.className = "pb-2 text-sm font-medium text-slate-400 hover:text-white transition";
            }
        }

        async function refreshAnalyticsModal() {
            const stats = await fetchAnalyticsStats();
            if (!stats) return;

            document.getElementById('statActiveUsers').textContent = stats.activeUsersCount;
            document.getElementById('statTodayUV').textContent = stats.todayUniqueVisitors;
            document.getElementById('statTodayPV').textContent = stats.todayPageViews;
            document.getElementById('statTotalNodes').textContent = stats.totalTrackedNodes;
            document.getElementById('userCountLabel').textContent = stats.visitors.length;
            document.getElementById('logCountLabel').textContent = stats.recentLogs.length;
            document.getElementById('analyticsUpdateTime').textContent = `更新於 ${stats.serverTime.split(' ')[1]}`;

            const tbody = document.getElementById('visitorTableBody');
            if (stats.visitors && stats.visitors.length > 0) {
                tbody.innerHTML = stats.visitors.map(v => {
                    const statusBadge = v.isActive 
                        ? `<span class="px-2 py-0.5 bg-emerald-950 text-emerald-400 border border-emerald-700/60 rounded-full text-[10px] font-bold animate-pulse">● 活躍在線</span>`
                        : `<span class="px-2 py-0.5 bg-slate-800 text-slate-400 border border-slate-700 rounded-full text-[10px]">離線</span>`;
                    return `<tr class="hover:bg-slate-800/40 transition">
                        <td class="p-2">${statusBadge}</td>
                        <td class="p-2 font-bold text-slate-200">${v.ip}</td>
                        <td class="p-2 text-indigo-300 font-sans">${v.device}</td>
                        <td class="p-2 text-slate-400">${v.firstSeen.substring(5)}</td>
                        <td class="p-2 text-amber-300 font-bold">${v.lastSeen.substring(5)}</td>
                        <td class="p-2 text-slate-300 font-bold">${v.hits}</td>
                        <td class="p-2 text-emerald-400 font-sans truncate max-w-[200px]" title="${v.lastAction}">${v.lastAction}</td>
                    </tr>`;
                }).join('');
            } else {
                tbody.innerHTML = `<tr><td colspan="7" class="p-4 text-center text-slate-500">暫無訪客記錄</td></tr>`;
            }

            const logsContainer = document.getElementById('activityLogsContainer');
            if (stats.recentLogs && stats.recentLogs.length > 0) {
                logsContainer.innerHTML = stats.recentLogs.map(l => {
                    return `<div class="p-2 rounded bg-slate-800/50 hover:bg-slate-800 border border-slate-800 flex items-center justify-between text-xs transition">
                        <div class="flex items-center space-x-2">
                            <span class="text-slate-500">${l.timestamp.substring(5)}</span>
                            <span class="px-1.5 py-0.5 bg-slate-700 text-slate-300 rounded text-[10px]">${l.ip}</span>
                            <span class="text-indigo-400 text-[11px] font-sans">[${l.device}]</span>
                            <span class="text-slate-200 font-sans font-medium">${l.action}</span>
                        </div>
                        <span class="text-slate-500 text-[10px]">${l.path}</span>
                    </div>`;
                }).join('');
            } else {
                logsContainer.innerHTML = `<div class="text-center text-slate-500 p-4">暫無操作日誌</div>`;
            }
        // ==================== 採集控制台與手動抓取邏輯 ====================
        function toggleIntervalInput() {
            const mode = document.getElementById('cfgMode').value;
            const box = document.getElementById('fixedIntervalBox');
            if (mode === 'fixed') {
                box.classList.remove('hidden');
            } else {
                box.classList.add('hidden');
            }
        }

        function setScraperDateToday() {
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, '0');
            const dd = String(today.getDate()).padStart(2, '0');
            document.getElementById('cfgTargetDate').value = `${yyyy}-${mm}-${dd}`;
        }

        async function openScraperModal() {
            document.getElementById('scraperModal').classList.remove('hidden');
            await fetchScraperStatusModal();
        }

        function closeScraperModal() {
            document.getElementById('scraperModal').classList.add('hidden');
        }

        async function fetchScraperStatusModal() {
            try {
                const res = await fetch('/api/scraper/status?_t=' + Date.now());
                const data = await res.json();
                
                document.getElementById('statLastRunTime').innerText = data.lastRunTime || '尚未運行';
                document.getElementById('statCurrentInterval').innerText = `${data.currentInterval || 60} 秒`;
                document.getElementById('statTotalRuns').innerText = `${data.totalRuns || 0} 次`;
                document.getElementById('statTotalSaved').innerText = `${(data.totalRecordsSaved || 0).toLocaleString()} 筆`;
                document.getElementById('statLastStatus').innerText = data.lastStatus || '待命';
                
                const badge = document.getElementById('scraperModalBadge');
                if (data.enabled) {
                    badge.innerText = `🟢 自動運行中 (${data.mode === 'smart' ? '智能' : data.currentInterval + 's'})`;
                    badge.className = "text-xs font-normal text-emerald-400 bg-emerald-950/80 px-2 py-0.5 rounded-full border border-emerald-800/60";
                } else {
                    badge.innerText = `⏸️ 已暫停`;
                    badge.className = "text-xs font-normal text-amber-400 bg-amber-950/80 px-2 py-0.5 rounded-full border border-amber-800/60";
                }

                if (data.targetDate) {
                    document.getElementById('cfgTargetDate').value = data.targetDate;
                }
                if (data.targetVenue) {
                    document.getElementById('cfgTargetVenue').value = data.targetVenue;
                }
                if (data.maxRaces) {
                    document.getElementById('cfgMaxRaces').value = String(data.maxRaces);
                }
                if (data.mode) {
                    document.getElementById('cfgMode').value = data.mode;
                    toggleIntervalInput();
                }
                if (data.fixedInterval) {
                    document.getElementById('cfgFixedInterval').value = String(data.fixedInterval);
                }
                document.getElementById('cfgEnabled').value = data.enabled ? "1" : "0";
            } catch (err) {
                console.error("獲取採集器狀態失敗:", err);
            }
        }

        async function triggerManualScrape() {
            const btn = document.getElementById('btnTriggerScraper');
            const icon = document.getElementById('btnTriggerIcon');
            const text = document.getElementById('btnTriggerText');
            const alertBox = document.getElementById('scrapeResultAlert');

            btn.disabled = true;
            btn.classList.add('opacity-70', 'cursor-not-allowed');
            icon.innerText = "⏳";
            text.innerText = "正在極速抓取 1~11 場實盤數據...";
            alertBox.classList.add('hidden');

            try {
                const res = await fetch('/api/scraper/trigger?force=1&_t=' + Date.now());
                const resData = await res.json();
                const r = resData.result || {};

                alertBox.classList.remove('hidden');
                if (r.success) {
                    alertBox.className = "mt-3 p-3 rounded-lg text-xs font-mono bg-emerald-950/80 border border-emerald-700/80 text-emerald-300";
                    alertBox.innerHTML = `
                        <div class="font-bold text-sm mb-1">🎉 實盤數據抓取成功！</div>
                        <div>• 目標賽期: <span class="text-white font-bold">${r.targetDate} (${r.targetVenue === 'HV' ? '跑馬地' : '沙田'})</span></div>
                        <div>• 抓取場次: <span class="text-amber-300 font-bold">第 1 至 ${r.maxRaces} 場 (已覆蓋全日賽程)</span></div>
                        <div>• 本次入庫: 獨贏 <b>${r.winCount}</b> 條 · 連贏 <b>${r.qinCount}</b> 條 · 位置Q <b>${r.qplCount}</b> 條 (共計 ${r.totalSaved} 條)</div>
                        <div class="text-slate-400 mt-1 text-[11px]">${r.status || ''}</div>
                    `;
                } else {
                    alertBox.className = "mt-3 p-3 rounded-lg text-xs font-mono bg-amber-950/80 border border-amber-700/80 text-amber-300";
                    alertBox.innerHTML = `
                        <div class="font-bold text-sm mb-1">⚠️ 抓取已執行完畢</div>
                        <div>• 原因/狀態: <span class="text-white">${r.reason || r.error || '彩池暫未開盤或非受注時間'}</span></div>
                        <div class="text-slate-400 mt-1 text-[11px]">賽期: ${r.targetDate || ''} ${r.targetVenue || ''}</div>
                    `;
                }

                await fetchScraperStatusModal();
                // 刷新主看板
                await checkAndAutoRefresh();
                await initPage();
            } catch (err) {
                alertBox.classList.remove('hidden');
                alertBox.className = "mt-3 p-3 rounded-lg text-xs font-mono bg-rose-950/80 border border-rose-700/80 text-rose-300";
                alertBox.innerHTML = `❌ 請求失敗: ${err.message}`;
            } finally {
                btn.disabled = false;
                btn.classList.remove('opacity-70', 'cursor-not-allowed');
                icon.innerText = "🚀";
                text.innerText = "再次抓取";
            }
        }

        async function saveScraperConfig() {
            const targetDate = document.getElementById('cfgTargetDate').value.trim();
            const targetVenue = document.getElementById('cfgTargetVenue').value;
            const maxRaces = document.getElementById('cfgMaxRaces').value;
            const mode = document.getElementById('cfgMode').value;
            const interval = mode === 'fixed' ? document.getElementById('cfgFixedInterval').value : '60';
            const enabled = document.getElementById('cfgEnabled').value;

            let url = `/api/scraper/config?mode=${mode}&interval=${interval}&enabled=${enabled}&maxRaces=${maxRaces}`;
            if (targetDate) {
                url += `&targetDate=${encodeURIComponent(targetDate)}`;
            }
            if (targetVenue) {
                url += `&targetVenue=${encodeURIComponent(targetVenue)}`;
            }

            try {
                const res = await fetch(url);
                const updated = await res.json();
                await fetchScraperStatusModal();
                alert(`✅ 採集設定已成功保存並立即生效！\n\n• 目標賽期: ${updated.targetDate || '自動嗅探'}\n• 賽事場地: ${updated.targetVenue || '自動'}\n• 抓取場次上限: 1 ~ ${updated.maxRaces} 場\n• 工作模式: ${updated.mode}\n• 輪詢間隔: ${updated.currentInterval} 秒`);
            } catch (err) {
                alert(`❌ 保存設定失敗: ${err.message}`);
            }
        }

        let lastSeenTimestamp = null;
        async function checkAndAutoRefresh() {
            try {
                fetchAnalyticsStats();
                const res = await fetch('/api/timestamps?_t=' + Date.now());
                const tsList = await res.json();
                if (tsList && tsList.length > 0) {
                    const latest = tsList[0];
                    const badge = document.getElementById('liveStatusBadge');
                    if (badge) {
                        badge.innerHTML = `<span class="w-2 h-2 bg-emerald-400 rounded-full animate-pulse mr-2"></span>🟢 實盤全自動同步中 · 最新時刻: <span class="font-mono font-bold text-white ml-1">${latest}</span>`;
                    }

                    if (lastSeenTimestamp && latest !== lastSeenTimestamp) {
                        console.log("[AutoRefresh] 偵測到新盤口時間戳:", latest);
                        allTimestamps = tsList;
                        const selectedDate = document.getElementById('compDateSelect') ? document.getElementById('compDateSelect').value : '';
                        const selectedRaceNo = parseInt(document.getElementById('compRaceNo').value, 10) || 1;
                        updateTimeSelectors(selectedDate, selectedRaceNo, true);

                        await loadCompareData();
                        if (!document.getElementById('matrixTab').classList.contains('hidden')) {
                            await loadMatrixData();
                        }
                    }
                    lastSeenTimestamp = latest;
                }
            } catch (err) {
                console.warn("[AutoRefresh] 輪詢失敗:", err);
            }
        }

        window.onload = async () => {
            await initPage();
            await fetchAnalyticsStats();
            if (allTimestamps.length > 0) {
                lastSeenTimestamp = allTimestamps[0];
            }
            setInterval(checkAndAutoRefresh, 15000);
        };
    </script>
</body>
</html>
"""

class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        if args and len(args) > 0 and "/health" in str(args[0]):
            return
        forwardedFor = self.headers.get("X-Forwarded-For")
        clientIp = forwardedFor.split(",")[0].strip() if forwardedFor else self.headers.get("X-Real-IP", self.client_address[0])
        sys.stdout.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{clientIp}] {format % args}\n")

    def sendJsonResponse(self, data):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(encoded)

    def sendHtmlResponse(self, htmlStr):
        encoded = htmlStr.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        try:
            parsedUrl = urllib.parse.urlparse(self.path)
            path = parsedUrl.path
            queryParams = urllib.parse.parse_qs(parsedUrl.query)

            forwardedFor = self.headers.get("X-Forwarded-For")
            clientIp = forwardedFor.split(",")[0].strip() if forwardedFor else self.headers.get("X-Real-IP", self.client_address[0])
            userAgent = self.headers.get("User-Agent", "")
            visitorTracker.recordVisit(clientIp, userAgent, path, queryParams)
            
            if path == "/" or path == "/index.html":
                self.sendHtmlResponse(htmlTemplate)
            elif path == "/health":
                self.sendHtmlResponse("OK")
            elif path == "/api/analytics/stats":
                self.sendJsonResponse(visitorTracker.getStats())
            elif path == "/api/timestamps":
                data = queryDistinctTimestamps()
                self.sendJsonResponse(data)
            elif path == "/api/compare":
                pool = queryParams.get("pool", ["WIN"])[0]
                raceNo = int(queryParams.get("raceNo", [1])[0])
                time1 = queryParams.get("time1", [None])[0]
                time2 = queryParams.get("time2", [None])[0]
                data = queryCompareData(pool, raceNo, time1, time2)
                self.sendJsonResponse(data)
            elif path == "/api/matrix":
                pool = queryParams.get("pool", ["QIN"])[0]
                raceNo = int(queryParams.get("raceNo", [1])[0])
                timeStr = queryParams.get("time", [None])[0]
                data = queryMatrixData(pool, raceNo, timeStr)
                self.sendJsonResponse(data)
            elif path == "/api/results":
                data = querySeasonResults()
                self.sendJsonResponse(data)
            elif path == "/api/racecards":
                data = queryRaceCards()
                self.sendJsonResponse(data)
            elif path == "/api/scraper/status":
                self.sendJsonResponse(getScraperStatus())
            elif path == "/api/scraper/config":
                mode = queryParams.get("mode", [None])[0]
                interval = queryParams.get("interval", [None])[0]
                enabled = queryParams.get("enabled", [None])[0]
                targetDate = queryParams.get("targetDate", [None])[0]
                targetVenue = queryParams.get("targetVenue", [None])[0]
                maxRaces = queryParams.get("maxRaces", [None])[0]
                if enabled is not None:
                    enabled = enabled.lower() in ["1", "true", "yes"]
                updated = updateScraperConfig(mode, interval, enabled, targetDate, targetVenue, maxRaces)
                self.sendJsonResponse(updated)
            elif path == "/api/scraper/trigger":
                force = queryParams.get("force", ["0"])[0] in ["1", "true", "yes"]
                res = runScraperCycle(force=force)
                status = getScraperStatus()
                self.sendJsonResponse({"status": status, "result": res})
            elif path == "/api/db/status":
                self.sendJsonResponse(dbAdapter.getDatabaseStatus())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as err:
            print(f"[HTTP Error] Error handling GET {self.path}: {err}")
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

def runServer():
    startBackgroundWorkers()
    http.server.ThreadingHTTPServer.allow_reuse_address = False
    with http.server.ThreadingHTTPServer(("", portNumber), CustomHandler) as httpd:
        print(f"=== hsjc 2.0 Cloud Web Service Active on Port {portNumber} ===")
        httpd.serve_forever()

if __name__ == "__main__":
    runServer()
