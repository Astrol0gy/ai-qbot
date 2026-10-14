#!/usr/bin/env bash
#
# ai-qbot 一键管理脚本（Termux 单机部署：机器人 + NapCat 同机）
#
#   bash start.sh           一键启动（唤醒锁 → NapCat → 机器人）
#   bash start.sh stop      停止机器人 + NapCat，并释放唤醒锁
#   bash start.sh status    查看状态（进程真活着没 / 端口 / QQ 在线 / 日志尾部）
#   bash start.sh webui     打开 NapCat 的 WebUI（首次登录 QQ 要扫码）
#   bash start.sh log       实时跟随机器人日志
#   bash start.sh help      帮助
#
# 为什么用 bash 调用：项目放在共享存储（/sdcard）上时是 noexec，./start.sh 会
# Permission denied；bash start.sh 只需要读权限，绕过它。
#
# 若提示 bad interpreter，用 bash 调用即可：bash start.sh

# ------------------------------ 可按需修改 ------------------------------
# 机器人的 python；用了虚拟环境就改成 /path/to/venv/bin/python
PYTHON_CMD="${PYTHON_CMD:-python}"
# NapCat 启动命令：整条命令行（用 bash -lc 执行，可以带引号、管道）。
# 默认对应官方安装脚本的「Shell (Rootless) / proot 容器」方式：这种方式 Termux 里
# **没有** napcat 命令（除非安装时选了 TUI-CLI），NapCat 在容器里，得靠 proot-distro 进去跑。
NAPCAT_CMD_DEFAULT='proot-distro sh napcat -- bash -c "xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox"'
NAPCAT_CMD="${NAPCAT_CMD:-$NAPCAT_CMD_DEFAULT}"
# 检测 NapCat 在不在跑时匹配的进程关键词（容器方式下进程命令行里含 napcat）
NAPCAT_PROC="${NAPCAT_PROC:-napcat}"
# 存在性检查只看整条命令的第一个词（即上面那条的 proot-distro）
NAPCAT_BIN="${NAPCAT_CMD%% *}"
# NapCat 的 WebUI 配置文件（默认是容器方式的路径；装法不同就整条覆盖）。
# 扫码登录用的地址 + token 都在这个文件里，bash start.sh webui 会读它。
TERMUX_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
WEBUI_JSON="${WEBUI_JSON:-$TERMUX_PREFIX/var/lib/proot-distro/installed-rootfs/napcat/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/webui.json}"
# 启动 NapCat 后等它监听 WS 端口的秒数（等不到也会继续启动机器人：
# 机器人自己有重连循环，NapCat 起来后会自动连上）
# 容器方式（proot + xvfb + QQ）启动慢，给宽一点
WS_WAIT_SEC="${WS_WAIT_SEC:-45}"
# ----------------------------------------------------------------------

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$PROJECT_DIR" || exit 1

BOT_SESSION="qbot"
NAPCAT_SESSION="napcat"
LOG_DIR="$PROJECT_DIR/logs"
BOT_LOG="$LOG_DIR/qbot.log"

info() { printf '  %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
err()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; }

# 从 .env 读出 OneBot 地址（以 src/config.py 为准，读不到就用默认值）
WS_URL="$(
    "$PYTHON_CMD" -c 'from src.config import ONEBOT_WS_URL; print(ONEBOT_WS_URL)' 2>/dev/null
)"
WS_URL="${WS_URL:-ws://127.0.0.1:3001}"
WS_HOST="${WS_URL#*://}"
WS_HOST="${WS_HOST%%:*}"
WS_PORT="${WS_URL##*:}"
WS_PORT="${WS_PORT%%/*}"

# ------------------------------ 基础检查 ------------------------------
require_screen() {
    if ! command -v screen >/dev/null 2>&1; then
        err "没装 screen，先执行：pkg install screen"
        exit 1
    fi
}

