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

安装完成后，在电报里给你的机器人发 `/menu` 即可看到菜单。底部保留常用入口；模型、会话、授权及版本更新使用回复消息下方的按钮，不切换多层底部键盘。点击「模型与会话」直接展示模型、智能体、会话和版本操作，点击「授权管理」直接展示平台授权。旧消息按钮五分钟后或机器人重启后失效，重新打开入口即可。

默认最多同时执行 **3 个任务**，超过上限继续排队。当前会话已有智能体任务时，再发来的任务会使用独立的新会话，避免两个执行器同时写同一份会话记录。查看「查看进度」可列出所有运行任务；只有一个任务时点「停止任务」即可停止，多个任务时发送 `停止任务 任务ID` 精确停止其中一个。

并发上限由服务环境变量 `AGENT_MAX_CONCURRENT` 配置，允许 1–6，默认 3，修改后重启服务。并发会话独立，但仍共用服务器与工作目录；不要让多个任务同时修改同一文件或部署同一服务。

## 检查与更新机器人

点击「模型与会话」，在回复消息下方直接选择「检查版本」或「更新机器人」：

- **检查版本**：显示已安装版本、最新发布版本和本地代码修改情况。
- **更新机器人**：展示更新信息，再点「确认更新机器人」。更新时必须没有运行或排队任务。
- 升级只替换程序源码，不覆盖配置、密钥、平台授权、执行器登录态或任务目录，也不重装执行器。
- 下载采用官方归档并校验发布的 SHA256；更新前自动备份，启动检查失败恢复旧代码。
- 有本地代码修改时拒绝直接覆盖；没有免密管理员权限时，需管理员在终端运行升级命令。

没有菜单升级入口的旧版本需要先由管理员升级一次以接入该功能。已具备升级器的默认安装可运行：

```sh
sudo python3 /opt/telegram-agent-hub/updater.py --apply --app-dir /opt/telegram-agent-hub
```

自定义安装需替换为实际程序目录。发布依据是仓库 `version.txt` 指向的已校验提交，而非每一个尚未定型的提交。

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

## 账号登录与退出

「设置 → 账号登录」里可以登录 Claude、CodeBuddy、Codex 三个执行器的账号，用订阅额度代替接口密钥跑任务。渠道是**按执行器独立**的，切换某一个不会影响另外两个。

退出账号走「设置 → 账号登录 → 退出账号」，列表里只列**当前确实登录着**的执行器，并显示身份，避免退错。退出后：

- 该执行器的渠道若正处于「账号」，会自动切回接口密钥——账号没了还留在账号渠道，任务会拿不到任何凭据直接失败。
- 正在运行的任务不受影响；正在登录中的执行器不允许退出（登录进程最后一步会把凭据写回来）。
- 只清除账号登录态，接口密钥、任务数据、会话记录都不动。

三家 CLI 的退出机制**完全不同**，这里按各家的真实行为实现，不能想当然：

| 执行器 | 官方退出命令 | 实际做法 |
|---|---|---|
| Claude | 有 `claude auth logout` | 直接调用官方命令 |
| Codex | **没有** logout 子命令 | 移除整个 `~/.codex/auth.json` |
| CodeBuddy | **没有** logout 子命令 | 清空 `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info` 里的 `auth` 与 `account` 段（同一文件里的其它设置保留） |

Codex 那条最容易写错：只把文件里的 `tokens` 置空、甚至把 `auth_mode` 也写成 null，`codex login status` **仍然报已登录**——它只看文件在不在。所以必须整份移除。移除前会备份成 `auth.json.logout-<时间>.bak`（权限 600），因为同一个文件里可能还有网关的 `OPENAI_API_KEY`。

两条实测踩出来的细节：

- **CodeBuddy 的「未登录」可能是假的。** 它的登录态靠跑一段临时对话来探，那一段会因为超时或网络抖动失败，看起来像没登录。所以退出前判断"能不能退"时会同时看凭据文件在不在：官方说未登录但凭据还在，仍然允许退出，否则凭据好好的却退不掉。
- **身份不是只有邮箱。** CodeBuddy 的凭据里没有邮箱字段，但有 `displayName` / `enterpriseName`，所以菜单显示成「CodeBuddy：已登录 · 李静 · 某教育公益版」，按钮也带上名字（`退出CodeBuddy账号（李静）`）。取不到展示名时只显示「已登录」，不会编造，也不会把状态词当身份渲染成「已登录 · 已登录」。

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
python3 tests/test_project.py         # 源码回归测试
python3 tests/test_hardening.py       # 五轮排查修复项的反向验证用例
python3 tests/test_codex_engine.py    # Codex 执行器参数与环境变量映射
python3 tests/test_installer.py       # 安装器配置校验与代码生成
python3 tests/test_menu_layout.py     # 键盘按钮与分组菜单一致性
python3 tests/test_auth_channel.py    # 网关/账号双渠道切换
python3 tests/test_platform_auth.py   # GitHub/Cloudflare 授权、凭据存取与注入
python3 tests/test_image_tool.py      # 生图 MCP 工具与协议测试
for t in tests/test_interaction_r*.py; do python3 "$t"; done
                                # 交互逻辑五轮测试：路由 / 队列 / 渠道 / 授权 / 边界
