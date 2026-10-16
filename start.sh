#!/usr/bin/env bash
#
# ai-qbot 一键管理脚本
#
# 部署结构：
#   - ai-qbot：宿主机 Python / Conda 环境运行
#   - NapCatQQ：Docker Compose 运行
#   - OneBot：NapCat 正向 WebSocket，默认 ws://127.0.0.1:3001
#
# 用法：
#
#   bash start.sh
#   bash start.sh start
#       启动 NapCat Docker + qbot
#
#   bash start.sh reload
#       只重启 qbot
#       不重启 NapCat，不影响 QQ 登录
#
#   bash start.sh stop
#       停止 qbot + NapCat
#
#   bash start.sh status
#       查看 Docker / OneBot / QQ / qbot 状态
#
#   bash start.sh log
#       实时查看 qbot 日志
#
#   bash start.sh naplog
#       实时查看 NapCat 日志
#
#   bash start.sh webui
#       查看 NapCat WebUI SSH 隧道使用方法
#
#   bash start.sh help
#       查看帮助
#
# ----------------------------------------------------------------------

set -uo pipefail


# ==============================
# 可配置项
# ==============================

# 默认使用执行 start.sh 时当前环境中的 python。
# 如果当前已 conda activate qbot，则一般就是：
# /opt/miniconda3/envs/qbot/bin/python
PYTHON_CMD="${PYTHON_CMD:-python}"

# NapCat Docker Compose 目录
NAPCAT_DIR="${NAPCAT_DIR:-/opt/napcat}"

# compose.yaml 中 service 名
NAPCAT_SERVICE="${NAPCAT_SERVICE:-napcat}"

# Docker 容器名
NAPCAT_CONTAINER="${NAPCAT_CONTAINER:-napcat}"

# WebUI 映射到宿主机的端口
NAPCAT_WEBUI_PORT="${NAPCAT_WEBUI_PORT:-6099}"

# Docker 启动后最多等多久让 OneBot WS 就绪。
# QQ 尚未登录或 WS 尚未配置时，即使等不到也会继续启动 qbot，
# 因为 qbot 自身会进行重连。
WS_WAIT_SEC="${WS_WAIT_SEC:-30}"

# screen 会话名
BOT_SESSION="${BOT_SESSION:-qbot}"


# ==============================
# 项目路径
# ==============================

PROJECT_DIR="$(
    cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 &&
    pwd
)"

if [ -z "$PROJECT_DIR" ]; then
    echo "无法确定项目目录" >&2
    exit 1
fi

cd "$PROJECT_DIR" || exit 1

LOG_DIR="$PROJECT_DIR/logs"
BOT_LOG="$LOG_DIR/qbot.log"

# 先使用默认值；require_python 后再从 src.config 读取真实地址
WS_URL="ws://127.0.0.1:3001"


# ==============================
# 输出函数
# ==============================

info() {
    printf '  %s\n' "$*"
}

ok() {
    printf '\033[32m✓\033[0m %s\n' "$*"
}

warn() {
    printf '\033[33m!\033[0m %s\n' "$*"
}

err() {
    printf '\033[31m✗\033[0m %s\n' "$*" >&2
}


# ==============================
# 基础依赖
# ==============================

require_python() {
    if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
        err "找不到 Python：$PYTHON_CMD"
        info "如果你使用 Conda，请先："
        info "  conda activate qbot"
        exit 1
    fi
}


require_screen() {
    if ! command -v screen >/dev/null 2>&1; then
        err "没有安装 screen"
        info "OpenCloudOS / RHEL 系可尝试："
        info "  dnf install -y screen"
        exit 1
    fi
}