require_python() {
    if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
        err "找不到 python 命令（$PYTHON_CMD），请检查 PYTHON_CMD 配置"
        exit 1
    fi
}

# 会话状态：running / dead / none
# 注意：screen 里进程退出后会话会以 (Dead) 残留，这里要区分开，
# 否则会出现「显示在运行、其实早就挂了」的误判。
session_state() {
    local line
    line="$(screen -ls 2>/dev/null | grep -E "[0-9]+\.$1[[:space:]]")"
    if [ -z "$line" ]; then
        echo none
    elif [[ "$line" == *Dead* ]]; then
        echo dead
    else
        echo running
    fi
}

port_open() {
    "$PYTHON_CMD" - "$WS_HOST" "$WS_PORT" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(1.5)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
}

# 扫 /proc 找进程（不依赖 procps 的 pgrep，Termux 默认没装）
# 关键词走环境变量而不是命令行参数：参数会出现在自己的 /proc/<pid>/cmdline 里，
# 那样每次都扫到自己、永远返回「还活着」，机器人就再也起不来了。
# 多个关键词任意命中即算存活。
proc_alive() {
    PROC_KEYS="$(printf '%s\n' "$@")" PROC_ROOT="${PROC_ROOT:-/proc}" \
        "$PYTHON_CMD" - <<'PY' 2>/dev/null
import os
import sys

keys = [k for k in os.environ["PROC_KEYS"].splitlines() if k]
root = os.environ["PROC_ROOT"]
try:
    entries = os.listdir(root)
except OSError:
    sys.exit(1)  # 没有 /proc（非 Linux）→ 当作没找到

for pid in entries:
    if not pid.isdigit():
        continue
    try:
        with open(os.path.join(root, pid, "cmdline"), "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        continue
    # 跳过 screen 自己的进程：它的命令行里带着会话名（screen -S napcat），
    # 会把「空壳会话」误判成「服务还在」
    if cmd.split(" ", 1)[0].rsplit("/", 1)[-1] in ("screen", "SCREEN"):
        continue
    if any(k in cmd for k in keys):
        sys.exit(0)
sys.exit(1)
PY
}

# 「真的在跑」和「screen 会话还在」是两件事。
# 会话里的程序退出后，如果那是个交互式 shell，会话照样活着，于是出现
# 「显示在运行、其实早就挂了」—— NapCat 半夜挂掉、Termux 却看起来一切正常，
# 就是这么来的。
bot_running()    { proc_alive "src.main" "src/main"; }
napcat_running() { proc_alive "$NAPCAT_PROC" || port_open; }

# 清掉只剩壳的残留会话，否则 screen -dmS 会因会话重名而起不来
drop_stale_session() {
    case "$(session_state "$1")" in
        none) return 0 ;;
    esac
    warn "清理残留的 screen 会话 $1（里面已经没有进程）"
    screen -S "$1" -X quit >/dev/null 2>&1
    sleep 1
}

