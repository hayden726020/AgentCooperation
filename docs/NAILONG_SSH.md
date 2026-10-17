# 奶龙 SSH 登录动画

`nailong-ssh` 是运行在 Windows PowerShell 客户端的趣味 SSH 启动器。它不会向 Ubuntu
服务器安装文件，也不会修改 `.bashrc`、MOTD 或 SSH 配置。

## 效果与流程

1. 使用一次只读 SSH 会话获取主机名、Ubuntu 版本、运行时间、负载、内存、根分区和内网 IP。
2. 在 Windows 终端本地播放约 1.7 秒的“奶龙出现 → 捧腹大笑”动画，任意键可跳过。
3. 显示“SPCUTEAG的奶服”和“奶龙已上线，开始工作”。
4. 打开正常的交互式 SSH 会话。

动画会根据终端宽度切换宽屏、紧凑和迷你布局。`NO_COLOR`、`TERM=dumb`、不支持颜色的
终端及重定向输出会自动使用纯文本或静态显示。

## 安装

在准备使用该命令的 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& 'E:\AgentCooperation\scripts\install_nailong_ssh.ps1'
```

重新打开 PowerShell，然后连接：

```powershell
nailong-ssh 192.168.1.20
```

默认用户名是 `spcuteag`，默认端口是 `22`。也可以临时覆盖：

```powershell
nailong-ssh server.example -User spcuteag -Port 2222
nailong-ssh server.example -IdentityFile "$HOME\.ssh\id_ed25519"
```

其他实用参数：

```powershell
nailong-ssh -Preview
nailong-ssh server.example -NoColor
nailong-ssh server.example -NoAnimation
```

Windows PowerShell 5.1 和 PowerShell 7 使用不同的用户 Profile。如果两者都需要该命令，
请分别在对应 Shell 中运行一次安装器。安装器只修改当前用户 Profile，并创建
`*.nailong-ssh.bak` 备份。

## 身份验证说明

该方案会建立两次 SSH 连接：一次读取系统信息，一次进入交互终端。因此密码认证通常会
要求输入两次密码。建议使用 SSH 密钥和 `ssh-agent`；脚本不会读取、缓存或保存密码。

脚本保留 OpenSSH 的主机密钥验证，不会自动接受陌生主机密钥。第一次连接新服务器时，
仍应核对服务器指纹。

服务器信息来自探测瞬间的只读快照。IP 仅从服务器本机接口读取并筛选私网地址，不访问
公网 IP 查询服务。

该工具面向你自己管理的可信服务器。客户端已给正常探测设置连接保活和 8 秒远端超时，
但恶意 SSH 服务仍可能忽略命令并持续输出；不要用它连接来源不明的服务器。

## 卸载

请在之前执行安装的同一种 PowerShell 中运行：

```powershell
& 'E:\AgentCooperation\scripts\install_nailong_ssh.ps1' -Uninstall
```

卸载器只移除自身写入的标记块和已安装脚本，不会覆盖 Profile 中的其他内容。