require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        err "找不到 docker"
        exit 1
    fi

    if ! docker compose version >/dev/null 2>&1; then
        err "docker compose 不可用"
        exit 1
    fi

    if [ ! -d "$NAPCAT_DIR" ]; then
        err "NapCat 目录不存在：$NAPCAT_DIR"
        exit 1
    fi

    if [ ! -f "$NAPCAT_DIR/compose.yaml" ] &&
       [ ! -f "$NAPCAT_DIR/compose.yml" ] &&
       [ ! -f "$NAPCAT_DIR/docker-compose.yml" ] &&
       [ ! -f "$NAPCAT_DIR/docker-compose.yaml" ]; then

        err "在 $NAPCAT_DIR 找不到 Docker Compose 配置"
        info "支持："
        info "  compose.yaml"
        info "  compose.yml"
        info "  docker-compose.yml"
        info "  docker-compose.yaml"
        exit 1
    fi
}


# ==============================
# 配置读取
# ==============================

load_ws_url() {
    local value

    value="$(
        "$PYTHON_CMD" -c \
            'from src.config import ONEBOT_WS_URL; print(ONEBOT_WS_URL)' \
            2>/dev/null
    )" || true

    if [ -n "$value" ]; then
        WS_URL="$value"
    else
        WS_URL="ws://127.0.0.1:3001"
    fi
}


# ==============================
# screen / 进程状态
# ==============================

session_state() {
    local name="$1"
    local line

    line="$(
        screen -ls 2>/dev/null |
        grep -E "[0-9]+\.${name}[[:space:]]" |
        head -n 1
    )"

    if [ -z "$line" ]; then
        echo "none"
    elif [[ "$line" == *Dead* ]]; then
        echo "dead"
    else
        echo "running"
    fi
}


# 查找 qbot Python 进程。
# 不依赖 pgrep，避免不同发行版差异。
bot_running() {
    QBOT_PROJECT_DIR="$PROJECT_DIR" "$PYTHON_CMD" - <<'PY' 2>/dev/null
import os
import sys

project = os.environ.get("QBOT_PROJECT_DIR", "")

for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue

    base = os.path.join("/proc", entry)

    try:
        with open(os.path.join(base, "cmdline"), "rb") as f:
            args = [part.decode("utf-8", "replace") for part in f.read().split(b"\0") if part]
    except OSError:
        continue

    # Match argv, not words inside shell/administration command strings.
    module = any(args[i] == "-m" and args[i + 1] in ("src.main", "src.runner")
                 for i in range(len(args) - 1))
    script = len(args) > 1 and args[1].replace("\\", "/").endswith(("src/main.py", "src/runner.py"))
    if not module and not script:
        continue

    # 尽量限制到当前项目，避免服务器上另一个 qbot 被误判。
    try:
        cwd = os.path.realpath(os.path.join(base, "cwd"))
    except OSError:
        cwd = ""

    if cwd == project or cwd.startswith(project + os.sep):
        sys.exit(0)

sys.exit(1)
PY
}


drop_stale_bot_session() {
    local state

    state="$(session_state "$BOT_SESSION")"

    case "$state" in
        none)
            return 0
            ;;
    esac

    if bot_running; then
        return 0
    fi

    warn "清理残留 screen 会话：$BOT_SESSION"

    screen -S "$BOT_SESSION" -X quit >/dev/null 2>&1 || true
    sleep 1

    screen -wipe >/dev/null 2>&1 || true
}


# ==============================
# OneBot 端口检查
# ==============================

port_open() {
    "$PYTHON_CMD" - "$WS_URL" <<'PY' >/dev/null 2>&1
import socket
import sys
from urllib.parse import urlparse

url = sys.argv[1]

parsed = urlparse(url)

host = parsed.hostname
port = parsed.port

if not host:
    raise SystemExit(1)

if port is None:
    port = 443 if parsed.scheme == "wss" else 80

try:
    sock = socket.create_connection((host, port), timeout=1.5)
except OSError:
    raise SystemExit(1)

sock.close()
raise SystemExit(0)
PY
}


