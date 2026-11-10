#!/bin/bash
# ============================================================
# CSI 采集一体化脚本 （纯bash，兼容老系统）
# ============================================================
# 用法：
#   1. 先编辑下方的 CONFIG 区域
#   2. sudo ./collect.sh <session_name>
#      示例：sudo ./collect.sh session_A
#            sudo ./collect.sh session_B
#            sudo ./collect.sh session_C
#            sudo ./collect.sh session_D
#
# 该脚本自动完成：
#   重置WiFi网卡 → 连接路由器WiFi → 启动CSI记录(log_to_file)
#   → 启动hping3(持续ping) → 标注控制 → 自动停止所有进程
# ============================================================

set -e

# ============================================================
# 配置区域（用户必填）
# ============================================================
CSI_DAT_DIR="./csi_dat_batch"                  # CSI 数据保存目录
CSI_LOG_DIR="./csi_log"                  # 标注日志保存目录
CSITOOL_PATH="../linux-80211n-csitool-supplementary"  # csitool 路径
ROUTER_IP="192.168.3.1"                 # 路由器 IP
PING_INTERVAL_US=2000                   # ping间隔(μs), 2000=500Hz
WIFI_SSID="HUAWEI-LabCSI_5G"            # 路由器 WiFi 名称
WIFI_PASS=""                             # 路由器 WiFi 密码（开放网络，无密码）
SESSION_DESC="session_default"          # 默认 Session 名称

# ============================================================
# 辅助函数
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CSI_DAT_SAVE_DIR="$(realpath -m "${SCRIPT_DIR}/${CSI_DAT_DIR}" 2>/dev/null || echo "${SCRIPT_DIR}/${CSI_DAT_DIR}")"
CSI_LOG_SAVE_DIR="$(realpath -m "${SCRIPT_DIR}/${CSI_LOG_DIR}" 2>/dev/null || echo "${SCRIPT_DIR}/${CSI_LOG_DIR}")"
PID_HPING=""
PID_LOG=""
SESSION_NAME=""
RUN_NUM=1
FILE_SEQ=1
CREATED_FILES=""
CSI_ERR_FILE=""

log_info()  { echo "[INFO]  $*"; }
log_ok()   { echo "[OK]    $*"; }
log_warn() { echo "[WARN]  $*"; }
log_err()  { echo "[ERROR] $*" >&2; }

# ============================================================
# 信号处理：随时 Ctrl+C 也能干净退出
# ============================================================
cleanup_exit() {
    echo ""
    log_warn "收到中断信号，清理中..."
    stop_all
    log_warn "已清理退出"
    exit 1
}
trap cleanup_exit SIGINT SIGTERM

stop_all() {
    # 停止 hping3
    if [ -n "$PID_HPING" ] && kill -0 "$PID_HPING" 2>/dev/null; then
        log_info "停止 hping3 (PID $PID_HPING)..."
        sudo kill "$PID_HPING" 2>/dev/null
        wait "$PID_HPING" 2>/dev/null
        log_ok "hping3 已停止"
    fi

    # 停止 log_to_file
    if [ -n "$PID_LOG" ] && kill -0 "$PID_LOG" 2>/dev/null; then
        log_info "停止 log_to_file (PID $PID_LOG)..."
        sudo kill "$PID_LOG" 2>/dev/null
        wait "$PID_LOG" 2>/dev/null
        log_ok "log_to_file 已停止"
        
        # 额外保险：如果 log_to_file 没退出，用 SIGKILL
        sleep 1
        if kill -0 "$PID_LOG" 2>/dev/null; then
            sudo kill -9 "$PID_LOG" 2>/dev/null
            wait "$PID_LOG" 2>/dev/null
        fi
    fi
}

