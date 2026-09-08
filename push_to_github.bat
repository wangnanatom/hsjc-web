@echo off
chcp 65001 >nul
echo ========================================================
echo   hsjc 2.0 雲端工程一鍵推送至 GitHub (用戶: wangnanatom)
echo ========================================================
echo.

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
