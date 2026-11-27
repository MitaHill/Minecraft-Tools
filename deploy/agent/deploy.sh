#!/usr/bin/env bash
# Deploy one MinecraftTools FRP agent on a Debian/Ubuntu host.
#
# The agent generates its own stable node id in the data volume and the
# control server registers unknown agents automatically. No node id, node
# secret or frpc login token is required here.
set -Eeuo pipefail

CONTROL_URL="https://ctl.mctools.mitahill.com"
DEFAULT_DIR="/opt/minecraft-tools/agent"
AGENT_IMAGE="kindmitaishere/minecrafttools-frp-agent"
DEFAULT_AGENT_TAG="2026-11-27-8"
DEFAULT_PORTS="30000-30015"
MAX_PORTS=16
MIN_PORT=1024
MAX_PORT=65535

# Environment values are captured before the working variables are initialized,
# so an unset/empty default does not erase what the caller provided.
ENV_NODE_NAME=${NODE_NAME:-}
ENV_PORTS=${PORTS:-}
ENV_COMPOSE_DIR=${COMPOSE_DIR:-}
ENV_TAG=${AGENT_IMAGE_TAG:-}
ENV_PROJECT=${PROJECT_NAME:-}
ENV_PUBLIC_PORT=${FRP_PUBLIC_CONTROL_PORT:-}
ENV_BIND=${NODE_BIND_ADDRESS:-}
ENV_PUBLIC_HOST=${NODE_PUBLIC_HOST:-}
ENV_CA=${CONTROL_CA_FILE:-}

DIR_ARG=""
PORTS_ARG=""
NAME_ARG=""
TAG_ARG=""
PROJECT_ARG=""
PUBLIC_PORT_ARG=""
BIND_ARG=""
PUBLIC_HOST_ARG=""
CA_FILE_ARG=""
CONTROL_URL_ARG=""

PUBLIC_PORT_SET=no
BIND_SET=no
PUBLIC_HOST_SET=no
CA_SET=no
CHANGES=no

START_ONLY=no
FORCE=no
DRY_RUN=no
ASSUME_YES=no
SELF_TEST=no
SHOW_HELP=no

NODE_NAME=""
PORT_SPEC=""
PORT_COUNT=0
RANGE_STARTS=()
RANGE_ENDS=()
DIR=""
PROJECT=""
NODE_PUBLIC_HOST=""
FRP_PUBLIC_CONTROL_PORT=7000
NODE_BIND_ADDRESS="0.0.0.0"
BIND_COMPOSE="0.0.0.0"
AGENT_IMAGE_TAG=""
CONTROL_CA_FILE=""
FRP_TOKEN=""
EXISTING=no
BACKUP_DIR=""

usage() {
    cat <<'USAGE'
用法: deploy.sh [选项]

在 Debian/Ubuntu 上部署一个 MinecraftTools FRP Agent。首次安装会询问
节点名称、端口范围和 Compose 目录；已有 deploy.env 时会直接复用并启动。

选项:
  --dir PATH             Compose 目录（默认 /opt/minecraft-tools/agent）
  --ports SPEC           端口范围，例如 10000-10002,10004-10005（最多 16 个）
  --name NAME            节点名称（用于管理台显示和 Compose 项目名）
  --tag TAG              手动指定 Agent 镜像标签（默认取最新稳定版）
  --project NAME         Compose 项目名（默认 minecrafttools-<节点名>）
  --public-port PORT     frpc 连接端口（默认 7000）
  --bind ADDRESS         宿主机监听地址（默认 0.0.0.0，可为 ::）
  --public-host HOST     Agent 上报给控制端的公网主机，可选
  --ca-file PATH         内网控制端 CA 文件，可选
  --control-url URL      仅测试用，覆盖硬编码的控制端地址
  --start                只启动已有目录，不写文件
  --force                允许覆盖非部署目录并强制重建
  --dry-run              只检查并打印计划，不写文件、不启动
  --yes                  非交互；所有必填项必须由参数或环境变量提供
  --self-test            运行端口解析自检后退出
  -h, --help             显示帮助

环境变量:
  NODE_NAME, PORTS, COMPOSE_DIR, AGENT_IMAGE_TAG, PROJECT_NAME,
  FRP_PUBLIC_CONTROL_PORT, NODE_BIND_ADDRESS, NODE_PUBLIC_HOST,
  CONTROL_CA_FILE
USAGE
}

info() { printf '%s\n' "$*"; }
warn() { printf '警告: %s\n' "$*" >&2; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

ask() {
    local label=$1 default=${2:-} value
    if [ "$ASSUME_YES" = yes ] || [ ! -t 0 ]; then
        die "非交互运行需要提供: $label"
    fi
    if [ -n "$default" ]; then
        printf '%s [%s]: ' "$label" "$default" >&2
    else
        printf '%s: ' "$label" >&2
    fi
    IFS= read -r value || die "读取输入失败"
    [ -n "$value" ] || value=$default
    [ -n "$value" ] || die "$label 不能为空"
    printf '%s' "$value"
}

validate_name() {
    local name=$1
    [ -n "$name" ] || return 1
    [ "${#name}" -le 80 ] || return 1
    case "$name" in
        *$'\n'*|*$'\r'*|*$'\t'*) return 1 ;;
    esac
    return 0
}

sanitize_name() {
    local value
    value=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//; s/-*$//')
    [ -n "$value" ] || value=agent
    printf '%s' "${value:0:50}"
}