wait_ws() {
    local elapsed=0

    printf '  等待 OneBot WebSocket %s ' "$WS_URL"

    while [ "$elapsed" -lt "$WS_WAIT_SEC" ]; do
        if port_open; then
            printf '\n'
            ok "OneBot WebSocket 已就绪"
            return 0
        fi

        printf '.'
        sleep 2
        elapsed=$((elapsed + 2))
    done

    printf '\n'
    return 1
}


# ==============================
# Docker / NapCat
# ==============================

napcat_running() {
    [ "$(
        docker inspect \
            -f '{{.State.Running}}' \
            "$NAPCAT_CONTAINER" \
            2>/dev/null
    )" = "true" ]
}


napcat_exists() {
    docker inspect "$NAPCAT_CONTAINER" >/dev/null 2>&1
}


compose() {
    (
        cd "$NAPCAT_DIR" || exit 1
        docker compose "$@"
    )
}


start_napcat() {
    require_docker

    if napcat_running; then
        info "NapCat Docker 已在运行，跳过启动"
        return 0
    fi

    info "启动 NapCat Docker..."

    if ! compose up -d "$NAPCAT_SERVICE"; then
        err "NapCat Docker 启动失败"
        return 1
    fi

    sleep 2

    if napcat_running; then
        ok "NapCat Docker 已启动"
        return 0
    fi

    err "NapCat 容器没有进入运行状态"
    info "查看日志："
    info "  bash start.sh naplog"

    return 1
}


stop_napcat() {
    require_docker

    if ! napcat_exists; then
        info "NapCat 容器不存在"
        return 0
    fi

    if ! napcat_running; then
        info "NapCat 已停止"
        return 0
    fi

    info "停止 NapCat..."

    if compose stop "$NAPCAT_SERVICE"; then
        ok "NapCat 已停止"
    else
        err "停止 NapCat 失败"
        return 1
    fi
}


# ==============================
# qbot 启停
# ==============================

start_bot() {
    require_python
    require_screen

    mkdir -p "$LOG_DIR"

    if bot_running; then
        info "qbot 已在运行，跳过启动"
        return 0
    fi

    drop_stale_bot_session

    # 将当前 Python 的绝对路径传进 screen。
    # 这样即使 screen 里的 login shell 没继承 Conda 环境，
    # 仍然会使用当前 qbot 环境中的 Python。
    local python_abs
    python_abs="$(command -v "$PYTHON_CMD")"

    info "启动 qbot..."
    info "Python：$python_abs"

    screen -dmS "$BOT_SESSION" env \
        QBOT_DIR="$PROJECT_DIR" \
        QBOT_PY="$python_abs" \
        QBOT_LOG="$BOT_LOG" \
        bash -lc '
            cd "$QBOT_DIR" || exit 1
            exec "$QBOT_PY" -u -m src.runner
        '

    sleep 2

    if bot_running; then
        ok "qbot 已启动"
        info "日志：$BOT_LOG"
        return 0
    fi

    err "qbot 启动后立即退出"

    if [ -f "$BOT_LOG" ]; then
        echo
        tail -n 30 "$BOT_LOG"
    fi

    return 1
}


stop_bot() {
    require_screen

    local state
    state="$(session_state "$BOT_SESSION")"

    if ! bot_running && [ "$state" = "none" ]; then
        info "qbot 未运行"
        return 0
    fi

    info "停止 qbot..."

    if [ "$state" != "none" ]; then
        screen -S "$BOT_SESSION" -X quit >/dev/null 2>&1 || true
    fi

    # 最多等十秒让 Python 真正退出。
    local i=0

    while bot_running && [ "$i" -lt 10 ]; do
        sleep 1
        i=$((i + 1))
    done

    screen -wipe >/dev/null 2>&1 || true

    if bot_running; then
        err "qbot 仍在退出中"
        info "暂不重复启动，稍后再执行："
        info "  bash start.sh reload"
        return 1
    fi

    ok "qbot 已停止"
    return 0
}


