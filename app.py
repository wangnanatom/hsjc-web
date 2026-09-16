import http.server
import socketserver
import json
import sqlite3
import os
import sys
import urllib.parse
from datetime import datetime

# 确保控制台 UTF-8 输出且行缓冲实时刷新
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
from scraperWorker import startBackgroundWorkers, getScraperStatus, updateScraperConfig, runScraperCycle

baseDir = os.path.dirname(os.path.abspath(__file__))
dbPath = os.path.join(baseDir, "hsjc.db")
portNumber = int(os.environ.get("PORT", 5050))

def getDbConnection():
    conn = sqlite3.connect(dbPath, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

def queryDistinctTimestamps():
    timestamps = []
    try:
        conn = getDbConnection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT CollectionDateTime FROM win ORDER BY CollectionDateTime DESC LIMIT 500;")
        rows = cursor.fetchall()
        timestamps = [r[0] for r in rows if r[0]]
        conn.close()
    except Exception as err:
        print("Error querying timestamps:", err)
    return timestamps

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
        conn = getDbConnection()
        cursor = conn.cursor()
        time1 = (time1 or "").strip()
        time2 = (time2 or "").strip()
        if not time1 or not time2 or time1 == "undefined" or time2 == "undefined":
            cursor.execute(f"SELECT DISTINCT CollectionDateTime FROM {tableName} ORDER BY CollectionDateTime DESC LIMIT 2;")
            recentRows = cursor.fetchall()
            if len(recentRows) >= 2:
                time2 = recentRows[0][0]
                time1 = recentRows[1][0]
            elif len(recentRows) == 1:
                time2 = recentRows[0][0]
                time1 = recentRows[0][0]
            else:
                time1 = "2026-09-06 11:30:00"
                time2 = "2026-09-06 18:00:00"
        
        # Query Time 1
        sql1 = f"SELECT Number, {oddsCol}, Scratched, Hot FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
        cursor.execute(sql1, (time1, raceNo))
        t1Dict = {str(r[0]): {"odds": float(r[1]) if r[1] else 0.0, "scratched": r[2], "hot": r[3]} for r in cursor.fetchall()}

        # Query Time 2
        sql2 = f"SELECT Number, {oddsCol}, Scratched, Hot FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
        cursor.execute(sql2, (time2, raceNo))
        t2Dict = {str(r[0]): {"odds": float(r[1]) if r[1] else 0.0, "scratched": r[2], "hot": r[3]} for r in cursor.fetchall()}

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
        conn.close()
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
        conn = getDbConnection()
        cursor = conn.cursor()
        timeStr = (timeStr or "").strip()
        if not timeStr or timeStr == "undefined":
            cursor.execute(f"SELECT DISTINCT CollectionDateTime FROM {tableName} ORDER BY CollectionDateTime DESC LIMIT 1;")
            r = cursor.fetchone()
            if r:
                timeStr = r[0]

        if timeStr:
            sql = f"SELECT Number, {oddsCol}, OddsDrop FROM {tableName} WHERE CollectionDateTime = ? AND RaceNo = ?;"
            cursor.execute(sql, (timeStr, raceNo))
            for row in cursor.fetchall():
                numStr = str(row[0])
                oddsVal = float(row[1]) if row[1] else 0.0
                matrixData[numStr] = {
                    "odds": oddsVal,
                    "oddsDrop": float(row[2]) if row[2] else 0.0
                }
        conn.close()
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
                </div>
                <p class="text-xs text-slate-400">香港職業賽馬大戶暗盤資金流向 · 2D 矩陣深度監控終端</p>
            </div>
        </div>

        <div class="flex items-center space-x-2 mt-2 sm:mt-0">
            <button onclick="switchTab('compareTab')" id="btnCompare" class="tab-btn px-4 py-2 rounded-lg text-sm font-bold bg-blue-600 text-white shadow-md shadow-blue-900/40">雙時刻落飛比對 (ResultForm)</button>
            <button onclick="switchTab('matrixTab')" id="btnMatrix" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">2D 組合矩陣 (CompareForm)</button>
            <button onclick="switchTab('resultsTab')" id="btnResults" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">賽事結果復盤</button>
            <button onclick="switchTab('racecardTab')" id="btnRacecard" class="tab-btn px-4 py-2 rounded-lg text-sm font-semibold bg-slate-800 text-slate-300 hover:bg-slate-700">最新排位表 (RaceCard)</button>
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
                            <option value="9">第 9 場 (11匹·55組)</option>
                            <option value="10">第 10 場 (13匹·78組)</option>
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
        </section>
    </main>

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

        let lastSeenTimestamp = null;
        async function checkAndAutoRefresh() {
            try {
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
            
            if path == "/" or path == "/index.html":
                self.sendHtmlResponse(htmlTemplate)
            elif path == "/health":
                self.sendHtmlResponse("OK")
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
                if enabled is not None:
                    enabled = enabled.lower() in ["1", "true", "yes"]
                updated = updateScraperConfig(mode, interval, enabled, targetDate, targetVenue)
                self.sendJsonResponse(updated)
            elif path == "/api/scraper/trigger":
                runScraperCycle()
                self.sendJsonResponse(getScraperStatus())
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
