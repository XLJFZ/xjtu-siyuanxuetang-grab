@echo off
REM ============================================================
REM  xjtu-siyuanxuetang-grab 一键启用 GitHub Actions
REM
REM  为什么需要这个：GitHub 对 .github/workflows/ 下的文件有额外权限要求，
REM  用 Contents API（或权限不足的 token）推送会被 403 拒掉。
REM  所以 CI 配置放在 ci.yml.txt，本脚本把它还原到正确位置。
REM
REM  在仓库根目录双击运行即可；启用后 commit + push 就生效了。
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "ci.yml.txt" (
    echo [跳过] 当前目录下没有 ci.yml.txt
    echo.
    echo   如果你是从 Release 压缩包解压出来的，请把本脚本和 ci.yml.txt
    echo   放到仓库根目录再运行。
    goto :end
)

if not exist ".github\workflows" mkdir ".github\workflows"

if exist ".github\workflows\ci.yml" (
    echo [提示] .github\workflows\ci.yml 已存在，覆盖为新版本...
)

copy /y "ci.yml.txt" ".github\workflows\ci.yml" >nul
if errorlevel 1 (
    echo [失败] 复制失败，请检查目录写权限。
    goto :end
)

echo [完成] 已生成 .github\workflows\ci.yml
echo.
echo   接下来：
echo     git add .github/workflows/ci.yml
echo     git commit -m "ci: add workflow"
echo     git push
echo.
echo   推上去之后到仓库 Actions 页面就能看到 CI 跑起来了。
echo   （如果你的 token 有 Workflows: write 权限，也可以直接在本地 push，
echo     因为这是真实的 git 操作而不是 API 调用。）

:end
echo.
pause
endlocal
