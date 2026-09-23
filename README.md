# hsjc 2.0 賽馬實戰雲端戰術看板

面向賽馬數據分析與操盤研究的高頻盤口資金流向監控終端。

## 核心功能
- **雙時刻落飛比對看板 (ResultForm)**：自由選擇任意兩個時間切片，自動計算賠率升跌與資金異動，紅綠雙色狀態燈直觀反映大盤資金流向。
- **2D 組合玩法動態矩陣 (CompareForm)**：$N \times N$ 自適應出賽馬匹網格，展開連贏（QIN）與位置 Q（QPL）二元組合賠率，生成實盤資金變動熱力圖。
- **賽事結果復盤**：往期各場次頭馬派彩、名次與實戰走勢回顧。
- **即時排位表**：即將舉行賽事之出賽馬匹編號、評分與負磅數據呈現。

## 雲端 Render 部署指南

1. 在 GitHub 上新建倉庫（如 `hsjc-web`）。
2. 在本地完成配置並推送代碼：
   ```bash
   git remote add origin https://github.com/wangnanatom/hsjc-web.git
   git branch -M main
   git push -u origin main
   ```
3. 打開 [Render.com](https://dashboard.render.com/)，點擊 **New +** -> **Web Service**。
4. 關聯對應的 GitHub 倉庫。
5. **重要配置**：在 Environment 中配置 `TURSO_DATABASE_URL` 與 `TURSO_AUTH_TOKEN`，以便線上服務接入雲端資料庫。
6. 點擊 **Create Web Service**，Render 將在數分鐘內自動構建並生成專屬的 HTTPS 訪問域名。
