@echo off
chcp 65001 >nul
title AgentCooperation CLI 控制台 (阶段五交付版)
cd /d "E:\AgentCooperation"
powershell.exe -NoExit -ExecutionPolicy Bypass -File "E:\AgentCooperation\scripts\interactive_cli.ps1"