stop_csi_recording() {
    if [ -n "$PID_LOG" ] && kill -0 "$PID_LOG" 2>/dev/null; then
        log_info "停止 log_to_file (PID $PID_LOG)..."
        sudo kill "$PID_LOG" 2>/dev/null
        wait "$PID_LOG" 2>/dev/null
        sleep 1
        if kill -0 "$PID_LOG" 2>/dev/null; then
            sudo kill -9 "$PID_LOG" 2>/dev/null
            wait "$PID_LOG" 2>/dev/null
        fi
    fi

    # 检查 log_to_file 的 stderr
    if [ -n "$CSI_ERR_FILE" ] && [ -s "$CSI_ERR_FILE" ]; then
        log_warn "log_to_file 输出错误信息:"
        while IFS= read -r line; do
            log_err "  $line"
        done < "$CSI_ERR_FILE"
    fi
    # 清理错误文件
    rm -f "$CSI_ERR_FILE" 2>/dev/null
    CSI_ERR_FILE=""
}

# 检查 log_to_file 是否意外退出（"No buffer space available"）
check_csi_health() {
    if [ -n "$PID_LOG" ] && ! kill -0 "$PID_LOG" 2>/dev/null; then
        log_err "CSI 记录(log_to_file) 已意外停止！"
        log_err "可能原因：No buffer space available"
        if [ -n "$CSI_ERR_FILE" ] && [ -s "$CSI_ERR_FILE" ]; then
            log_err "log_to_file 错误输出:"
            while IFS= read -r line; do
                log_err "  $line"
            done < "$CSI_ERR_FILE"
        fi
        log_err "本次采集数据可能不可用，建议重新采集"
        return 1
    fi
    return 0
}

# ============================================================
# 1. 重置 WiFi 网卡
# ============================================================
reset_wifi() {
    log_info "步骤1/5: 重置 WiFi 网卡..."
    sudo modprobe -r iwlwifi mac80211 2>/dev/null || true
    sudo modprobe iwlwifi connector_log=0x1 || {
        log_err "modprobe iwlwifi 失败，请检查驱动是否安装"
        exit 1
    }
    sudo killall wpa_supplicant 2>/dev/null || true
    sleep 1
    log_ok "WiFi 网卡已重置"
}

# ============================================================
# 2. 自动连接 WiFi
# ============================================================
connect_wifi() {
    log_info "步骤2/5: 连接 WiFi '$WIFI_SSID'..."

    # 默认使用 iwconfig + dhclient（兼容老旧 Ubuntu 系统）
    log_info "使用 iwconfig 连接..."
    sudo iwconfig wlan0 essid "$WIFI_SSID" 2>/dev/null || true
    sudo dhclient wlan0 2>/dev/null || true

    # 如果 iwconfig 方式未获得 IP 且 nmcli 可用，尝试 nmcli 作为备选
    local ip=""
    for i in $(seq 1 5); do
        ip=$(ip -4 addr show 2>/dev/null | grep -oP 'inet \K[\d.]+' | grep -v '127.0.0.1' | head -1)
        if [ -n "$ip" ]; then
            break
        fi
        sleep 2
    done

    if [ -z "$ip" ] && command -v nmcli &>/dev/null; then
        log_warn "iwconfig 未获取到 IP，尝试 nmcli..."
        if [ -n "$WIFI_PASS" ]; then
            nmcli dev wifi connect "$WIFI_SSID" password "$WIFI_PASS" 2>/dev/null || true
        else
            nmcli dev wifi connect "$WIFI_SSID" 2>/dev/null || true
        fi
    fi

    # 等待并检查连接
    for i in $(seq 1 10); do
        ip=$(ip -4 addr show 2>/dev/null | grep -oP 'inet \K[\d.]+' | grep -v '127.0.0.1' | head -1)
        if [ -n "$ip" ]; then
            log_ok "已连接 WiFi，IP: $ip"
            return 0
        fi
        sleep 2
    done

    log_err "连接 WiFi 超时，请检查 SSID/密码或手动连接"
    log_err "提示：连接后重新运行 ./collect.sh"
    exit 1
}