# ------------------------------ 启动 ------------------------------
do_start() {
    require_screen
    require_python
    screen -wipe >/dev/null 2>&1
    mkdir -p "$LOG_DIR"

    # 1) 唤醒锁：挂在 Termux 应用进程上，App 被杀/设备重启后会丢，
    #    所以每次 start 都重新获取一次（重复获取无副作用）
    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock >/dev/null 2>&1 && ok "已获取唤醒锁（息屏后不被冻结）"
    else
        warn "找不到 termux-wake-lock，建议 pkg install termux-tools"
    fi

    # 2) NapCat 协议端
    if napcat_running; then
        info "NapCat 已在运行，跳过启动"
    else
        if ! command -v "$NAPCAT_BIN" >/dev/null 2>&1; then
            err "找不到命令「$NAPCAT_BIN」（NapCat 启动命令的第一个词）"
            info "NapCat 装在 proot 容器里的话，需要：pkg install proot-distro"
            info "启动命令不一样的话，整条覆盖掉再跑："
            info "  NAPCAT_CMD='你的启动命令' bash start.sh"
            exit 1
        fi
        drop_stale_session "$NAPCAT_SESSION"
        screen -dmS "$NAPCAT_SESSION" bash -lc "$NAPCAT_CMD"
        ok "已启动 NapCat（screen 会话 $NAPCAT_SESSION）"

        printf '  等待 NapCat 监听 %s ' "$WS_URL"
        i=0
        while [ "$i" -lt "$WS_WAIT_SEC" ]; do
            if port_open; then
                printf '\n'
                ok "WS 端口已就绪"
                break
            fi
            sleep 2
            i=$((i + 2))
            printf '.'
        done
        if ! port_open; then
            printf '\n'
            warn "等不到 WS 端口，仍然继续启动机器人（它会自己重连）"
            if [ "$(session_state "$NAPCAT_SESSION")" = "dead" ]; then
                err "NapCat 进程已经退出——启动命令本身有问题"
                info "直接在 Termux 里跑一遍就能看到报错（不用进 screen）："
                info "  $NAPCAT_CMD"
                info "命令不对就整条覆盖：NAPCAT_CMD='...' bash start.sh"
            else
                info "登录成功之前 NapCat 不会监听这个端口。要扫码登录的话："
                info "  bash start.sh webui    # 读 webui.json 拼出地址，直接打开扫码页面"
                info "（进 screen -r $NAPCAT_SESSION 也能看到同一行网址，Ctrl+A 再按 D 退出）"
                info "其他可能：还没在 WebUI 里建「WebSocket 服务器」"
            fi
        fi
    fi

    # 3) 机器人（输出同时进日志文件，方便事后回看「它到底卡在哪」）
    if bot_running; then
        info "机器人已在运行，跳过启动"
        if [ ! -f "$BOT_LOG" ]; then
            warn "它不是本脚本启动的，所以没有日志文件"
            info "想要日志（下次出事能查）：bash start.sh stop && bash start.sh"
        fi
    else
        drop_stale_session "$BOT_SESSION"
        screen -dmS "$BOT_SESSION" env \
            QBOT_DIR="$PROJECT_DIR" \
            QBOT_PY="$PYTHON_CMD" \
            QBOT_LOG="$BOT_LOG" \
            bash -lc 'cd "$QBOT_DIR" && "$QBOT_PY" -u -m src.main 2>&1 | tee -a "$QBOT_LOG"'
        sleep 2
        if bot_running; then
            ok "已启动机器人（日志：$BOT_LOG）"
        else
            err "机器人启动后立刻退出了，看看日志："
            tail -n 20 "$BOT_LOG" 2>/dev/null
            exit 1
        fi
    fi

    echo
    ok "启动完成"
    info "看日志：bash start.sh log      或  screen -r $BOT_SESSION"
    info "看状态：bash start.sh status"
    info "停全部：bash start.sh stop"
}

# ------------------------------ 停止 ------------------------------
do_stop() {
    require_screen
    for name in "$BOT_SESSION" "$NAPCAT_SESSION"; do
        case "$(session_state "$name")" in
            none)
                info "$name 未在运行"
                ;;
            *)
                screen -S "$name" -X quit >/dev/null 2>&1
                ok "已停止 $name"
                ;;
        esac
    done
    screen -wipe >/dev/null 2>&1

    # 容器方式下 proot 的子孙进程偶尔会活过 screen 会话，确认一下（没有 python 就跳过，
    # 停止这件事不能因为缺工具而失败）
    if command -v "$PYTHON_CMD" >/dev/null 2>&1 && port_open; then
        warn "NapCat 的 WS 端口还在（$WS_URL）——容器里可能有残留进程没退"
        info "稍等几秒再看一次：bash start.sh status"
    fi

    if command -v termux-wake-unlock >/dev/null 2>&1; then
        termux-wake-unlock >/dev/null 2>&1 && info "已释放唤醒锁"
    fi
}

