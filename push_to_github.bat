@echo off
chcp 65001 >nul
echo ========================================================
echo   hsjc 2.0 雲端工程一鍵推送至 GitHub (用戶: wangnanatom)
echo ========================================================
echo.

echo 正在檢查並暫存本地變更 (git add) ...
git add .

REM 若有未提交的變更則自動提交
git diff-index --quiet HEAD --
if %ERRORLEVEL% neq 0 (
    echo 檢測到代碼變更，正在自動執行 git commit ...
    git commit -m "update: sync latest codebase and configs"
) else (
    echo 工作區無新變更，跳過 commit。
)

git remote remove origin 2>nul
git remote add origin https://github.com/wangnanatom/hsjc-web.git
git branch -M main

echo 正在推送到 https://github.com/wangnanatom/hsjc-web.git ...
git push -u origin main

if %ERRORLEVEL% equ 0 (
    echo.
    echo [成功] 代碼已成功推送到 GitHub！
    echo 請前往 https://dashboard.render.com/ 關聯倉庫並一鍵上線！
) else (
    echo.
    echo [提示] 請確保您已在 GitHub 上創建了名為 hsjc-web 的新倉庫！
    echo 創建地址: https://github.com/new
)
pause

