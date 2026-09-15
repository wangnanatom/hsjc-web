import os
import sys
import json
import ssl
import re
import urllib.request
from bs4 import BeautifulSoup

# Ensure UTF-8 stdout
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

baseDir = os.path.dirname(os.path.abspath(__file__))
dataDir = os.path.join(baseDir, "data")
racecardsFile = os.path.join(dataDir, "racecards.json")
metaFile = os.path.join(dataDir, "race_meta.json")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

def fetchHtml(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, context=ctx, timeout=15) as res:
        return res.read().decode("utf-8", errors="ignore")

def extractMeetingMeta(soup):
    """提取赛期、场地、头场开跑时间等元数据"""
    meta = {
        "raceDate": "2026/09/16",
        "racecourse": "HV",
        "venueName": "跑馬地",
        "meetingType": "夜賽",
        "firstRaceTime": "19:10",
        "totalRaces": 8
    }

    # 寻找开跑时间与赛地文本，例如：2026年9月16日, 星期三, 跑馬地, 19:10
    textBlocks = soup.find_all("div", class_="f_fs13")
    for block in textBlocks:
        txt = block.get_text()
        dateMatch = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', txt)
        if dateMatch:
            y, m, d = dateMatch.groups()
            meta["raceDate"] = f"{y}/{int(m):02d}/{int(d):02d}"
        if "跑馬地" in txt:
            meta["racecourse"] = "HV"
            meta["venueName"] = "跑馬地"
            meta["meetingType"] = "夜賽"
        elif "沙田" in txt:
            meta["racecourse"] = "ST"
            meta["venueName"] = "沙田"
            meta["meetingType"] = "日賽"
        timeMatch = re.search(r'(\d{1,2}:\d{2})', txt)
        if timeMatch:
            meta["firstRaceTime"] = timeMatch.group(1)
        break

    return meta

def parseStarterTable(soup, raceNo, raceDate, racecourse):
    """解析单场 table.starter 中的全部马匹排位"""
    horses = []
    table = soup.find("table", class_="starter")
    if not table:
        return horses

    rows = table.find_all("tr")
    if len(rows) <= 1:
        return horses

    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all(["td", "th"])]
        if len(cells) < 14:
            continue
        horseNo = cells[0]
        if not horseNo.isdigit():
            continue

        item = {
            "raceDate": raceDate,
            "racecourse": racecourse,
            "raceNo": raceNo,
            "horseNo": horseNo,
            "last6Runs": cells[1] if len(cells) > 1 else "",
            "horseName": cells[3] if len(cells) > 3 else "",
            "brandNo": cells[4] if len(cells) > 4 else "",
            "actualWeight": cells[5] if len(cells) > 5 else "",
            "jockey": cells[6] if len(cells) > 6 else "",
            "overweight": cells[7] if len(cells) > 7 else "",
            "draw": cells[8] if len(cells) > 8 else "",
            "trainer": cells[9] if len(cells) > 9 else "",
            "rating": cells[11] if len(cells) > 11 else "",
            "horseWeight": cells[13] if len(cells) > 13 else ""
        }
        horses.append(item)

    return horses

def scrapeAllRaceCards():
    print("[Crawler] Starting HKJC RaceCard crawler...")
    os.makedirs(dataDir, exist_ok=True)

    # 1. 获取第一场页面
    baseUrl = "https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx"
    firstPageHtml = fetchHtml(f"{baseUrl}?RaceNo=1")
    firstSoup = BeautifulSoup(firstPageHtml, "html.parser")

    meta = extractMeetingMeta(firstSoup)
    print(f"[Crawler] Detected Meeting: {meta['raceDate']} ({meta['venueName']} - {meta['racecourse']}), First Race: {meta['firstRaceTime']}")

    # 探测总场次
    raceLinks = firstSoup.find_all("a", href=True)
    raceNumbers = {1}
    for a in raceLinks:
        m = re.search(r'RaceNo=(\d+)', a["href"])
        if m:
            raceNumbers.add(int(m.group(1)))
    totalRaces = max(raceNumbers) if raceNumbers else 8
    meta["totalRaces"] = totalRaces
    print(f"[Crawler] Total races detected: {totalRaces} (Races: {sorted(list(raceNumbers))})")

    allHorses = []
    # 2. 依次抓取各场次排位
    for rNo in range(1, totalRaces + 1):
        print(f"[Crawler] Fetching Race {rNo}/{totalRaces}...")
        try:
            if rNo == 1:
                soup = firstSoup
            else:
                html = fetchHtml(f"{baseUrl}?RaceNo={rNo}")
                soup = BeautifulSoup(html, "html.parser")
            raceHorses = parseStarterTable(soup, rNo, meta["raceDate"], meta["racecourse"])
            print(f"  -> Race {rNo}: {len(raceHorses)} horses parsed.")
            allHorses.extend(raceHorses)
        except Exception as e:
            print(f"  -> Error parsing Race {rNo}: {e}")

    meta["totalHorses"] = len(allHorses)

    # 3. 写入文件
    with open(racecardsFile, "w", encoding="utf-8") as f:
        json.dump(allHorses, f, ensure_ascii=False, indent=2)
    print(f"[Crawler] Successfully wrote {len(allHorses)} horses to {racecardsFile}")

    with open(metaFile, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[Crawler] Successfully wrote meeting metadata to {metaFile}")

    return meta, allHorses

if __name__ == "__main__":
    scrapeAllRaceCards()