yaml_quote() {
    local value=$1
    value=${value//\'/\'\'}
    printf "'%s'" "$value"
}

validate_dir() {
    local path=$1 candidate
    case "$path" in
        /*) ;;
        *) return 1 ;;
    esac
    [ "$path" != "/" ] || return 1
    case "$path" in
        *[[:space:]]*|*'#'*|*'='*|*':'*|*'$'*|*'\\'*) return 1 ;;
    esac
    if command -v realpath >/dev/null 2>&1; then
        candidate=$(realpath -m -- "$path" 2>/dev/null || true)
        [ -n "$candidate" ] && path=$candidate
    fi
    printf '%s' "$path"
}

setup_bind() {
    local value=$1
    case "$value" in
        *[[:space:]]*) die "监听地址不能包含空白" ;;
    esac
    [[ $value =~ ^[0-9A-Fa-f:.]+$ ]] || die "监听地址必须是 IPv4 或 IPv6（不带方括号）"
    if [[ $value == *:* ]]; then
        BIND_COMPOSE="[$value]"
    else
        BIND_COMPOSE=$value
    fi
}

check_reserved() {
    local start=$1 end=$2 port
    for port in 7000 7400 9000 "$FRP_PUBLIC_CONTROL_PORT"; do
        if [ "$start" -le "$port" ] && [ "$port" -le "$end" ]; then
            warn "端口 $port 被控制端口/管理端口占用"
            return 1
        fi
    done
    return 0
}

# parse_ports fills RANGE_STARTS/RANGE_ENDS/PORT_SPEC/PORT_COUNT.
# It returns non-zero instead of exiting so --self-test can check failures.
parse_ports() {
    local spec=$1 part start end i j n last key_start key_end total
    local -a parts=() starts=() ends=()
    IFS=',' read -r -a parts <<< "$spec"
    [ "${#parts[@]}" -gt 0 ] || return 1
    for part in "${parts[@]}"; do
        part=${part//[[:space:]]/}
        [ -n "$part" ] || return 1
        if [[ $part =~ ^([0-9]{1,5})-([0-9]{1,5})$ ]]; then
            start=${BASH_REMATCH[1]}
            end=${BASH_REMATCH[2]}
        elif [[ $part =~ ^([0-9]{1,5})$ ]]; then
            start=${BASH_REMATCH[1]}
            end=$start
        else
            return 1
        fi
        start=$((10#$start))
        end=$((10#$end))
        if [ "$start" -lt "$MIN_PORT" ] || [ "$start" -gt "$MAX_PORT" ] ||
            [ "$end" -lt "$MIN_PORT" ] || [ "$end" -gt "$MAX_PORT" ] ||
            [ "$end" -lt "$start" ]; then
            return 1
        fi
        starts+=("$start")
        ends+=("$end")
    done

    n=${#starts[@]}
    for ((i = 1; i < n; i++)); do
        key_start=${starts[i]}
        key_end=${ends[i]}
        j=$((i - 1))
        while [ "$j" -ge 0 ] && [ "${starts[j]}" -gt "$key_start" ]; do
            starts[j + 1]=${starts[j]}
            ends[j + 1]=${ends[j]}
            j=$((j - 1))
        done
        starts[j + 1]=$key_start
        ends[j + 1]=$key_end
    done

    RANGE_STARTS=()
    RANGE_ENDS=()
    for ((i = 0; i < n; i++)); do
        last=$(( ${#RANGE_STARTS[@]} - 1 ))
        if [ "$last" -ge 0 ] && [ "${starts[i]}" -le "${RANGE_ENDS[last]}" ]; then
            return 1
        fi
        if [ "$last" -ge 0 ] && [ "${starts[i]}" -eq $(( ${RANGE_ENDS[last]} + 1 )) ]; then
            RANGE_ENDS[last]=${ends[i]}
        else
            RANGE_STARTS+=("${starts[i]}")
            RANGE_ENDS+=("${ends[i]}")
        fi
    done

    total=0
    for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
        total=$((total + RANGE_ENDS[i] - RANGE_STARTS[i] + 1))
    done
    if [ "$total" -gt "$MAX_PORTS" ]; then
        return 1
    fi
    for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
        check_reserved "${RANGE_STARTS[i]}" "${RANGE_ENDS[i]}" || return 1
    done

    PORT_SPEC=""
    for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
        if [ -n "$PORT_SPEC" ]; then
            PORT_SPEC="$PORT_SPEC,"
        fi
        if [ "${RANGE_STARTS[i]}" -eq "${RANGE_ENDS[i]}" ]; then
            PORT_SPEC="$PORT_SPEC${RANGE_STARTS[i]}"
        else
            PORT_SPEC="$PORT_SPEC${RANGE_STARTS[i]}-${RANGE_ENDS[i]}"
        fi
    done
    PORT_COUNT=$total
    return 0
}

check_ephemeral() {
    local low high i
    [ -r /proc/sys/net/ipv4/ip_local_port_range ] || return 0
    read -r low high < /proc/sys/net/ipv4/ip_local_port_range || return 0
    for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
        if [ "${RANGE_STARTS[i]}" -le "$high" ] && [ "${RANGE_ENDS[i]}" -ge "$low" ]; then
            warn "端口范围与 ip_local_port_range ($low-$high) 重叠，可能影响出站连接"
            return 0
        fi
    done
}

check_listening_ports() {
    command -v ss >/dev/null 2>&1 || return 0
    local occupied port conflicts="" i
    occupied=$(ss -H -ltn 2>/dev/null | sed 's/.*://' | tr -d ' ' | sort -un || true)
    [ -n "$occupied" ] || return 0
    for port in $occupied; do
        case $port in
            ''|*[!0-9]*) continue ;;
        esac
        for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
            if [ "$port" -ge "${RANGE_STARTS[i]}" ] && [ "$port" -le "${RANGE_ENDS[i]}" ]; then
                conflicts="$conflicts $port"
            fi
        done
    done
    if [ -n "$conflicts" ]; then
        die "宿主机已有服务监听端口:$conflicts"
    fi
}

check_existing_containers() {
    local names
    names=$(docker ps -a --format '{{.Names}}' 2>/dev/null |
        grep -E '^mitahill-mctools-agent(-net|-firewall|-frps)?$' || true)
    if [ -n "$names" ]; then
        die "宿主机已有节点容器（$(printf '%s' "$names" | tr '\n' ' ')）；请先停止并删除旧部署，或进入原目录使用 --start/--force"
    fi
}

# Bash 3.2 (the macOS default) lacks local -n; use a global loop variable.
i=0

self_test() {
    local failures=0
    FRP_PUBLIC_CONTROL_PORT=7000

    if ! parse_ports "10000-10002,10004-10005" ||
        [ "$PORT_COUNT" -ne 5 ] || [ "$PORT_SPEC" != "10000-10002,10004-10005" ]; then
        printf '自检失败: 多段端口\n' >&2
        failures=$((failures + 1))
    fi
    if ! parse_ports "10000-10002, 10003-10004" ||
        [ "$PORT_COUNT" -ne 5 ] || [ "$PORT_SPEC" != "10000-10004" ]; then
        printf '自检失败: 相邻段合并\n' >&2
        failures=$((failures + 1))
    fi
    if ! parse_ports "10000-10015" || [ "$PORT_COUNT" -ne 16 ]; then
        printf '自检失败: 16 个端口上限\n' >&2
        failures=$((failures + 1))
    fi
    if parse_ports "10000-10016" 2>/dev/null; then
        printf '自检失败: 超过 16 个端口未被拒绝\n' >&2
        failures=$((failures + 1))
    fi
    if parse_ports "10000-10002,10002-10003" 2>/dev/null; then
        printf '自检失败: 重叠端口未被拒绝\n' >&2
        failures=$((failures + 1))
    fi
    if parse_ports "1000-1002" 2>/dev/null; then
        printf '自检失败: 低于 1024 的端口未被拒绝\n' >&2
        failures=$((failures + 1))
    fi
    if parse_ports "7000" 2>/dev/null; then
        printf '自检失败: 保留端口未被拒绝\n' >&2
        failures=$((failures + 1))
    fi
    if parse_ports "abc" 2>/dev/null || parse_ports "10000-,10002" 2>/dev/null; then
        printf '自检失败: 非法格式未被拒绝\n' >&2
        failures=$((failures + 1))
    fi
    if [ "$failures" -ne 0 ]; then
        printf '自检失败: %d 项\n' "$failures" >&2
        return 1
    fi
    printf '自检通过\n'
}

detect_os() {
    [ -r /etc/os-release ] || die "无法读取 /etc/os-release，仅支持 Debian/Ubuntu"
    local id id_like
    id=$(sed -n 's/^ID=//p' /etc/os-release | head -1 | tr -d '"')
    id_like=$(sed -n 's/^ID_LIKE=//p' /etc/os-release | head -1 | tr -d '"')
    case "$id $id_like" in
        *debian*|*ubuntu*) ;;
        *) die "仅支持 Debian/Ubuntu（当前 ID=$id ID_LIKE=$id_like）" ;;
    esac
    command -v apt-get >/dev/null 2>&1 || die "未找到 apt-get，仅支持 Debian/Ubuntu"
}

detect_docker() {
    command -v docker >/dev/null 2>&1 || die "未安装 Docker。Debian/Ubuntu 可参考 https://docs.docker.com/engine/install/ 安装 docker.io 或官方仓库"
    docker compose version >/dev/null 2>&1 || die "未安装 Docker Compose v2。安装 docker-compose-plugin（官方仓库）或 docker-compose-v2（Ubuntu）后重试"
    docker info >/dev/null 2>&1 || die "无法连接 Docker daemon，请确认服务已启动且当前用户有权限"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo $0"
}

load_existing() {
    local env_file="$DIR/deploy.env"
    EXISTING_NAME=$(sed -n 's/^# NODE_NAME: //p' "$env_file" | head -1 || true)
    EXISTING_PORTS=$(sed -n 's/^NODE_PORT_SPEC=//p' "$env_file" | tail -1 || true)
    if [ -z "$EXISTING_PORTS" ]; then
        local start end
        start=$(sed -n 's/^NODE_PORT_START=//p' "$env_file" | tail -1 || true)
        end=$(sed -n 's/^NODE_PORT_END=//p' "$env_file" | tail -1 || true)
        [ -n "$start" ] && [ -n "$end" ] && EXISTING_PORTS="$start-$end"
    fi
    EXISTING_TAG=$(sed -n 's/^AGENT_IMAGE_TAG=//p' "$env_file" | tail -1 || true)
    EXISTING_PROJECT=$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$env_file" | tail -1 || true)
    EXISTING_PUBLIC_PORT=$(sed -n 's/^FRP_PUBLIC_CONTROL_PORT=//p' "$env_file" | tail -1 || true)
    EXISTING_BIND=$(sed -n 's/^NODE_BIND_ADDRESS=//p' "$env_file" | tail -1 || true)
    EXISTING_PUBLIC_HOST=$(sed -n 's/^NODE_PUBLIC_HOST=//p' "$env_file" | tail -1 || true)
    EXISTING_CA=$(sed -n 's/^CONTROL_CA_FILE=//p' "$env_file" | tail -1 || true)
    EXISTING_CONTROL_URL=$(sed -n 's/^CONTROL_URL=//p' "$env_file" | tail -1 || true)
}

any_changes() {
    [ "$PORTS_ARG" != "" ] || [ "$NAME_ARG" != "" ] || [ "$TAG_ARG" != "" ] ||
        [ "$PROJECT_ARG" != "" ] || [ "$PUBLIC_PORT_SET" = yes ] || [ "$BIND_SET" = yes ] ||
        [ "$PUBLIC_HOST_SET" = yes ] || [ "$CA_SET" = yes ] || [ "$CHANGES" = yes ] ||
        [ "$FORCE" = yes ]
}

compose_cmd() {
    docker compose --project-name "$PROJECT" --env-file "$DIR/deploy.env" -f "$DIR/compose.yaml" "$@"
}

write_compose() {
    local name_yaml host_yaml first_port last_port
    name_yaml=$(yaml_quote "$NODE_NAME")
    host_yaml=$(yaml_quote "$NODE_PUBLIC_HOST")
    first_port=${RANGE_STARTS[0]}
    last_port=${RANGE_ENDS[${#RANGE_ENDS[@]} - 1]}
    {
        cat <<EOF
# 由 deploy/agent/deploy.sh 生成；节点身份由 Agent 自动生成。
services:
  node-net:
    container_name: mitahill-mctools-agent-net
    image: alpine:3.22
    command: ["tail", "-f", "/dev/null"]
    networks: [node]
    ports:
      - "${BIND_COMPOSE}:${FRP_PUBLIC_CONTROL_PORT}:7000"
EOF
        local idx
        for ((idx = 0; idx < ${#RANGE_STARTS[@]}; idx++)); do
            if [ "${RANGE_STARTS[idx]}" -eq "${RANGE_ENDS[idx]}" ]; then
                printf '      - "%s:%s:%s"\n' "$BIND_COMPOSE" "${RANGE_STARTS[idx]}" "${RANGE_STARTS[idx]}"
            else
                printf '      - "%s:%s-%s:%s-%s"\n' "$BIND_COMPOSE" \
                    "${RANGE_STARTS[idx]}" "${RANGE_ENDS[idx]}" \
                    "${RANGE_STARTS[idx]}" "${RANGE_ENDS[idx]}"
            fi
        done
        cat <<EOF
    cap_drop: [ALL]
    restart: unless-stopped

  firewall:
    container_name: mitahill-mctools-agent-firewall
    image: ${AGENT_IMAGE}:${AGENT_IMAGE_TAG}
    command: ["firewall-helper"]
    user: "0:10001"
    network_mode: "service:node-net"
    depends_on:
      node-net:
        condition: service_started
        restart: true
    cap_drop: [ALL]
    cap_add: [NET_ADMIN]
    security_opt: ["no-new-privileges:true"]
    environment:
      NODE_PORT_START: "${first_port}"
      NODE_PORT_END: "${last_port}"
    volumes:
      - firewall-data:/var/lib/minecrafttools-firewall
      - firewall-run:/run/minecrafttools
    healthcheck:
      test: ["CMD", "/usr/local/bin/frp-agent", "firewall-health"]
      interval: 3s
      timeout: 2s
      retries: 6
    restart: unless-stopped

  agent:
    container_name: mitahill-mctools-agent
    image: ${AGENT_IMAGE}:${AGENT_IMAGE_TAG}
    networks: [node]
    depends_on:
      firewall:
        condition: service_healthy
        restart: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    environment:
      CONTROL_URL: "${CONTROL_URL}"
      CONTROL_CA_FILE: "${CONTROL_CA_FILE}"
      CONTROL_CLIENT_CERT_FILE: ""
      CONTROL_CLIENT_KEY_FILE: ""
      NODE_NAME: ${name_yaml}
      NODE_PUBLIC_HOST: ${host_yaml}
      FRP_PUBLIC_CONTROL_PORT: "${FRP_PUBLIC_CONTROL_PORT}"
      NODE_PORT_START: "${first_port}"
      NODE_PORT_END: "${last_port}"
      FRPS_ADMIN_URL: http://node-net:7400
      FRPS_ADMIN_USER: admin
      FRPS_ADMIN_PASSWORD_FILE: /run/node-secrets/frps-admin-password
      PLUGIN_LISTEN: 0.0.0.0:9000
    volumes:
      - node-data:/data
      - firewall-run:/run/minecrafttools
      - ${DIR}/secrets:/run/node-secrets:ro
    restart: unless-stopped

  frps:
    container_name: mitahill-mctools-agent-frps
    image: ${AGENT_IMAGE}:${AGENT_IMAGE_TAG}
    command: ["frps", "-c", "/etc/frp/frps.toml"]
    user: "10001:10001"
    network_mode: "service:node-net"
    depends_on:
      node-net:
        condition: service_started
        restart: true
      firewall:
        condition: service_healthy
        restart: true
      agent:
        condition: service_started
    cap_drop: [ALL]
    volumes:
      - ${DIR}/frps.toml:/etc/frp/frps.toml:ro
    restart: unless-stopped

networks:
  node:
    driver: bridge

volumes:
  node-data:
  firewall-data:
  firewall-run:
EOF
    } > "$DIR/compose.yaml"
}

write_env() {
    local first_port last_port
    first_port=${RANGE_STARTS[0]}
    last_port=${RANGE_ENDS[${#RANGE_ENDS[@]} - 1]}
    cat > "$DIR/deploy.env" <<EOF
# NODE_NAME: ${NODE_NAME}
# 由 deploy/agent/deploy.sh 生成；不要在这里写 NODE_ID 或 secret。
COMPOSE_PROJECT_NAME=${PROJECT}
AGENT_IMAGE_TAG=${AGENT_IMAGE_TAG}
CONTROL_URL=${CONTROL_URL}
CONTROL_CA_FILE=${CONTROL_CA_FILE}
CONTROL_CLIENT_CERT_FILE=
CONTROL_CLIENT_KEY_FILE=
NODE_PUBLIC_HOST=${NODE_PUBLIC_HOST}
FRP_PUBLIC_CONTROL_PORT=${FRP_PUBLIC_CONTROL_PORT}
NODE_BIND_ADDRESS=${NODE_BIND_ADDRESS}
NODE_PORT_START=${first_port}
NODE_PORT_END=${last_port}
NODE_PORT_SPEC=${PORT_SPEC}
NODE_SECRETS_DIRECTORY=${DIR}/secrets
FRPS_CONFIG=${DIR}/frps.toml
FRPS_ADMIN_USER=admin
EOF
}

write_frps() {
    local proxy_bind="0.0.0.0" token_escape password
    if [[ $NODE_BIND_ADDRESS == *:* ]]; then
        proxy_bind="::"
    fi
    password=$(tr -d '\r\n' < "$DIR/secrets/frps-admin-password")
    token_escape=""
    if [ -n "$FRP_TOKEN" ]; then
        token_escape=${FRP_TOKEN//\\/\\\\}
        token_escape=${token_escape//\"/\\\"}
    fi
    {
        printf 'bindPort = 7000\n'
        printf 'proxyBindAddr = "%s"\n' "$proxy_bind"
        if [ -n "$token_escape" ]; then
            printf 'auth.token = "%s"\n' "$token_escape"
        fi
        printf '\nallowPorts = [\n'
        local idx
        for ((idx = 0; idx < ${#RANGE_STARTS[@]}; idx++)); do
            printf '  { start = %s, end = %s },\n' "${RANGE_STARTS[idx]}" "${RANGE_ENDS[idx]}"
        done
        printf ']\n\n'
        printf 'webServer.addr = "0.0.0.0"\n'
        printf 'webServer.port = 7400\n'
        printf 'webServer.user = "admin"\n'
        printf 'webServer.password = "%s"\n\n' "$password"
        printf '[[httpPlugins]]\n'
        printf 'name = "node-agent"\n'
        printf 'addr = "agent:9000"\n'
        printf 'path = "/handler"\n'
        printf 'ops = ["Login", "NewProxy", "CloseProxy", "NewUserConn"]\n'
    } > "$DIR/frps.toml"
}

random_hex() {
    od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
}

# Use the latest non-prerelease GitHub release so the operator never has to
# copy an image tag by hand. Fall back to the tag this script shipped with.
latest_stable_tag() {
    local tag=""
    if command -v curl >/dev/null 2>&1; then
        tag=$(curl -4 -fsSL --max-time 8 \
            https://api.github.com/repos/MitaHill/Minecraft-Tools/releases/latest 2>/dev/null |
            sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)
    elif command -v wget >/dev/null 2>&1; then
        tag=$(wget -4 -qO- --timeout=8 \
            https://api.github.com/repos/MitaHill/Minecraft-Tools/releases/latest 2>/dev/null |
            sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)
    fi
    case "$tag" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[1-9]*) printf '%s' "$tag" ;;
        *) printf '%s' "$DEFAULT_AGENT_TAG" ;;
    esac
}

prepare_secrets() {
    mkdir -p "$DIR/secrets"
    if [ ! -s "$DIR/secrets/frps-admin-password" ]; then
        random_hex > "$DIR/secrets/frps-admin-password"
    fi
    if [ -s "$DIR/secrets/frp-token" ]; then
        FRP_TOKEN=$(tr -d '\r\n' < "$DIR/secrets/frp-token")
    fi
    # 旧目录可能只在 frps.toml 里保存了 auth.token；更新时保留它，避免旧客户端断线。
    if [ -z "$FRP_TOKEN" ] && [ -f "$DIR/frps.toml" ]; then
        FRP_TOKEN=$(sed -n 's/^auth\.token *= *"\(.*\)"/\1/p' "$DIR/frps.toml" | head -1 || true)
    fi
    if [ -n "$CA_FILE_ARG" ]; then
        [ -r "$CA_FILE_ARG" ] || die "无法读取 CA 文件: $CA_FILE_ARG"
        cp -f "$CA_FILE_ARG" "$DIR/secrets/control-ca.pem"
    fi
    if [ -s "$DIR/secrets/control-ca.pem" ]; then
        CONTROL_CA_FILE="/run/node-secrets/control-ca.pem"
    else
        CONTROL_CA_FILE=""
    fi
}

backup_files() {
    BACKUP_DIR="$DIR/backup-$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$BACKUP_DIR"
    local file
    for file in compose.yaml deploy.env frps.toml; do
        [ -f "$DIR/$file" ] && cp -a "$DIR/$file" "$BACKUP_DIR/$file"
    done
    [ -d "$DIR/secrets" ] && cp -a "$DIR/secrets" "$BACKUP_DIR/secrets"
    info "已备份到 $BACKUP_DIR"
}

check_image_version() {
    local reported
    reported=$(docker run --rm --entrypoint /usr/local/bin/frp-agent \
        "${AGENT_IMAGE}:${AGENT_IMAGE_TAG}" version 2>/dev/null || true)
    case "$reported" in
        ""|0.0.0-local|dev)
            warn "镜像 ${AGENT_IMAGE}:${AGENT_IMAGE_TAG} 的 version=${reported:-未知}；"
            warn "说明构建时没有注入 VERSION，管理台会继续显示 0.0.0-local。"
            ;;
    esac
}

wait_for_agent() {
    local deadline=$((SECONDS + 120)) started logs status
    while [ "$SECONDS" -lt "$deadline" ]; do
        status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' mitahill-mctools-agent-firewall 2>/dev/null || true)
        if [ "$status" = "healthy" ] &&
            docker inspect --format '{{.State.Status}}' mitahill-mctools-agent-net 2>/dev/null | grep -qx running &&
            docker inspect --format '{{.State.Status}}' mitahill-mctools-agent 2>/dev/null | grep -qx running &&
            docker inspect --format '{{.State.Status}}' mitahill-mctools-agent-frps 2>/dev/null | grep -qx running; then
            started=$(docker inspect --format '{{.State.StartedAt}}' mitahill-mctools-agent 2>/dev/null || true)
            logs=$(docker logs --since "$started" mitahill-mctools-agent 2>&1 || true)
            case "$logs" in
                *"connected to control server"*)
                    return 0
                    ;;
            esac
        fi
        sleep 2
    done
    printf '错误: 等待 Agent 连接控制端超时\n' >&2
    compose_cmd ps >&2 || true
    compose_cmd logs --tail=80 --no-color agent frps firewall >&2 || true
    return 1
}

print_summary() {
    local idx
    printf '\n部署完成\n'
    printf '  节点名称: %s\n' "$NODE_NAME"
    printf '  Compose 项目: %s\n' "$PROJECT"
    printf '  部署目录: %s\n' "$DIR"
    printf '  镜像: %s:%s\n' "$AGENT_IMAGE" "$AGENT_IMAGE_TAG"
    printf '  控制端: %s\n' "$CONTROL_URL"
    printf '  端口: %s（%d 个）\n' "$PORT_SPEC" "$PORT_COUNT"
    printf '  frpc 连接端口: %s\n' "$FRP_PUBLIC_CONTROL_PORT"
    printf '\n管理台下一步：\n'
    printf '  1. 节点会自动出现在 /admin/nodes，默认未发布。\n'
    printf '  2. 确认公网地址（自动填的是连接来源 IP；经 nginx 转发时需改成节点公网 IP/域名）。\n'
    printf '  3. 允许端口默认等于部署范围 %s，可按需缩小。\n' "$PORT_SPEC"
    printf '  4. 选择发布通道后客户端才能使用。\n'
    printf '\n安全组/防火墙需放行 TCP %s' "$FRP_PUBLIC_CONTROL_PORT"
    for ((idx = 0; idx < ${#RANGE_STARTS[@]}; idx++)); do
        if [ "${RANGE_STARTS[idx]}" -eq "${RANGE_ENDS[idx]}" ]; then
            printf ', %s' "${RANGE_STARTS[idx]}"
        else
            printf ', %s-%s' "${RANGE_STARTS[idx]}" "${RANGE_ENDS[idx]}"
        fi
    done
    printf '\n'
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        printf '  例如：ufw allow %s/tcp\n' "$FRP_PUBLIC_CONTROL_PORT"
        for ((idx = 0; idx < ${#RANGE_STARTS[@]}; idx++)); do
            if [ "${RANGE_STARTS[idx]}" -eq "${RANGE_ENDS[idx]}" ]; then
                printf '        ufw allow %s/tcp\n' "${RANGE_STARTS[idx]}"
            else
                printf '        ufw allow %s:%s/tcp\n' "${RANGE_STARTS[idx]}" "${RANGE_ENDS[idx]}"
            fi
        done
    fi
    printf '\n常用命令：cd %s && docker compose --env-file deploy.env -f compose.yaml ps|logs|down\n' "$DIR"
}

start_stack() {
    [ -f "$DIR/compose.yaml" ] || die "缺少 $DIR/compose.yaml"
    [ -f "$DIR/deploy.env" ] || die "缺少 $DIR/deploy.env"
    [ -f "$DIR/frps.toml" ] || die "缺少 $DIR/frps.toml"
    [ -d "$DIR/secrets" ] || die "缺少 $DIR/secrets"
    compose_cmd config -q
    if [ "$START_ONLY" != yes ]; then
        compose_cmd pull
    fi
    check_image_version
    if [ "$START_ONLY" = yes ]; then
        compose_cmd up -d
    elif [ "$EXISTING" = yes ] || [ "$FORCE" = yes ]; then
        compose_cmd up -d --force-recreate
    else
        compose_cmd up -d
    fi
    wait_for_agent
}

start_existing() {
    START_ONLY=yes
    load_existing
    PROJECT=${EXISTING_PROJECT:-minecrafttools-$(sanitize_name "${EXISTING_NAME:-agent}")}
    NODE_NAME=${EXISTING_NAME:-$PROJECT}
    PORT_SPEC=${EXISTING_PORTS:-$DEFAULT_PORTS}
    FRP_PUBLIC_CONTROL_PORT=${EXISTING_PUBLIC_PORT:-7000}
    NODE_BIND_ADDRESS=${EXISTING_BIND:-0.0.0.0}
    AGENT_IMAGE_TAG=${EXISTING_TAG:-}
    CONTROL_URL=${EXISTING_CONTROL_URL:-$CONTROL_URL}
    if ! parse_ports "$PORT_SPEC"; then
        die "deploy.env 中的端口范围无效: $PORT_SPEC"
    fi
    if [ "$DRY_RUN" = yes ]; then
        info "dry-run：将启动 $DIR 中的现有配置"
        return 0
    fi
    info "复用 $DIR 中的现有配置"
    start_stack
    print_summary
}

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --dir) DIR_ARG=${2:?缺少 --dir 参数}; shift 2 ;;
            --ports) PORTS_ARG=${2:?缺少 --ports 参数}; shift 2 ;;
            --name) NAME_ARG=${2:?缺少 --name 参数}; shift 2 ;;
            --tag) TAG_ARG=${2:?缺少 --tag 参数}; shift 2 ;;
            --project) PROJECT_ARG=${2:?缺少 --project 参数}; shift 2 ;;
            --public-port) PUBLIC_PORT_ARG=${2:?缺少 --public-port 参数}; PUBLIC_PORT_SET=yes; CHANGES=yes; shift 2 ;;
            --bind) BIND_ARG=${2:?缺少 --bind 参数}; BIND_SET=yes; CHANGES=yes; shift 2 ;;
            --public-host) PUBLIC_HOST_ARG=${2:?缺少 --public-host 参数}; PUBLIC_HOST_SET=yes; CHANGES=yes; shift 2 ;;
            --ca-file) CA_FILE_ARG=${2:?缺少 --ca-file 参数}; CA_SET=yes; CHANGES=yes; shift 2 ;;
            --control-url) CONTROL_URL_ARG=${2:?缺少 --control-url 参数}; CHANGES=yes; shift 2 ;;
            --start) START_ONLY=yes; shift ;;
            --force) FORCE=yes; CHANGES=yes; shift ;;
            --dry-run) DRY_RUN=yes; shift ;;
            --yes) ASSUME_YES=yes; shift ;;
            --self-test) SELF_TEST=yes; shift ;;
            -h|--help) SHOW_HELP=yes; shift ;;
            *) die "未知参数: $1（使用 --help 查看用法）" ;;
        esac
    done
    [ -n "$ENV_NODE_NAME" ] && { NAME_ARG=$ENV_NODE_NAME; CHANGES=yes; }
    [ -n "$ENV_PORTS" ] && { PORTS_ARG=$ENV_PORTS; CHANGES=yes; }
    [ -n "$ENV_COMPOSE_DIR" ] && { DIR_ARG=$ENV_COMPOSE_DIR; CHANGES=yes; }
    [ -n "$ENV_TAG" ] && { TAG_ARG=$ENV_TAG; CHANGES=yes; }
    [ -n "$ENV_PROJECT" ] && { PROJECT_ARG=$ENV_PROJECT; CHANGES=yes; }
    if [ -n "$ENV_PUBLIC_PORT" ]; then
        PUBLIC_PORT_ARG=$ENV_PUBLIC_PORT
        PUBLIC_PORT_SET=yes
        CHANGES=yes
    fi
    if [ -n "$ENV_BIND" ]; then
        BIND_ARG=$ENV_BIND
        BIND_SET=yes
        CHANGES=yes
    fi
    if [ -n "$ENV_PUBLIC_HOST" ]; then
        PUBLIC_HOST_ARG=$ENV_PUBLIC_HOST
        PUBLIC_HOST_SET=yes
        CHANGES=yes
    fi
    if [ -n "$ENV_CA" ]; then
        CA_FILE_ARG=$ENV_CA
        CA_SET=yes
        CHANGES=yes
    fi
}

main() {
    parse_args "$@"
    if [ "$SHOW_HELP" = yes ]; then
        usage
        exit 0
    fi
    if [ "$SELF_TEST" = yes ]; then
        self_test
        exit $?
    fi

    detect_os
    [ "$DRY_RUN" = yes ] || require_root
    detect_docker

    if [ -n "$CONTROL_URL_ARG" ]; then
        CONTROL_URL=$CONTROL_URL_ARG
    fi
    case "$CONTROL_URL" in
        https://*|tls://*) ;;
        *) die "CONTROL_URL 必须是 https:// 或 tls:// 地址" ;;
    esac
    case "$CONTROL_URL" in
        *[[:space:]]*) die "CONTROL_URL 不能包含空白" ;;
        *[\"\'\`\$\\]*) die "CONTROL_URL 包含不支持的字符" ;;
    esac

    if [ "$START_ONLY" = yes ]; then
        DIR=${DIR_ARG:-$DEFAULT_DIR}
        DIR=$(validate_dir "$DIR") || die "无效的 Compose 目录"
        [ -f "$DIR/deploy.env" ] || die "$DIR 中没有 deploy.env"
        EXISTING=yes
        start_existing
        exit 0
    fi

    if [ -n "$DIR_ARG" ]; then
        DIR=$(validate_dir "$DIR_ARG") || die "无效的 Compose 目录: $DIR_ARG"
    elif [ -f "$DEFAULT_DIR/deploy.env" ] && [ "$FORCE" = no ]; then
        DIR=$DEFAULT_DIR
        info "检测到现有部署，复用 $DIR"
    fi

    if [ -n "$DIR" ] && [ -e "$DIR" ] && [ ! -d "$DIR" ]; then
        die "$DIR 不是目录"
    fi

    if [ -n "$DIR" ] && [ -f "$DIR/deploy.env" ]; then
        EXISTING=yes
        load_existing
        if [ "$FORCE" = no ] && ! any_changes; then
            start_existing
            exit 0
        fi
    elif [ -n "$DIR" ] && [ -d "$DIR" ] && [ "$FORCE" = no ]; then
        if [ -n "$(ls -A "$DIR" 2>/dev/null | head -1)" ]; then
            die "$DIR 不为空且不是本脚本生成的部署目录；使用 --force 覆盖"
        fi
    fi

    NODE_NAME=${NAME_ARG:-${EXISTING_NAME:-}}
    if [ -z "$NODE_NAME" ]; then
        NODE_NAME=$(ask "节点名称（用于管理台显示）" "")
    fi
    validate_name "$NODE_NAME" || die "节点名称无效（1-80 字符，不能包含换行）"

    if [ -n "$PUBLIC_PORT_ARG" ]; then
        FRP_PUBLIC_CONTROL_PORT=$PUBLIC_PORT_ARG
    elif [ -n "${EXISTING_PUBLIC_PORT:-}" ]; then
        FRP_PUBLIC_CONTROL_PORT=$EXISTING_PUBLIC_PORT
    fi
    [[ $FRP_PUBLIC_CONTROL_PORT =~ ^[0-9]+$ ]] && [ "$FRP_PUBLIC_CONTROL_PORT" -ge 1 ] &&
        [ "$FRP_PUBLIC_CONTROL_PORT" -le 65535 ] || die "frpc 连接端口无效"
    FRP_PUBLIC_CONTROL_PORT=$((10#$FRP_PUBLIC_CONTROL_PORT))

    if [ -z "${PORTS_ARG:-}" ]; then
        if [ -n "${EXISTING_PORTS:-}" ]; then
            PORTS_ARG=$EXISTING_PORTS
        else
            PORTS_ARG=$(ask "端口范围（例如 10000-10002,10004-10005，最多 16 个端口）" "$DEFAULT_PORTS")
        fi
    fi
    if ! parse_ports "$PORTS_ARG"; then
        die "端口范围无效：需在 1024-65535，总数不超过 16，且不能重叠或包含 7000/7400/9000"
    fi

    if [ -z "$DIR" ]; then
        DIR=$(ask "Compose 目录" "$DEFAULT_DIR")
        DIR=$(validate_dir "$DIR") || die "无效的 Compose 目录"
    fi

    if [ -z "${TAG_ARG:-}" ]; then
        if [ -n "${EXISTING_TAG:-}" ]; then
            TAG_ARG=$EXISTING_TAG
        else
            TAG_ARG=$(latest_stable_tag)
            info "自动使用最新稳定版镜像标签: ${TAG_ARG}"
        fi
    fi
    AGENT_IMAGE_TAG=$TAG_ARG
    [[ $AGENT_IMAGE_TAG =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "镜像标签格式无效"

    PROJECT=${PROJECT_ARG:-${EXISTING_PROJECT:-minecrafttools-$(sanitize_name "$NODE_NAME")}}
    [[ $PROJECT =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "Compose 项目名无效: $PROJECT"

    for ((i = 0; i < ${#RANGE_STARTS[@]}; i++)); do
        if [ "$FRP_PUBLIC_CONTROL_PORT" -ge "${RANGE_STARTS[i]}" ] &&
            [ "$FRP_PUBLIC_CONTROL_PORT" -le "${RANGE_ENDS[i]}" ]; then
            die "frpc 连接端口不能落在游戏端口范围内"
        fi
    done

    NODE_BIND_ADDRESS=${BIND_ARG:-${EXISTING_BIND:-0.0.0.0}}
    setup_bind "$NODE_BIND_ADDRESS"

    NODE_PUBLIC_HOST=${PUBLIC_HOST_ARG:-${EXISTING_PUBLIC_HOST:-}}
    case "$NODE_PUBLIC_HOST" in
        *[[:space:]]*|*'#'*|*'='*|*'/'*|*'?'*|*'@'*|*'$'*|*'"'*|*"'"*)
            die "公网主机格式无效"
            ;;
    esac

    check_ephemeral
    if [ "$EXISTING" = no ]; then
        check_listening_ports
        check_existing_containers
    fi

    info ""
    info "即将部署："
    info "  节点名称: $NODE_NAME"
    info "  Compose 项目: $PROJECT"
    info "  目录: $DIR"
    info "  镜像: $AGENT_IMAGE:$AGENT_IMAGE_TAG"
    info "  端口: ${PORT_SPEC}（${PORT_COUNT} 个）"
    info "  frpc 连接端口: $FRP_PUBLIC_CONTROL_PORT"
    info "  控制端: $CONTROL_URL"
    info ""

    if [ "$DRY_RUN" = yes ]; then
        info "dry-run：未写入文件"
        exit 0
    fi

    [ "$EXISTING" = yes ] && backup_files
    mkdir -p "$DIR"
    umask 077
    prepare_secrets
    write_compose
    write_env
    write_frps

    chmod 0755 "$DIR"
    chmod 0750 "$DIR/secrets"
    chmod 0644 "$DIR/compose.yaml"
    chmod 0600 "$DIR/deploy.env"
    chmod 0640 "$DIR/frps.toml" "$DIR/secrets"/*
    chown 0:10001 "$DIR/secrets" "$DIR/secrets"/* "$DIR/frps.toml"

    start_stack
    print_summary
}

main "$@"
