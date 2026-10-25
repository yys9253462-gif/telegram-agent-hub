#!/usr/bin/env bash
# 电报双智能体工作台 · 一条命令交互式安装
#
# 用法（Debian 12 / Ubuntu 22.04 / 24.04）：
#   curl -fsSL https://raw.githubusercontent.com/yys9253462-gif/telegram-agent-hub/main/get.sh | bash
#
# 安全说明：
#   - 只从本仓库固定 commit 下载，不执行浮动分支内容
#   - 下载后校验归档哈希，与发布页公布值不一致即中止
#   - 不使用任何 GitHub 令牌，仓库是公开的
set -euo pipefail

REPO="yys9253462-gif/telegram-agent-hub"
REF="${AGENT_REF:-__REF__}"
SHA256="${AGENT_ARCHIVE_SHA256:-__SHA256__}"

say() { printf '%s\n' "$*"; }
fail() { printf '安装中止：%s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        fail "需要 root 权限，且系统未安装 sudo。请用 root 账号执行。"
    fi
    exec sudo -E bash "$0" "$@"
fi

command -v python3 >/dev/null 2>&1 || {
    apt-get update -qq && apt-get install -y -qq python3 ca-certificates tar >/dev/null
}

if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu) : ;;
        *) say "提示：当前系统为 ${ID:-未知}，仅在 Debian 12 与 Ubuntu 22.04/24.04 上验证过，继续安装可能失败。" ;;
    esac
fi

case "$(uname -m)" in
    x86_64|aarch64|arm64) : ;;
    *) fail "仅支持 x86_64 与 arm64，当前为 $(uname -m)" ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

say "正在下载固定版本源码（提交 ${REF:0:12}）…"
ARCHIVE="$WORK/source.tar.gz"
URL="https://api.github.com/repos/$REPO/tarball/$REF"
if ! curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 2 --max-time 180 -o "$ARCHIVE" "$URL"; then
    fail "下载失败，请检查网络能否访问 GitHub。"
fi

if [ "$SHA256" != "__SHA256__" ] && [ -n "$SHA256" ]; then
    command -v sha256sum >/dev/null 2>&1 || apt-get install -y -qq coreutils >/dev/null
    ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
    if [ "$ACTUAL" != "$SHA256" ]; then
        fail "归档校验不一致，已停止。期望 $SHA256，实际 $ACTUAL。可能是发布方更新或网络被篡改。"
    fi
    say "归档校验通过。"
fi

mkdir -p "$WORK/src"
tar -xzf "$ARCHIVE" -C "$WORK/src" --strip-components=1 --no-same-owner --no-same-permissions
[ -f "$WORK/src/install.py" ] || fail "归档中缺少安装器，可能是版本不匹配。"

say "源码已就绪，开始交互式安装。"
say ""
exec python3 "$WORK/src/install.py" "$@"