# ============================================================
# 3. 准备 CSI 输出目录
# ============================================================
prepare_csi_dir() {
    log_info "步骤3/5: 准备 CSI 目录..."
    mkdir -p "$CSI_DAT_SAVE_DIR"
    mkdir -p "$CSI_LOG_SAVE_DIR"
    log_ok "CSI 数据保存到: $CSI_DAT_SAVE_DIR"
    log_ok "标注日志保存到: $CSI_LOG_SAVE_DIR"
}

# ============================================================
# 5. 启动 hping3（按动作启动，有限包数，避免buffer满）
# ============================================================
start_hping_block() {
    local duration_sec="$1"
    # 计算包数：duration × (1,000,000 / interval_us)，多给 10% 余量
    local rate_hz=$((1000000 / PING_INTERVAL_US))
    local extra=$((duration_sec * rate_hz / 10))  # 10% 余量
    local packet_count=$((duration_sec * rate_hz + extra))

    log_info "启动 hping3 → ${ROUTER_IP}，${rate_hz}Hz，约 ${duration_sec}s (${packet_count} 包)"

    nohup sudo hping3 -1 -i "u${PING_INTERVAL_US}" -c "$packet_count" "$ROUTER_IP" >/dev/null 2>&1 &
    PID_HPING=$!
    disown "$PID_HPING" 2>/dev/null

    # 等待 hping3 自然结束（超时保护：duration + 10 秒）
    local waited=0
    local max_wait=$((duration_sec + 10))
    while [ "$waited" -lt "$max_wait" ]; do
        if ! kill -0 "$PID_HPING" 2>/dev/null; then
            # hping3 已自然结束
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # 超时了强制 kill
    log_warn "hping3 超时未结束，强制停止"
    sudo kill "$PID_HPING" 2>/dev/null
    wait "$PID_HPING" 2>/dev/null
}

# 清理 hping3（备用）
stop_hping() {
    if [ -n "$PID_HPING" ] && kill -0 "$PID_HPING" 2>/dev/null; then
        log_info "强制停止 hping3 (PID $PID_HPING)..."
        sudo kill "$PID_HPING" 2>/dev/null
        wait "$PID_HPING" 2>/dev/null
    fi
}