# ==============================
# QQ 在线状态检查
# ==============================

check_qq() {
    "$PYTHON_CMD" - <<'PY'
import asyncio
import json
import sys

import aiohttp

from src.config import ONEBOT_ACCESS_TOKEN, ONEBOT_WS_URL


async def request(ws, action, echo):
    await ws.send_str(
        json.dumps(
            {
                "action": action,
                "params": {},
                "echo": echo,
            }
        )
    )

    while True:
        message = await ws.receive()

        if message.type != aiohttp.WSMsgType.TEXT:
            return None

        try:
            payload = json.loads(message.data)
        except Exception:
            continue

        messages = payload if isinstance(payload, list) else [payload]

        for item in messages:
            if not isinstance(item, dict):
                continue

            if str(item.get("echo")) == echo:
                return item


async def probe():
    headers = {}

    if ONEBOT_ACCESS_TOKEN:
        headers["Authorization"] = (
            f"Bearer {ONEBOT_ACCESS_TOKEN}"
        )

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout,
    ) as session:

        async with session.ws_connect(
            ONEBOT_WS_URL,
            heartbeat=None,
        ) as ws:

            status = await asyncio.wait_for(
                request(ws, "get_status", "qbot-status"),
                timeout=6,
            )

            login = await asyncio.wait_for(
                request(ws, "get_login_info", "qbot-login"),
                timeout=6,
            )

    online = bool(
        isinstance(status, dict)
        and isinstance(status.get("data"), dict)
        and status["data"].get("online") is True
    )

    user_id = "?"
    nickname = ""

    if isinstance(login, dict):
        data = login.get("data")

        if isinstance(data, dict):
            user_id = data.get("user_id", "?")
            nickname = data.get("nickname") or ""

    identity = str(user_id)

    if nickname:
        identity += f" {nickname}"

    if online:
        print(
            "\033[32m✓\033[0m "
            f"QQ 在线（{identity}）"
        )
        return 0

    print(
        "\033[31m✗\033[0m "
        f"QQ 当前不在线（{identity}）"
    )
    print(
        "  NapCat 容器可能还活着，"
        "但 QQ 登录本身已经断开。"
    )
    print(
        "  查看：bash start.sh naplog"
    )
    print(
        "  登录：bash start.sh webui"
    )

    return 1


async def main():
    try:
        return await probe()

    except asyncio.TimeoutError:
        print(
            "\033[31m✗\033[0m "
            "OneBot 请求超时"
        )
        return 1

    except aiohttp.WSServerHandshakeError as exc:
        print(
            "\033[31m✗\033[0m "
            f"WebSocket 握手失败：HTTP {exc.status}"
        )
        print(
            "  请检查 ONEBOT_ACCESS_TOKEN "
            "是否与 NapCat 一致。"
        )
        return 1

    except Exception as exc:
        print(
            "\033[31m✗\033[0m "
            f"无法连接 NapCat："
            f"{type(exc).__name__}: {exc}"
        )
        return 1


try:
    sys.exit(
        asyncio.run(
            asyncio.wait_for(main(), timeout=20)
        )
    )
except asyncio.TimeoutError:
    print(
        "\033[31m✗\033[0m "
        "NapCat 状态检查整体超时"
    )
    sys.exit(1)
PY
}


# ==============================
# start
# ==============================