python3 scripts/security_check.py       # 提交前的敏感信息扫描
bash -n get.sh && bash -n install.sh
```

`tests/test_interaction_r1.py` ~ `tests/test_interaction_r5.py` 是按「机主真的按了一下」写的五轮交互测试，与前面那些按源码结构检查的用例互补：

| 轮次 | 覆盖范围 | 主要判据 |
|---|---|---|
| 第一轮 | 顶层按钮、二级分组、分组名、全部别名 | 每一下都必须有反应；普通文字必须进队列 |
| 第二轮 | 队列、并发隔离、停止与撤销、语音草稿 | 同会话不并行、撤销后不被领走、只停指定任务 |
| 第三轮 | 执行器与渠道切换、账号模型、底部摘要 | 渠道按执行器独立；切渠道不偷偷切执行器 |
| 第四轮 | 设备码、令牌回填、撤销、凭据注入、续期 | 等待期普通任务不被吞；未授权变量必须剔除 |
| 第五轮 | 超长消息、畸形更新、状态文件损坏、重入 | 任何异常都要落到一句人话，不能中断轮询 |


`scripts/verify_platform_auth.py` 是授权功能的**反向验证脚本**：它把 8 处保护逐条故意改坏，确认 `tests/test_platform_auth.py` 真的报红。永远绿的测试等于没有测试，改动授权相关代码后请跑一遍。

`tests/test_hardening.py` 里每个用例都对应一处真实缺陷，注释写明缺陷现象，并可用「把修复改回原写法、测试必须报红」的方式自检。**改完代码请先删掉 `__pycache__` 再跑测试**——陈旧的 `bot.cpython-*.pyc` 会让测试读到旧字节码，从而给出全绿的假象（本项目踩过）。

`tests/test_integration.py` 只提供绑定回环地址的假消息接口，必须配合独立运行用户与独立服务名使用，不能替代真实端到端验收，也禁止用于正式机器人。

官方执行器安装脚本使用已审阅的固定校验值，上游脚本一变安装器就拒绝执行。这是刻意的设计：安全边界不提供关闭开关。

## 代码结构与发布

| 文件 | 内容 |
|---|---|
| `bot.py` | 电报收发、菜单、任务队列（`TaskController`）、执行器命令、流式回复（`StreamMessage`） |
| `textfmt.py` | 回复排版、切片、脱敏、耗时格式等纯函数 |
| `usage.py` | 订阅用量（5 小时 / 7 天窗口）与套餐、续费日 |
| `github_auth.py` | GitHub 设备码授权、执行器账号登录与身份 |
| `cloudflare_auth.py` / `credentials.py` | Cloudflare 授权、平台凭据存取与注入 |
| `updater.py` / `bootstrap.py` / `install.py` | 菜单升级、归档校验、交互式安装 |
| `tests/` | 全部测试（`python3 tests/test_xxx.py` 逐个运行）与测试数据 |
| `scripts/` | 开发辅助：提交前敏感信息扫描、授权保护的反向验证 |
| `memory/` | 安装时复制给执行器的共享说明 |

程序文件必须留在仓库根目录：已安装机器的升级器按归档根目录取文件，挪进子目录会让旧版本升级失败。

文字命令和按钮在 `TaskController` 里用 `@on_text('按钮名')` 登记，按定义顺序匹配；新增按钮只要加一个处理方法。

每次推送到 `main`，GitHub Actions 会逐个跑全部测试。发布新版本：在 Actions 页手动运行 **tests** 工作流并勾选 `release`，测试通过后它会自动把 `version.txt` 定型到当前提交，已安装的机器人在菜单里「检查版本」即可看到。

新增代码文件要分两版发布：先在 `updater.py` 的 `OPTIONAL_FILES` 登记文件名并发布一版，再发布真正依赖它的代码。已安装机器上的升级器只认识自己白名单里的文件，跳过这一步会漏拷新模块、升级后启动失败并自动回滚。

## 常见问题

**机器人不回复？** 先看 `journalctl -u telegram-agent-hub -n 50`。最常见的是令牌错、`owner ID` 填成了别人的、或者服务器连不上 Telegram。

**想换模型？** 在电报里发「刷新模型」「模型搜索」即可，不用重装。

**能装在 ARM 小鸡上吗？** 可以，但建议先确认内存 ≥ 1 GiB；语音识别模型会额外占用内存，内存紧张就跳过语音功能。

**用私有仓库或自己的 fork？** 克隆自己的仓库后执行 `sudo bash install.sh`。注意 `get.sh` 里固定了官方仓库和提交，换仓库要同步改这两个值。
