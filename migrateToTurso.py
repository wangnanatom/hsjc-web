import os
import sys
import sqlite3
import libsql_client
from dbAdapter import isTursoEnabled, tursoUrl, tursoToken, baseDir, defaultDbPath

def migrateData():
    if not isTursoEnabled():
        print("❌ [Error] 未检测到 Turso 云数据库连接信息！")
        print("请在 .env 文件中配置：")
        print("  TURSO_DATABASE_URL=libsql://your-database.turso.io")
        print("  TURSO_AUTH_TOKEN=your-auth-token")
        return False

    print("=" * 65)
    print("🚀 开始将本地 SQLite (hsjc.db) 全量数据迁移至 Turso 分布式云数据库")
    print("=" * 65)
    print(f"📁 本地数据库文件: {defaultDbPath}")
    print(f"☁️  目标 Turso 节点: {tursoUrl}\n")

    if not os.path.exists(defaultDbPath):
        print(f"❌ 错误: 本地数据库文件不存在: {defaultDbPath}")
        return False

    # 1. 打开本地 SQLite
    localConn = sqlite3.connect(defaultDbPath)
    localCursor = localConn.cursor()

    # 2. 连接 Turso 云端
    tursoClient = libsql_client.create_client_sync(tursoUrl, auth_token=tursoToken)

    tables = ["win", "qin", "qpl"]

    try:
        for table in tables:
            print(f"-----------------------------------------------------------------")
            print(f"📦 [1/3] 正在同步表结构与索引: [{table}] ...")

            # 获取建表 DDL
            localCursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?;", (table,))
            row = localCursor.fetchone()
            if not row or not row[0]:
                print(f"  ⚠️  本地未发现表 {table}，跳过")
                continue

            ddl = row[0]
            if "CREATE TABLE IF NOT EXISTS" not in ddl:
                ddl = ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1)

            tursoClient.execute(ddl)
            print(f"  ✅ 表结构同步就绪: {table}")

            # 获取并同步索引
            localCursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL;", (table,))
            indices = localCursor.fetchall()
            for idxRow in indices:
                idxSql = idxRow[0]
                if "CREATE INDEX IF NOT EXISTS" not in idxSql:
                    idxSql = idxSql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1)
                try:
                    tursoClient.execute(idxSql)
                except Exception:
                    pass
            print(f"  ✅ 索引同步就绪: 共 {len(indices)} 个索引")

            # 3. 读取本地数据并分批推送到 Turso
            localCursor.execute(f"SELECT * FROM {table};")
            rows = localCursor.fetchall()
            totalRows = len(rows)
            print(f"🚚 [2/3] 正在上传全量历史数据: 共 {totalRows:,} 条记录 ...")

            if totalRows > 0:
                localCursor.execute(f"PRAGMA table_info({table});")
                cols = [c[1] for c in localCursor.fetchall()]
                colNames = ", ".join(cols)
                placeholders = ", ".join(["?"] * len(cols))
                insertSql = f"INSERT OR REPLACE INTO {table} ({colNames}) VALUES ({placeholders});"

                chunkSize = 100
                transferred = 0
                for i in range(0, totalRows, chunkSize):
                    chunk = rows[i:i + chunkSize]
                    stmts = [libsql_client.Statement(insertSql, list(r)) for r in chunk]
                    tursoClient.batch(stmts)
                    transferred += len(chunk)
                    progressPct = (transferred / totalRows) * 100
                    sys.stdout.write(f"\r  Progress: [{transferred:,} / {totalRows:,}] ({progressPct:.1f}%) 完成")
                    sys.stdout.flush()

                print(f"\n  🎉 [{table}] 表全量迁移完成！")

        print("=" * 65)
        print("🏆 恭喜！本地所有历史赔率数据已成功无缝迁移至 Turso 云端数据库！")
        print("现在无论是本地还是 Render 云端，都能通过 Turso 查询到完整的历史走势！")
        print("=" * 65)
        return True
    except Exception as e:
        print(f"\n❌ 迁移过程中发生异常: {e}")
        return False
    finally:
        localConn.close()
        tursoClient.close()

if __name__ == "__main__":
    migrateData()