do_start() {
    require_python
    require_screen
    require_docker

    load_ws_url

    screen -wipe >/dev/null 2>&1 || true
    mkdir -p "$LOG_DIR"

    echo "── NapCat ─────────────────────────────"

    if ! start_napcat; then
        exit 1
    fi

    if ! wait_ws; then
        warn "OneBot WebSocket 暂未就绪"
        info "可能原因："
        info "  1. QQ 尚未登录"
        info "  2. WebUI 中尚未启用 WebSocket 服务器"
        info "  3. WebSocket 端口不是 3001"
        info "  4. .env 的 ONEBOT_WS_URL 配置错误"
        info
        info "仍然继续启动 qbot，它会自行重连。"
    fi

    echo
    echo "── qbot ──────────────────────────────"

    if ! start_bot; then
        exit 1
    fi

    echo
    ok "启动完成"

    info "状态："
    info "  bash start.sh status"

    info "qbot 日志："
    info "  bash start.sh log"

    info "NapCat 日志："
    info "  bash start.sh naplog"

    info "仅重载 qbot："
    info "  bash start.sh reload"
}


# ==============================
# reload
# ==============================

do_reload() {
    require_python
    require_screen

    load_ws_url

    echo "── qbot reload ───────────────────────"

    info "检查 Python 源码..."

    if ! "$PYTHON_CMD" -m compileall -q "$PROJECT_DIR/src"; then
        err "Python 语法检查失败"
        err "拒绝重启当前机器人"
        exit 1
    fi

    info "检查配置..."

    if ! "$PYTHON_CMD" - <<'PY'
from src import config

assert config.ONEBOT_WS_URL
PY
    then
        err "配置加载失败"
        err "拒绝重启当前机器人"
        exit 1
    fi

    ok "源码和配置检查通过"

    if ! stop_bot; then
        exit 1
    fi

    if ! start_bot; then
        exit 1
    fi

    echo
    ok "qbot 重载完成"
    ok "NapCat Docker 未重启"
    ok "QQ 登录保持不变"
}


# ==============================
# stop
# ==============================

do_stop() {
    require_python
    require_screen

    echo "── qbot ──────────────────────────────"

    stop_bot || true

    echo
    echo "── NapCat ─────────────────────────────"

    if command -v docker >/dev/null 2>&1 &&
       docker compose version >/dev/null 2>&1; then

        stop_napcat || true
    else
        warn "Docker 不可用，无法自动停止 NapCat"
    fi

    echo
    ok "停止流程完成"
}


# ==============================
# status
# ==============================

do_status() {
    require_python
    require_screen

    load_ws_url

    echo "── qbot ──────────────────────────────"

    if bot_running; then
        ok "qbot 运行中"

        local python_abs
        python_abs="$(command -v "$PYTHON_CMD")"

        info "Python：$python_abs"
    else
        case "$(session_state "$BOT_SESSION")" in
            dead)
                warn "qbot 未运行，存在 Dead screen 会话"
                ;;
            running)
                warn "screen 会话存在，但 qbot Python 已退出"
                ;;
            *)
                warn "qbot 未运行"
                ;;
        esac
    fi


    echo
    echo "── NapCat Docker ─────────────────────"

    if ! command -v docker >/dev/null 2>&1; then
        err "Docker 不可用"

    elif napcat_running; then
        ok "NapCat 容器运行中"

        docker ps \
            --filter "name=^/${NAPCAT_CONTAINER}$" \
            --format '  镜像：{{.Image}}
  状态：{{.Status}}
  端口：{{.Ports}}'

    elif napcat_exists; then
        warn "NapCat 容器存在，但当前已停止"

    else
        warn "找不到 NapCat 容器"
    fi


    echo
    echo "── OneBot WebSocket ──────────────────"

    info "地址：$WS_URL"

    if port_open; then
        ok "OneBot WebSocket 端口可连接"
    else
        warn "OneBot WebSocket 当前无法连接"
    fi


    echo
    echo "── QQ 登录状态 ───────────────────────"

    if port_open; then
        check_qq || true
    else
        warn "OneBot 端口未就绪，跳过 QQ 状态查询"
        info "查看 NapCat："
        info "  bash start.sh naplog"
    fi


    echo
    echo "── qbot 日志尾部 ─────────────────────"

    if [ -f "$BOT_LOG" ]; then
        tail -n 8 "$BOT_LOG"

        echo
        info "实时日志："
        info "  bash start.sh log"
    else
        warn "尚无 qbot 日志"
    fi


    echo

    if ! bot_running; then
        info "启动：bash start.sh"
    fi
}


