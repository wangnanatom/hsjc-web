import os
import sys
import libsql_client

# 解决 Windows 下 Python 3.8 asyncio 退出时的 Event loop is closed 警告
if sys.platform == "win32":
    import asyncio
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

baseDir = os.path.dirname(os.path.abspath(__file__))
defaultDbPath = os.path.join(baseDir, "hsjc.db")

# 支持从系统环境变量或本地 .env 文件读取 Turso 配置
tursoUrl = os.environ.get("TURSO_DATABASE_URL", "").strip()
tursoToken = os.environ.get("TURSO_AUTH_TOKEN", "").strip()

envPath = os.path.join(baseDir, ".env")
if os.path.exists(envPath) and (not tursoUrl or not tursoToken):
    try:
        with open(envPath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TURSO_DATABASE_URL="):
                    tursoUrl = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TURSO_AUTH_TOKEN="):
                    tursoToken = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass

# 核心兼容处理：将 libsql:// 自动标准化为 https:// (走稳健的 Hrana over HTTP 管道，避免 WebSocket 握手 400 异常)
if tursoUrl.startswith("libsql://"):
    tursoUrl = "https://" + tursoUrl[len("libsql://"):]

def isTursoEnabled():
    return bool(tursoUrl and tursoToken)

def getClient():
    if isTursoEnabled():
        return libsql_client.create_client_sync(tursoUrl, auth_token=tursoToken)
    else:
        # Fallback to local SQLite file
        return libsql_client.create_client_sync(f"file:{defaultDbPath}")

INIT_SCHEMAS = [
    """CREATE TABLE IF NOT EXISTS win (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        CollectionDateTime TEXT,
        RaceNo INTEGER,
        Number INTEGER,
        WinOdds REAL,
        Scratched INTEGER DEFAULT 0,
        OddsDrop REAL DEFAULT 0,
        Hot INTEGER DEFAULT 0,
        WillPay TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_win_query ON win (CollectionDateTime, RaceNo);",
    """CREATE TABLE IF NOT EXISTS qin (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        CollectionDateTime TEXT,
        RaceNo INTEGER,
        Number TEXT,
        QinOdds REAL,
        Scratched INTEGER DEFAULT 0,
        OddsDrop REAL DEFAULT 0,
        Hot INTEGER DEFAULT 0,
        WillPay TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_qin_query ON qin (CollectionDateTime, RaceNo);",
    """CREATE TABLE IF NOT EXISTS qpl (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        CollectionDateTime TEXT,
        RaceNo INTEGER,
        Number TEXT,
        QplOdds REAL,
        Scratched INTEGER DEFAULT 0,
        OddsDrop REAL DEFAULT 0,
        Hot INTEGER DEFAULT 0,
        WillPay TEXT
    );""",
    "CREATE INDEX IF NOT EXISTS idx_qpl_query ON qpl (CollectionDateTime, RaceNo);"
]

def initDatabase():
    """統一自愈初始化：無論連接本地 SQLite 還是全新 Turso 雲庫，啟動時均自動建表與建立索引"""
    try:
        client = getClient()
        try:
            for sql in INIT_SCHEMAS:
                try:
                    client.execute(sql)
                except Exception:
                    pass
        finally:
            client.close()
    except Exception as err:
        print("[dbAdapter] Database schema auto-init error:", err)

initDatabase()

def queryAll(sql, params=None):
    client = getClient()
    try:
        rs = client.execute(sql, params or [])
        cols = rs.columns
        return [dict(zip(cols, row)) for row in rs.rows]
    finally:
        client.close()

def queryOne(sql, params=None):
    client = getClient()
    try:
        rs = client.execute(sql, params or [])
        if rs.rows:
            return dict(zip(rs.columns, rs.rows[0]))
        return None
    finally:
        client.close()

def execute(sql, params=None):
    client = getClient()
    try:
        return client.execute(sql, params or [])
    finally:
        client.close()

def executeMany(sql, rows, chunkSize=100):
    if not rows:
        return
    client = getClient()
    try:
        for i in range(0, len(rows), chunkSize):
            chunk = rows[i:i + chunkSize]
            stmts = [libsql_client.Statement(sql, list(r)) for r in chunk]
            client.batch(stmts)
    finally:
        client.close()

def getDatabaseStatus():
    if isTursoEnabled():
        return {
            "type": "Turso Cloud (LibSQL)",
            "url": tursoUrl,
            "connected": True
        }
    else:
        return {
            "type": "Local SQLite",
            "path": defaultDbPath,
            "connected": True
        }