# ============================================================
# 标注控制器（纯 bash 版）
# ============================================================
annotate_session() {
    local session_name="$1"
    local anno_file="$2"
    local people_count="${3:-1}"

    SESSION_START_TIME=$(date +%s.%N)
    
    {
        echo "session_start, $SESSION_START_TIME, $session_name"
    } >> "$anno_file"

    # 子函数：显示醒目提示（大号字）
    show_prompt() {
        local text="$1"
        local duration="$2"
        local people="$3"
        
        # 清屏，减少干扰
        printf "\033[2J\033[H" 2>/dev/null || true
        
        # 大号分隔线
        echo ""
        echo "╔══════════════════════════════════════════════════════╗"
        echo "║                                                    ║"
        printf "║  %-51s║\n" "下一个动作：${text}"
        printf "║  %-51s║\n" "持续时间：${duration} 秒"
        printf "║  %-51s║\n" "当前人数：${people}"
        echo "║                                                    ║"
        echo "╚══════════════════════════════════════════════════════╝"
        echo ""

        # 尝试 figlet 大号字（如果装了的话）
        if command -v figlet &>/dev/null; then
            echo "$text" | figlet -f banner 2>/dev/null || echo "$text" | figlet 2>/dev/null || true
        else
            # fallback: 用重复的 # 拼出大字
            local len=${#text}
            local line=""
            local i=0
            while [ $i -lt $((len * 2 + 6)) ]; do
                line="${line}#"
                i=$((i + 1))
            done
            echo "$line"
            echo "##  $text  ##"
            echo "$line"
        fi
        echo ""
    }
    
    # 子函数：标注一个动作块
    annotate_action() {
        local action_name="$1"
        local duration="$2"
        local people="${3:-1}"
        local violence_label="${4:-}"

        # 生成当前动作的 CSI 文件名（FILE_SEQ 全局自增）
        local action_timestamp
        action_timestamp=$(date +%Y%m%d_%H%M%S)
        local seq_padded
        seq_padded=$(printf "%05d" "$FILE_SEQ")
        FILE_SEQ=$((FILE_SEQ + 1))
        local action_csi_file="${CSI_DAT_SAVE_DIR}/csi_${action_timestamp}_${SESSION_NAME}_${seq_padded}_${action_name}.dat"
        CREATED_FILES="${CREATED_FILES}${CREATED_FILES:+$'\n'}${action_csi_file}"

        # 显示醒目提示
        show_prompt "$action_name" "$duration" "$people"
        
        # 自动开始本段
        echo ""
        echo "===== [START] $action_name (${duration}s) ====="
        echo "[$(date +%H:%M:%S)] 开始: $action_name"
        echo ""
        
        # 启动本动作的 CSI 记录
        start_csi_recording_for_run "$action_csi_file"
        
        # hping 正式启动 — 大字提示
        printf "\033[2J\033[H" 2>/dev/null || true
        echo ""
        echo "===== [RECORDING] $action_name ====="
        echo ""
        if command -v figlet &>/dev/null; then
            echo "$action_name" | figlet -f banner 2>/dev/null || echo "$action_name" | figlet 2>/dev/null || true
        else
            local flen=${#action_name}
            local fline=""
            local fi=0
            while [ $fi -lt $((flen * 2 + 6)) ]; do
                fline="${fline}#"
                fi=$((fi + 1))
            done
            echo "$fline"
            echo "##  $action_name  ##"
            echo "$fline"
        fi
        echo ""
        
        # 启动本段 hping3（带 -c 限制，会自动结束）
        if start_hping_block "$duration"; then
            log_ok "hping3 正常结束"
        else
            log_warn "hping3 超时被强制终止，数据可能不完整"
        fi
        
        local t_start
        t_start=$(date +%s.%N)
        if [ -n "$violence_label" ]; then
            echo "action_start, $t_start, $action_name, people=$people, label=$violence_label, dat_file=$(basename "$action_csi_file")" >> "$anno_file"
        else
            echo "action_start, $t_start, $action_name, people=$people, dat_file=$(basename "$action_csi_file")" >> "$anno_file"
        fi

        # 逐秒倒计时
        local remaining=$duration
        while [ "$remaining" -ge 0 ]; do
            if [ "$remaining" -le 5 ]; then
                # 最后5秒：大字显示倒计时数字
                printf "\033[2J\033[H" 2>/dev/null || true
                echo ""
                echo "===== [RECORDING] $action_name — 剩余 ${remaining}s ====="
                echo ""
                if command -v figlet &>/dev/null; then
                    echo "$remaining" | figlet -f banner 2>/dev/null || echo "$remaining" | figlet 2>/dev/null || true
                else
                    local nlen=${#remaining}
                    local nline=""
                    local ni=0
                    while [ $ni -lt $((nlen * 6)) ]; do
                        nline="${nline}#"
                        ni=$((ni + 1))
                    done
                    echo "$nline"
                    echo "  $remaining  "
                    echo "$nline"
                fi
                echo ""
            elif [ $((remaining % 5)) -eq 0 ] || [ "$remaining" -le 3 ]; then
                printf "\r  [剩余 %2d 秒]  —— 动作: %s" "$remaining" "$action_name"
            fi
            # 检查 CSI 是否意外停止
            if ! check_csi_health; then
                break
            fi
            sleep 1
            remaining=$((remaining - 1))
        done
        echo ""
        echo "===== [END] $action_name ====="
        echo "[$(date +%H:%M:%S)] 结束: $action_name"
        echo ""
        
        local t_end
        t_end=$(date +%s.%N)
        if [ -n "$violence_label" ]; then
            echo "action_end, $t_end, $action_name, people=$people, label=$violence_label, dat_file=$(basename "$action_csi_file")" >> "$anno_file"
        else
            echo "action_end, $t_end, $action_name, people=$people, dat_file=$(basename "$action_csi_file")" >> "$anno_file"
        fi

        # 停止本动作的 CSI 记录
        stop_csi_recording

        # buffer 休息时间
        sleep 1
    }

    # 子函数：标注人数变化
    annotate_people_change() {
        local count="$1"
        local duration="$2"
        
        # 生成当前动作的 CSI 文件名（FILE_SEQ 全局自增）
        local action_timestamp
        action_timestamp=$(date +%Y%m%d_%H%M%S)
        local seq_padded
        seq_padded=$(printf "%05d" "$FILE_SEQ")
        FILE_SEQ=$((FILE_SEQ + 1))
        local action_csi_file="${CSI_DAT_SAVE_DIR}/csi_${action_timestamp}_${SESSION_NAME}_${seq_padded}_peoplecount.dat"
        CREATED_FILES="${CREATED_FILES}${CREATED_FILES:+$'\n'}${action_csi_file}"
        
        show_prompt "人数 → ${count}人" "$duration" "$count"
        
        echo ""
        echo "===== [START] 人数=${count}人 (${duration}s) ====="
        echo "[$(date +%H:%M:%S)] 开始: 人数=${count}人"
        echo ""
        
        # 启动本动作的 CSI 记录
        start_csi_recording_for_run "$action_csi_file"
        
        # hping 正式启动 — 大字提示
        printf "\033[2J\033[H" 2>/dev/null || true
        echo ""
        echo "===== [RECORDING] 人数=${count}人 ====="
        echo ""
        if command -v figlet &>/dev/null; then
            echo "${count}people" | figlet -f banner 2>/dev/null || echo "${count}people" | figlet 2>/dev/null || true
        else
            echo "########################"
            echo "##  人数=${count}  ##"
            echo "########################"
        fi
        echo ""
        
        # 启动本段 hping3（带 -c 限制，会自动结束）
        if start_hping_block "$duration"; then
            log_ok "hping3 正常结束"
        else
            log_warn "hping3 超时被强制终止，数据可能不完整"
        fi
        
        local t_start
        t_start=$(date +%s.%N)
        echo "people_change_start, $t_start, people=$count, dat_file=$(basename "$action_csi_file")" >> "$anno_file"

        local remaining=$duration
        while [ "$remaining" -ge 0 ]; do
            if [ "$remaining" -le 5 ]; then
                # 最后5秒：大字显示倒计时数字
                printf "\033[2J\033[H" 2>/dev/null || true
                echo ""
                echo "===== [RECORDING] 人数=${count}人 — 剩余 ${remaining}s ====="
                echo ""
                if command -v figlet &>/dev/null; then
                    echo "$remaining" | figlet -f banner 2>/dev/null || echo "$remaining" | figlet 2>/dev/null || true
                else
                    local nlen=${#remaining}
                    local nline=""
                    local ni=0
                    while [ $ni -lt $((nlen * 6)) ]; do
                        nline="${nline}#"
                        ni=$((ni + 1))
                    done
                    echo "$nline"
                    echo "  $remaining  "
                    echo "$nline"
                fi
                echo ""
            elif [ $((remaining % 5)) -eq 0 ] || [ "$remaining" -le 3 ]; then
                printf "\r  [剩余 %2d 秒]  —— 人数: %d" "$remaining" "$count"
            fi
            # 检查 CSI 是否意外停止
            if ! check_csi_health; then
                break
            fi
            sleep 1
            remaining=$((remaining - 1))
        done
        echo ""
        echo "===== [END] 人数=${count}人 ====="
        echo "[$(date +%H:%M:%S)] 结束: 人数=${count}人"
        echo ""

        local t_end
        t_end=$(date +%s.%N)
        echo "people_change_end, $t_end, people=$count, dat_file=$(basename "$action_csi_file")" >> "$anno_file"

        # 停止本动作的 CSI 记录
        stop_csi_recording

        # buffer 休息时间
        sleep 1
    }

    # ---------- 根据 Session 名称执行不同的动作序列 ----------
    case "$session_name" in
        session_A)
            # 0人基线（录制13段各3秒，与session_B时长一致）
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            annotate_action "emptyroom" 3 0
            ;;

        session_B)
            # 1人 HAR + 暴力模拟
            # 非暴力动作（label=0）
            annotate_action "standstill" 3 1 0
            annotate_action "walk" 3 1 0
            annotate_action "sit" 3 1 0
            annotate_action "standup" 3 1 0
            annotate_action "wave" 3 1 0
            annotate_action "bend" 3 1 0
            annotate_action "jump" 3 1 0
            # 暴力模拟动作（label=1）
            annotate_action "kicksim1" 3 1 1
            annotate_action "kicksim2" 3 1 1
            annotate_action "kicksim3" 3 1 1
            annotate_action "punchsim1" 3 1 1
            annotate_action "punchsim2" 3 1 1
            annotate_action "punchsim3" 3 1 1
            ;;

        session_C)
            # 2人交互动作识别（含暴力/非暴力标签）
            # 非暴力动作（label=0）
            annotate_action "approach" 3 2 0
            annotate_action "leave" 3 2 0
            annotate_action "handshake" 3 2 0
            annotate_action "highfive" 3 2 0
            annotate_action "pointleft" 3 2 0
            annotate_action "pointright" 3 2 0
            # 暴力动作（label=1）
            annotate_action "push" 3 2 1
            annotate_action "kickleft" 3 2 1
            annotate_action "kickright" 3 2 1
            annotate_action "punchleft" 3 2 1
            annotate_action "punchright" 3 2 1
            ;;

        session_D)
            # 人数检测 — 接收人数参数，跑固定人数
            annotate_people_change "$people_count" 30
            ;;

        *)
            log_err "未知 Session: $session_name"
            log_err "可选: session_A / session_B / session_C / session_D"
            stop_all
            exit 1
            ;;
    esac

    # Session 结束
    local t_end
    t_end=$(date +%s.%N)
    echo "session_end, $t_end, $session_name" >> "$anno_file"
}

