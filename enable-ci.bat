@echo off
REM ============================================================
REM  xjtu-siyuanxuetang-grab —— 一键启用 GitHub Actions
REM
REM  为什么需要这个：GitHub 对 .github/workflows/ 下的文件有额外权限要求
REM  （token 需要 Workflows: Read and write），通过 Contents API 推送会被 403。
REM  所以 CI / Release 工作流以 .txt 形式随包提供，本脚本负责还原到正确位置。
REM
REM  在仓库根目录双击运行即可。
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set DONE=0
set FAIL=0

echo ============================================================
echo   启用 GitHub Actions
echo ============================================================
echo.

if not exist ".github\workflows" mkdir ".github\workflows" 2>nul

REM ---------- 工作流 1：CI ----------
if exist "ci.yml.txt" (
    copy /y "ci.yml.txt" ".github\workflows\ci.yml" >nul 2>&1
    if errorlevel 1 (
        echo   [失败] ci.yml 写入失败，检查目录写权限
        set FAIL=1
    ) else (
        echo   [完成] .github\workflows\ci.yml        测试 + 隐私自检
        set DONE=1
    )
) else (
    echo   [跳过] 没找到 ci.yml.txt
)

REM ---------- 工作流 2：Release ----------
if exist "release.yml.txt" (
    copy /y "release.yml.txt" ".github\workflows\release.yml" >nul 2>&1
    if errorlevel 1 (
        echo   [失败] release.yml 写入失败
        set FAIL=1
    ) else (
        echo   [完成] .github\workflows\release.yml   打 tag 自动发版
        set DONE=1
    )
) else (
    echo   [跳过] 没找到 release.yml.txt
)

REM ---------- Release 工作流依赖的打包脚本 ----------
if exist "pack.py.txt" (
    if not exist ".github\scripts" mkdir ".github\scripts" 2>nul
    copy /y "pack.py.txt" ".github\scripts\pack.py" >nul 2>&1
    if errorlevel 1 (
        echo   [失败] .github\scripts\pack.py 写入失败
        set FAIL=1
    ) else (
        echo   [完成] .github\scripts\pack.py          CI 打包脚本
        set DONE=1
    )
)

echo.
if "%FAIL%"=="1" (
    echo   有步骤失败，请检查目录权限后重跑。
    goto :end
)
if "%DONE%"=="0" (
    echo   没有可安装的内容。
    echo   如果你是从 Release 压缩包解压的，请把本脚本和 *.txt 一起放到仓库根目录再运行。
    goto :end
)

echo   接下来：
echo     git add .github
echo     git commit -m "ci: add workflows"
echo     git push
echo.
echo   推上去之后：
echo     - 每次 push / PR 会自动跑测试（Actions 页可见）
echo     - 打一个 v* 开头的 tag 会自动发 Release

:end
echo.
pause
endlocal
