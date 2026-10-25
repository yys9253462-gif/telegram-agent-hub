#!/usr/bin/env bash
# 电报双智能体工作台 · 一条命令交互式安装
#
# 用法（Debian 12 / Ubuntu 22.04 / 24.04）：
#   curl -fsSL https://raw.githubusercontent.com/yys9253462-gif/telegram-agent-hub/main/get.sh | bash
#
# 国内网络访问 raw.githubusercontent.com 失败时，可用镜像入口：
#   curl -fsSL https://cdn.jsdelivr.net/gh/yys9253462-gif/telegram-agent-hub@main/get.sh | bash
#
# 安全说明：
#   - 只安装本仓库指定 commit 的内容，不跟随浮动分支
#   - 下载后校验归档 SHA256，与发布说明不一致立即中止
#   - 仓库是公开的，全程不使用任何 GitHub 凭据
set -euo pipefail

REPO="yys9253462-gif/telegram-agent-hub"
# 固定提交号存放在 version.txt，由发布流程更新，与代码分离，避免自引用。
# 可用环境变量覆盖以便测试：AGENT_REF=... AGENT_ARCHIVE_SHA256=...
REF="${AGENT_REF:-}"
SHA256="${AGENT_ARCHIVE_SHA256:-}"
VERSION_URLS=(
    "https://raw.githubusercontent.com/$REPO/main/version.txt"
    "https://ghproxy.net/https://raw.githubusercontent.com/$REPO/main/version.txt"
    "https://cdn.jsdelivr.net/gh/$REPO@main/version.txt"
)

say() { printf '%s\n' "$*"; }
fail() { printf '安装中止：%s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        fail "需要 root 权限，且系统未安装 sudo。请用 root 账号执行。"
    fi
    exec sudo -E bash "$0" "$@"
fi

if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3 ca-certificates curl tar coreutils >/dev/null
fi
command -v curl >/dev/null 2>&1 || apt-get install -y -qq curl >/dev/null

if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu) : ;;
        *) say "提示：当前系统为 ${ID:-未知}。仅在 Debian 12 与 Ubuntu 22.04/24.04 上验证过，继续可能失败。" ;;
    esac
fi
case "$(uname -m)" in
    x86_64|aarch64|arm64) : ;;
    *) fail "仅支持 x86_64 与 arm64，当前为 $(uname -m)" ;;
esac

if [ -z "$REF" ]; then
    say "正在获取固定版本号…"
    for url in "${VERSION_URLS[@]}"; do
        version="$(curl --proto '=https' --proto-redir '=https' -fsSL --max-time 30 "$url" 2>/dev/null || true)"
        ref_line="$(printf '%s
' "$version" | grep -E '^[0-9a-f]{40}$' | head -1 || true)"
        sha_line="$(printf '%s
' "$version" | grep -E '^sha256:[0-9a-f]{64}$' | head -1 | cut -d: -f2 || true)"
        if [ -n "$ref_line" ]; then
            REF="$ref_line"
            [ -n "$sha_line" ] && SHA256="$sha_line"
            break
        fi
    done
    [ -n "$REF" ] || fail "无法获取固定版本号。请检查网络，或手动指定 AGENT_REF=<40位提交号> 后重试。"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"
ARCHIVE="$WORK/source.tar.gz"

# 依次尝试多个下载源，任一成功即停。
FETCH_URLS=(
    "https://api.github.com/repos/$REPO/tarball/$REF"
    "https://ghproxy.net/https://api.github.com/repos/$REPO/tarball/$REF"
    "https://gh-proxy.com/https://api.github.com/repos/$REPO/tarball/$REF"
)

say "正在下载源码（提交 ${REF:0:12}）…"
OK=0
for url in "${FETCH_URLS[@]}"; do
    if curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 1 --max-time 180 -o "$ARCHIVE" "$url" 2>/dev/null; then
        if tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
            OK=1
            break
        fi
    fi
done
[ "$OK" -eq 1 ] || fail "所有下载源均失败。请检查网络能否访问 GitHub，或改用镜像安装命令（见 README）。"

if [ -n "$SHA256" ]; then
    ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
    if [ "$ACTUAL" != "$SHA256" ]; then
        fail "归档校验不一致，已停止。可能是发布方更新或网络被篡改。"
    fi
    say "归档校验通过。"
else
    say "提示：本次未提供归档校验值，仅依赖提交固定版本。"
fi

mkdir -p "$WORK/src"
tar -xzf "$ARCHIVE" -C "$WORK/src" --strip-components=1 --no-same-owner --no-same-permissions
[ -f "$WORK/src/install.py" ] || fail "归档中缺少安装器，可能是版本不匹配。"

say ""
say "源码已就绪，开始交互式安装。"
# 通过 curl | bash 执行时标准输入是管道，交互问答会读不到终端。
# 存在控制终端时把标准输入接回终端；没有终端则交给安装器报错提示用 --yes。
if [ -t 0 ]; then
    exec python3 "$WORK/src/install.py" "$@"
elif [ -r /dev/tty ] && exec 3</dev/tty 2>/dev/null; then
    exec python3 "$WORK/src/install.py" "$@" <&3 3<&-
else
    exec python3 "$WORK/src/install.py" "$@"
fi