# ==============================
# WebUI
# ==============================

do_webui() {
    echo "── NapCat WebUI ──────────────────────"
    echo

    if command -v docker >/dev/null 2>&1; then
        if napcat_running; then
            ok "NapCat 容器正在运行"
        else
            warn "NapCat 容器当前未运行"
            info "先执行："
            info "  bash start.sh"
            echo
        fi
    fi

    info "WebUI 在服务器上只监听本机："
    info "  http://127.0.0.1:${NAPCAT_WEBUI_PORT}/webui"

    echo
    info "在你自己的 Windows / macOS / Linux 电脑运行："
    echo

    printf '  ssh -L %s:127.0.0.1:%s root@你的服务器公网IP\n' \
        "$NAPCAT_WEBUI_PORT" \
        "$NAPCAT_WEBUI_PORT"

    echo
    info "保持 SSH 窗口打开，然后本机浏览器访问："
    echo

    printf '  http://127.0.0.1:%s/webui\n' \
        "$NAPCAT_WEBUI_PORT"

    echo
    info "WebUI 登录使用你在 /opt/napcat/compose.yaml"
    info "里设置的 WEBUI_TOKEN。"

    echo
    info "注意："
    info "  WEBUI_TOKEN 和 ONEBOT_ACCESS_TOKEN 是两回事。"
    info "  不要把任何 token 粘贴到聊天、日志或 Git 仓库。"
}


# ==============================
# qbot log
# ==============================

do_log() {
    if [ ! -f "$BOT_LOG" ]; then
        err "尚无 qbot 日志"
        info "先执行："
        info "  bash start.sh"
        exit 1
    fi

    info "qbot 实时日志"
    info "Ctrl+C 仅退出日志，不停止机器人"
    echo

    tail -n 50 -f "$BOT_LOG"
}


# ==============================
# NapCat log
# ==============================

do_naplog() {
    require_docker

    if ! napcat_exists; then
        err "找不到 NapCat 容器"
        exit 1
    fi

    info "NapCat 实时日志"
    info "Ctrl+C 仅退出日志，不停止 NapCat"
    echo

    compose logs --tail=100 -f "$NAPCAT_SERVICE"
}


# ==============================
# help
# ==============================

usage() {
    cat <<'EOF'
ai-qbot 管理脚本

用法：

  bash start.sh
  bash start.sh start
      启动 NapCat Docker + qbot

  bash start.sh reload
      仅重启 qbot
      NapCat / QQ 登录不受影响

  bash start.sh stop
      停止 qbot + NapCat

  bash start.sh status
      查看：
        - qbot 状态
        - NapCat Docker 状态
        - OneBot WebSocket
        - QQ 在线状态
        - qbot 最近日志

  bash start.sh log
      实时查看 qbot 日志

  bash start.sh naplog
      实时查看 NapCat 日志

  bash start.sh webui
      显示 WebUI SSH 隧道连接方法

  bash start.sh help
      显示本帮助


默认部署：

  NapCat Compose:
      /opt/napcat

  NapCat container:
      napcat

  OneBot:
      ws://127.0.0.1:3001

  WebUI:
      127.0.0.1:6099

以上项目均可通过环境变量覆盖。
EOF
}


# ==============================
# 命令入口
# ==============================

case "${1:-start}" in

    start)
        do_start
        ;;

    reload)
        do_reload
        ;;

    stop)
        do_stop
        ;;

    status)
        do_status
        ;;

    log)
        do_log
        ;;

    naplog)
        do_naplog
        ;;

    webui)
        do_webui
        ;;

    help|-h|--help)
        usage
        ;;

    *)
        err "未知命令：$1"
        echo
        usage
        exit 1
        ;;

esac