# ------------------------------ 状态 ------------------------------
# 直连 NapCat 查 get_status / get_login_info，判断 QQ 本身在不在线。
# 这是「机器人像死了但其实只是 QQ 掉线」唯一靠谱的判据：
# NapCat 进程活着、WS 也连着，只是 QQ 那条长连接断了，于是收不到任何消息。
# 会短暂开第二条 WS（NapCat 支持多客户端），不影响正在跑的机器人。
check_qq() {
    "$PYTHON_CMD" - <<'PY'
import asyncio
import json
import sys

import aiohttp

from src.config import ONEBOT_ACCESS_TOKEN, ONEBOT_WS_URL


async def call(ws, action, echo):
    await ws.send_str(json.dumps({"action": action, "echo": echo}))
    while True:
        msg = await ws.receive()
        if msg.type is not aiohttp.WSMsgType.TEXT:
            return None
        try:
            payload = json.loads(msg.data)
        except Exception:
            continue
        for item in payload if isinstance(payload, list) else [payload]:
            if isinstance(item, dict) and str(item.get("echo")) == echo:
                return item


async def main():
    headers = {}
    if ONEBOT_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {ONEBOT_ACCESS_TOKEN}"

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(ONEBOT_WS_URL) as ws:
            status = await asyncio.wait_for(call(ws, "get_status", "probe-status"), 6)
            login = await asyncio.wait_for(call(ws, "get_login_info", "probe-login"), 6)

    who = ""
    if login and login.get("data"):
        d = login["data"]
        nick = d.get("nickname") or ""
        who = f"（{d.get('user_id', '?')}{' ' + nick if nick else ''}）"

    if not status:
        print("?  NapCat 没回 get_status，无法判断登录状态")
        return 0

    if (status.get("data") or {}).get("online") is True:
        print(f"\033[32m✓\033[0m QQ 在线{who}")
        return 0

    print(f"\033[31m✗\033[0m QQ 掉线{who} —— NapCat 活着，但 QQ 自己断了")
    print("   重连机器人没用，要重新登录 QQ：")
    print("     screen -r napcat                      # 看 NapCat 的提示/二维码（Ctrl+A 再按 D 退出）")
    print("     bash start.sh stop && bash start.sh   # 或整体重启")
    return 1


try:
    sys.exit(asyncio.run(asyncio.wait_for(main(), 20)))
except asyncio.TimeoutError:
    print("\033[31m✗\033[0m 连接 NapCat 超时（没监听 / 没建 WebSocket 服务器）")
    sys.exit(1)
except Exception as e:
    print(f"\033[31m✗\033[0m 连不上 NapCat：{type(e).__name__}: {e}")
    sys.exit(1)
PY
}