# ============================================================
# 主函数
# ============================================================
main() {
    local repeat=1
    local session_name=""
    local people_count=1

    # 解析命令行参数
    while getopts "r:" opt; do
        case $opt in
            r) repeat="$OPTARG" ;;
            *) 
                echo "用法: $0 [-r N] <session_name> [people_count]"
                echo "  -r N      重复采集 N 次"
                echo "  session_A   空房间基线"
                echo "  session_B   单人 HAR + 暴力模拟"
                echo "  session_C   双人暴力行为检测"
                echo "  session_D   人数检测（需指定人数）"
                echo ""
                echo "例子:"
                echo "  sudo $0 session_B            单次 B"
                echo "  sudo $0 -r 3 session_B       重复 B 三次"
                echo "  sudo $0 session_D 3          3 人数检测"
                echo "  sudo $0 -r 5 session_D 2     2 人检测重复五次"
                exit 1
                ;;
        esac
    done
    shift $((OPTIND - 1))
    session_name="${1:-$SESSION_DESC}"
    people_count="${2:-1}"

    # 检查 root 权限
    if [ "$EUID" -ne 0 ]; then
        log_err "需要 root 权限运行 (sudo)"
        exit 1
    fi

    # 参数校验
    if [ "$session_name" = "session_D" ]; then
        if [ "$people_count" -lt 1 ] || [ "$people_count" -gt 10 ]; then
            log_err "人数检测需要指定人数 (1-10)"
            log_err "用法: sudo $0 session_D 3"
            exit 1
        fi
    elif [ "$session_name" = "session_A" ] || [ "$session_name" = "session_B" ] || [ "$session_name" = "session_C" ]; then
        # 这些 session 忽略 people_count，使用硬编码的值
        :
    else
        log_err "未知 Session: $session_name"
        log_err "可选: session_A / session_B / session_C / session_D"
        exit 1
    fi

    if [ "$repeat" -lt 1 ]; then
        log_err "重复次数必须 >= 1"
        exit 1
    fi

    # 显示总览
    if [ "$session_name" = "session_D" ]; then
        echo ""
        echo "========================================================"
        echo " CSI 采集一体化脚本"
        echo " Session: $session_name (${people_count}人)"
        echo " 重复:    ${repeat} 次"
        echo " 时间:    $(date)"
        echo "========================================================"
        echo ""
    else
        echo ""
        echo "========================================================"
        echo " CSI 采集一体化脚本"
        echo " Session: $session_name"
        echo " 重复:    ${repeat} 次"
        echo " 时间:    $(date)"
        echo "========================================================"
        echo ""
    fi

    # 只做一次准备工作（重复采集不重复重置网卡）
    reset_wifi
    connect_wifi
    prepare_csi_dir

    # 循环采集
    local run=1
    while [ "$run" -le "$repeat" ]; do
        local timestamp
        timestamp=$(date +%Y%m%d_%H%M%S)

        # 文件名使用与 .dat 一致的前缀格式
        local run_padded
        run_padded=$(printf "%05d" "$run")
        local anno_file_name="csi_${timestamp}_${session_name}_${run_padded}_annotation.log"
        local anno_full="${CSI_LOG_SAVE_DIR}/${anno_file_name}"
        CREATED_FILES=""
        CREATED_FILES="${CREATED_FILES}${CREATED_FILES:+$'\n'}${anno_full}"

        echo ""
        echo "========================================================"
        echo "  第 ${run}/${repeat} 次采集"
        echo "  标注文件:  ${anno_full}"
        echo "========================================================"
        echo ""

        # 自动开始采集
        echo ""
        echo "===== [START] 第 ${run}/${repeat} 次采集 ====="
        echo ""

        SESSION_NAME="$session_name"

        # 运行标注（每个动作内部独立启停 CSI 记录）
        annotate_session "$session_name" "$anno_full" "$people_count"

        echo ""
        echo "===== [END] 第 ${run}/${repeat} 次采集 ====="
        echo ""

        log_ok "第 ${run}/${repeat} 次完成"
        log_ok "标注文件: ${anno_full}"

        run=$((run + 1))

        # 如果不是最后一次，稍作休息
        if [ "$run" -le "$repeat" ]; then
            echo ""
            echo "准备下一次采集..."
            echo "（如需更换受试者，现在操作）"
            sleep 3
        fi
    done

    echo ""
    echo "========================================================"
    log_ok "全部 ${repeat} 次采集完成"
    echo "========================================================"
    echo ""
    echo "本轮采集文件:"
    if [ -n "$CREATED_FILES" ]; then
        echo "$CREATED_FILES" | while IFS= read -r f; do
            [ -n "$f" ] && ls -lh "$f" 2>/dev/null
        done
    fi
}

# 为单次 run 启动 CSI 记录
start_csi_recording_for_run() {
    local csi_file="$1"

    local log_to_file=""
    for candidate in \
        "${SCRIPT_DIR}/${CSITOOL_PATH}/netlink/log_to_file" \
        "${SCRIPT_DIR}/../linux-80211n-csitool-supplementary/netlink/log_to_file" \
        "log_to_file"; do
        if [ -x "$candidate" ]; then
            log_to_file="$candidate"
            break
        fi
    done

    if [ -z "$log_to_file" ]; then
        log_err "找不到 log_to_file！"
        log_err "请设置正确 CSITOOL_PATH"
        exit 1
    fi

    local err_file="${csi_file}.err"
    nohup sudo "$log_to_file" "$csi_file" >/dev/null 2>"$err_file" &
    PID_LOG=$!
    disown "$PID_LOG" 2>/dev/null
    CSI_ERR_FILE="$err_file"

    sleep 1
    if kill -0 "$PID_LOG" 2>/dev/null; then
        log_ok "CSI 记录已启动 (PID $PID_LOG), 输出: $csi_file"
    else
        log_err "CSI 记录启动失败"
        exit 1
    fi
}

main "$@"
