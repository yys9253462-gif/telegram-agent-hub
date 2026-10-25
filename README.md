# 电报双智能体工作台

用 Telegram 私聊遥控服务器上的 AI 编程助手。支持 Claude Code 与 CodeBuddy Code 双执行器、持久任务队列、停止任务、独立会话与摘要交接、模型搜索收藏、文件收发、语音转写确认、图片理解、自然语言生图，以及富文本流式回复。

装上之后，你在手机电报里发一句「帮我查一下 Nginx 为什么 502」，机器人就在服务器上真正执行并回传结果。

## 一条命令安装

在一台 **Debian 12 / Ubuntu 22.04 / 24.04** 服务器上，用 root 执行：

```sh
curl -fsSL https://raw.githubusercontent.com/yys9253462-gif/telegram-agent-hub/main/get.sh | bash
```

脚本会自动检查系统、安装依赖，然后进入**交互式问答**，你只需要按提示粘贴配置即可，不用手改任何配置文件。

开始前准备好这四样：

| 配置 | 从哪里拿 |
|---|---|
| 电报机器人令牌 | 对 Telegram 的 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示创建 |
| 你的数字用户编号 | 对 [@userinfobot](https://t.me/userinfobot) 发送任意消息，它会告诉你 `id` |
| 模型网关地址 | 任何兼容 Anthropic 或 OpenAI 协议的网关，形如 `https://你的网关`（不带 `/v1`） |
| 模型网关密钥 | 上面那个网关提供的 API key |

密钥输入时不回显、不写入命令历史。可选项（语音识别、生图通道、远程 SSH 目标、免密提权）安装时会逐个询问，可以直接回车跳过。

安装完成后，在电报里给你的机器人发 `/menu` 即可看到菜单。

## 安装后常用操作

```sh
# 查看运行状态
sudo python3 /opt/telegram-agent-hub/install.py --status

# 重新配置（会继承已有配置，密钥回车即保留）
curl -fsSL https://raw.githubusercontent.com/yys9253462-gif/telegram-agent-hub/main/get.sh | bash

# 卸载（停服务、撤销提权规则，保留配置与数据）
sudo python3 /opt/telegram-agent-hub/install.py --uninstall
```

服务名是 `telegram-agent-hub`，日志用 `journalctl -u telegram-agent-hub -f` 查看。

## 环境要求

- Debian 12，或 Ubuntu 22.04 / 24.04，使用 systemd。
- x86_64 或 arm64。
- 至少 2 GiB 内存、5 GiB 空闲磁盘。
- 能访问 Telegram、你的模型网关，以及官方 CLI 下载源。
- **不需要**开放任何入站端口，**不需要**域名和网页反代。
- 安装用 root 或可 sudo 的账号；机器人本身以专用普通账号 `agentbot` 运行，不以 root 运行。

## 数据与权限

- 程序目录：`/opt/telegram-agent-hub`
- 受限配置：`/etc/telegram-agent-hub`（权限 640，属主 root）
- 运行用户：`agentbot`，工作目录 `/home/agentbot/tasks`
- 共享记忆与技能：`/home/agentbot/memory`

说明几点边界，避免误解：

- 两种执行器的原生会话**分别保存**，通过最近任务摘要做交接，不是完整的原生历史互通。
- 「目标机器」是行为指令，不是安全沙箱。要真正隔离，请用独立账号或独立服务器。
- 可选的免密 sudo 等于把本机管理员权限交给机器人，安装时需要你明确回答。
- 远程 SSH 公钥必须由远端管理员自行加入授权，安装器不会绕过远端登录授权。

## 使用要点

发送 `/menu` 展开主菜单。直接输入任务即进入队列；任务执行期间仍可查看当前任务、停止或排队。停止**不会回滚**已执行的操作。同步网络请求可能要等到超时才能记录取消；已经提交给上游的生图请求不保证停止计费。

语音先转写成文字，再由你按任务编号确认、修改或取消。普通文件上传只保存，不自动执行。发送文件时只允许任务目录内的普通产物，并排除常见密钥与凭据文件——但规则扫描不可能识别所有秘密。

生图走独立的兼容 `/images/generations` 通道，要求返回内联图片数据。配置好之后，直接说「画一只趴在窗边的橘猫」就行，机器人会自动调用工具出图并回传，不需要记特定指令。

工具审计只记录工具名称和任务结果，不提供完整文件差异或一键回滚。

## 外部平台授权（GitHub / Cloudflare）

让机器人在任务里代替你操作 GitHub 与 Cloudflare。入口在「更多 → 平台授权」。

**GitHub 走设备码流程**，不需要回调地址，也不需要 client secret。**开箱即用：不用注册任何 OAuth App。** 点「授权GitHub」后机器人直接给一条消息，里面是「点一下就进授权页」的链接和「点一下就复制」的 8 位码，你点链接、粘贴码、确认，机器人自己等到授权完成并通知你，不需要回填任何东西。

之所以能免注册：设备码流程只要 client_id（GitHub 官方文档明确写着不需要 secret），而 client_id 本身就是公开标识，所以代码里预置了一个公共 App 的 client_id 作为默认值。想独享每小时 50 次的设备码配额，或换成自己名下的 App，注册一个 OAuth App 并勾选 **Enable Device Flow**，把 client_id 填到安装配置的 `github_client_id` 覆盖即可。

**Cloudflare 也走设备码流程，同样开箱即用。** 点「授权Cloudflare」后机器人给一条消息，链接里**已经带好了用户码**，你点进去只剩一个「允许」按钮，确认完机器人自己收到令牌并通知你。用的是 wrangler（Cloudflare 官方 CLI）的公开 client_id，不需要你去注册 OAuth 客户端。

有两点和 GitHub 不同，值得知道：用户码只有 **5 分钟**（GitHub 是 15 分钟），过期就再发一次；换回来的是 **OAuth 访问令牌**，约几天后过期，会附带 refresh_token，但当前版本不自动续期，过期后重新授权即可。

如果只想要某个站点的最小权限、或者不想要会过期的令牌，走**备选路径**：菜单里的「云flare粘贴令牌」，到控制台「我的个人资料 → API 令牌」按需勾权限，把整段令牌发给机器人。不接受账号密码和 Global API Key。

凭据去向：

- 只存在 `~/tasks/credentials.json`，权限 0600，不进 git、不进任务记录、不进日志。
- 只在**新任务启动时**以子进程环境变量注入：GitHub → `GH_TOKEN`，Cloudflare → `CLOUDFLARE_API_TOKEN`。任务结束即消失，不写进任何 shell 启动文件。
- 未授权的平台会把它那一族环境变量一起剔除（含 `GITHUB_TOKEN`、`CLOUDFLARE_API_KEY` 等），避免任务静默用到服务器上残留的别人的令牌。
- 「授权状态」只显示指纹与作用范围，任何情况下都不显示令牌原文。
- 「撤销授权」只删本地那份，**平台侧的授权仍然存在**，需要在 GitHub 的 Authorized OAuth Apps 或 Cloudflare 的令牌列表里另行撤销。

边界说明：GitHub 的设备码有效期 15 分钟（同一 OAuth App 每小时最多申请 50 次）；Cloudflare 设备码只有 5 分钟。两者都是长期凭据或数天有效的令牌，机器人侧无法感知它在平台被吊销，只会在下次调用时失败——所以「授权状态」显示的是**有没有保存**，不是**现在一定能用**。

Cloudflare 这条路径有一个环境前提：请求发往 `dash.cloudflare.com`，该域名前置了 Cloudflare 自己的机器人防护，**不带 User-Agent 的请求会被 403 拦截**（与出口 IP 无关）。代码里统一带 wrangler 的 UA，若你把它改掉，授权会在申请设备码这一步就失败，且报错看起来像网络问题。

## 仓库里没有什么

为了安全，这个仓库**不包含**任何个人记忆、运行日志、原生会话、SSH 私钥、模型配置、桌面登录态或真实主机地址。装完之后所有配置都在你自己的机器上。

想迁移自己的技能和记忆，用你自己持有的私有素材包，不要把运行数据提交到仓库。

## 从源码安装与自测

```sh
git clone https://github.com/yys9253462-gif/telegram-agent-hub.git
cd telegram-agent-hub
sudo bash install.sh            # 交互式安装
python3 test_project.py         # 源码回归测试
python3 test_hardening.py       # 五轮排查修复项的反向验证用例
python3 test_codex_engine.py    # Codex 执行器参数与环境变量映射
python3 test_installer.py       # 安装器配置校验与代码生成
python3 test_menu_layout.py     # 键盘按钮与分组菜单一致性
python3 test_auth_channel.py    # 网关/账号双渠道切换
python3 test_platform_auth.py   # GitHub/Cloudflare 授权、凭据存取与注入
python3 test_image_tool.py      # 生图 MCP 工具与协议测试
python3 security_check.py       # 提交前的敏感信息扫描
bash -n get.sh && bash -n install.sh
```

`verify_platform_auth.py` 是授权功能的**反向验证脚本**：它把 8 处保护逐条故意改坏，确认 `test_platform_auth.py` 真的报红。永远绿的测试等于没有测试，改动授权相关代码后请跑一遍。

`test_hardening.py` 里每个用例都对应一处真实缺陷，注释写明缺陷现象，并可用「把修复改回原写法、测试必须报红」的方式自检。**改完代码请先删掉 `__pycache__` 再跑测试**——陈旧的 `bot.cpython-*.pyc` 会让测试读到旧字节码，从而给出全绿的假象（本项目踩过）。

`test_integration.py` 只提供绑定回环地址的假消息接口，必须配合独立运行用户与独立服务名使用，不能替代真实端到端验收，也禁止用于正式机器人。

官方执行器安装脚本使用已审阅的固定校验值，上游脚本一变安装器就拒绝执行。这是刻意的设计：安全边界不提供关闭开关。

## 常见问题

**机器人不回复？** 先看 `journalctl -u telegram-agent-hub -n 50`。最常见的是令牌错、`owner ID` 填成了别人的、或者服务器连不上 Telegram。

**想换模型？** 在电报里发「刷新模型」「模型搜索」即可，不用重装。

**能装在 ARM 小鸡上吗？** 可以，但建议先确认内存 ≥ 1 GiB；语音识别模型会额外占用内存，内存紧张就跳过语音功能。

**用私有仓库或自己的 fork？** 克隆自己的仓库后执行 `sudo bash install.sh`。注意 `get.sh` 里固定了官方仓库和提交，换仓库要同步改这两个值。