do_status() {
    require_screen
    require_python

    # 只看 screen 会话会被空壳会话骗到，必须查进程/端口
    echo "── 真实运行状态 ──────────────────────"
    if bot_running; then
        ok "机器人 运行中"
        [ -f "$BOT_LOG" ] || warn "  但它不是本脚本启动的，没有日志文件"
    else
        case "$(session_state "$BOT_SESSION")" in
            dead) warn "机器人 未运行（会话以 (Dead) 残留，启动时会自动清理）" ;;
            none) warn "机器人 未运行" ;;
            *)    warn "机器人 未运行（screen 会话还在，但里面已经没有进程了）" ;;
        esac
    fi

    if port_open; then
        ok "NapCat 运行中（$WS_URL 已就绪）"
    elif proc_alive "$NAPCAT_PROC"; then
        warn "NapCat 进程在，但 $WS_URL 没监听（多半是还没登录 QQ）"
        info "  登录：bash start.sh webui    # 直接打开扫码页面"
        info "  掉登录了也一样：说这条的日志是「连不上协议端」而不是「QQ 掉线」"
    else
        case "$(session_state "$NAPCAT_SESSION")" in
            dead) warn "NapCat 未运行（会话以 (Dead) 残留，启动时会自动清理）" ;;
            none) warn "NapCat 未运行" ;;
            *)    warn "NapCat 未运行（screen 会话还在，但里面已经没有进程了）" ;;
        esac
    fi

    echo "── QQ 登录状态 ───────────────────────"
    if port_open; then
        check_qq
    else
        warn "端口没开，跳过（先把 NapCat 跑起来）"
    fi

    echo "── 机器人日志尾部 ────────────────────"
    if [ -f "$BOT_LOG" ]; then
        tail -n 6 "$BOT_LOG"
        info "（完整日志：$BOT_LOG，实时跟随：bash start.sh log）"
    else
        warn "还没有日志文件（用本脚本启动后才会写）"
    fi

    echo
    if ! bot_running || ! port_open; then
        info "要拉起它们：bash start.sh"
    fi
    info "唤醒锁：Termux 重启后会失效，重跑一次 bash start.sh 即可"
}

# ------------------------------ 打开 WebUI ------------------------------
# 首次登录 QQ 要扫码：二维码画在 WebUI 页面上，而进这个页面需要地址 + token
# （token 每次安装随机生成，存在 webui.json 里）。与其让你进 screen 里手抄，
# 不如直接读文件拼出来，能弹浏览器就顺手弹出来。
do_webui() {
    require_python

    local url
    if ! url="$(WEBUI_JSON="$WEBUI_JSON" "$PYTHON_CMD" - <<'PY'
import json
import os
import sys

# 下面的报错信息带中文：locale 不是 UTF-8 时（Termux 上把 LANG 设成 C 很常见）
# 直接 print 会 UnicodeEncodeError，退出码就不是约定的 2/3 了，脚本也就给不出提示
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

path = os.environ["WEBUI_JSON"]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except OSError as e:
    print(f"读不到 {path}（{e.strerror}）", file=sys.stderr)
    sys.exit(2)
except ValueError as e:
    print(f"这个文件不是合法 JSON：{e}", file=sys.stderr)
    sys.exit(3)
print(f"http://127.0.0.1:{cfg.get('port', 6099)}/webui?token={cfg.get('token', '')}")
PY
    )"; then
        err "拿不到 WebUI 地址"
        info "要读的文件：$WEBUI_JSON"
        info "NapCat 装在别处的话：WEBUI_JSON=/实际路径 bash start.sh webui"
        exit 1
    fi

    if ! napcat_running; then
        warn "NapCat 现在没在跑，这个页面会打不开——先 bash start.sh"
    fi

    echo
    ok "NapCat WebUI："
    info "$url"
    if command -v termux-open-url >/dev/null 2>&1; then
        if termux-open-url "$url" >/dev/null 2>&1; then
            info "已在浏览器里打开（二维码就在页面上，扫它登录 QQ）"
        else
            warn "自动打开浏览器失败，把上面那条地址手动粘到浏览器里"
        fi
    else
        info "想让它自动开浏览器：pkg install termux-tools"
    fi
    info "登录成功后 NapCat 才会监听 $WS_URL，确认：bash start.sh status"
}

do_log() {
    if [ ! -f "$BOT_LOG" ]; then
        err "还没有日志文件，先 bash start.sh 启动"
        exit 1
    fi
    info "实时日志（Ctrl+C 退出，不影响后台运行）：$BOT_LOG"
    tail -n 50 -f "$BOT_LOG"
}

usage() {
    sed -n '3,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-start}" in
    start)  do_start ;;
    stop)   do_stop ;;
    status) do_status ;;
    webui)  do_webui ;;
    log)    do_log ;;
    help|-h|--help) usage ;;
    *)
        err "未知命令：$1"
        echo
        usage
        exit 1
        ;;
esac
