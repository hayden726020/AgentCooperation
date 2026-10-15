@echo off
chcp 65001 >nul
title AgentCooperation CLI Console
cd /d "E:\AgentCooperation"
powershell.exe -NoExit -ExecutionPolicy Bypass -File "E:\AgentCooperation\scripts\interactive_cli.ps1"
