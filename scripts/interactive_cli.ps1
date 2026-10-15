#requires -Version 5.1
<#
.SYNOPSIS
    Interactive CLI Console for AgentCooperation.
#>

$host.UI.RawUI.WindowTitle = "AgentCooperation CLI Console (Stage 5)"
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot

function Show-Banner {
    Clear-Host
    Write-Host "=========================================================================" -ForegroundColor Cyan
    Write-Host "                🤖 AgentCooperation CLI 控制台 (阶段五交付版)                " -ForegroundColor White
    Write-Host "=========================================================================" -ForegroundColor Cyan
    Write-Host " 工作目录: $projectRoot" -ForegroundColor DarkGray
    Write-Host " 主力研发: Gemini 3.8 Flash High (原生 Antigravity 订阅)" -ForegroundColor DarkGray
    Write-Host " 独立复核: GLM-5.3-FlashX (默认首选) / DeepSeek Flash (可选)" -ForegroundColor DarkGray
    Write-Host "-------------------------------------------------------------------------" -ForegroundColor DarkCyan
    Write-Host " [1] 启动 Hermes 交互对话会话 (coop-dev Bot，支持 /coop 斜杠命令)" -ForegroundColor Yellow
    Write-Host " [2] 运行一次端到端闭环演练 (Mock 快速测试，零费用)" -ForegroundColor Green
    Write-Host " [3] 查看当前月度预算、支出与额度状态 (coop budget)" -ForegroundColor Cyan
    Write-Host " [4] 查询最近 10 条研发任务状态与记录 (coop list)" -ForegroundColor Cyan
    Write-Host " [5] 发起一次真实研发闭环任务 (Gemini 原生研发 + GLM 独立复核)" -ForegroundColor Magenta
    Write-Host " [6] 启动 Web 仪表盘控制台 (http://127.0.0.1:8000)" -ForegroundColor Blue
    Write-Host " [0] 退出控制台，保留在当前 PowerShell" -ForegroundColor Gray
    Write-Host "=========================================================================" -ForegroundColor Cyan
}

$running = $true
while ($running) {
    Show-Banner
    $choice = Read-Host "`n请选择操作编号 [0-6]"

    switch ($choice) {
        "1" {
            Write-Host "`n正在启动 Hermes coop-dev 交互式会话..." -ForegroundColor Yellow
            Write-Host "进入会话后，可输入文本交谈，或使用命令：/coop loop <需求描述> --repo E:\AgentCooperation" -ForegroundColor DarkCyan
            Write-Host "退出会话请输入：/exit`n" -ForegroundColor DarkGray
            & powershell -NoProfile -File (Join-Path $PSScriptRoot 'hermes_bots.ps1') chat coop-dev
            Write-Host "`nHermes 会话已结束。按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "2" {
            Write-Host "`n[演练] 正在执行端到端闭环演练 (Mock 模式，无外部 API 扣费)..." -ForegroundColor Green
            & python (Join-Path $projectRoot 'hermes-agent\plugins\agent_cooperation\cli.py') loop "实现一个字符串反转工具函数" --mock --repo $projectRoot --test-path "tests/test_fsm.py"
            Write-Host "`n演练完成。按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "3" {
            Write-Host "`n[预算与额度状态]:" -ForegroundColor Cyan
            & python (Join-Path $projectRoot 'hermes-agent\plugins\agent_cooperation\cli.py') budget
            Write-Host "`n按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "4" {
            Write-Host "`n[最近研发任务]:" -ForegroundColor Cyan
            & python (Join-Path $projectRoot 'hermes-agent\plugins\agent_cooperation\cli.py') list --limit 10
            Write-Host "`n按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "5" {
            Write-Host "`n[发起真实研发闭环]" -ForegroundColor Magenta
            $req = Read-Host "请输入研发需求描述 (例如: 为 utils 增加版本格式化函数)"
            if (-not [string]::IsNullOrWhiteSpace($req)) {
                $testPath = Read-Host "请输入要验证的局部测试路径 (直接回车默认 tests/test_fsm.py)"
                if ([string]::IsNullOrWhiteSpace($testPath)) {
                    $testPath = "tests/test_fsm.py"
                }
                Write-Host "`n正在启动真实闭环研发 (Gemini 3.8 Flash High + GLM FlashX)..." -ForegroundColor Yellow
                & python (Join-Path $projectRoot 'hermes-agent\plugins\agent_cooperation\cli.py') loop $req --repo $projectRoot --reviewer glm --test-path $testPath
            } else {
                Write-Host "需求描述不能为空。" -ForegroundColor Red
            }
            Write-Host "`n按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "6" {
            Write-Host "`n正在启动 Web 控制台 (访问地址: http://127.0.0.1:8000)..." -ForegroundColor Blue
            Start-Process "http://127.0.0.1:8000"
            & python -m orchestrator.cli web
            Write-Host "`nWeb 服务已停止。按回车返回主菜单..." -ForegroundColor DarkGray
            Read-Host
        }
        "0" {
            Write-Host "`n已退出控制台菜单。你现在可以直接使用 PowerShell 命令行。" -ForegroundColor Green
            Write-Host "快捷指令提示: .\scripts\coop.ps1 --help" -ForegroundColor DarkCyan
            $running = $false
        }
        default {
            Write-Host "无效输入，请输入 0 到 6 之间的数字。" -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
}
