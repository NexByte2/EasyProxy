#!/bin/sh
set -eu

INSTANCE="${WARP_INSTANCE:-primary}"
BASE_DIR="${WARP_RUNTIME_DIR:-/tmp/easyproxy-warp-${INSTANCE}}"
PID_FILE="${WARP_PID_FILE:-${BASE_DIR}/wireproxy.pid}"
CONFIG_FILE="${WARP_CONFIG_FILE:-/data/warp-primary.conf}"
WIREPROXY_CONFIG="${WARP_WIREPROXY_CONFIG:-${BASE_DIR}/wireproxy.conf}"
LOG_FILE="${WARP_LOG_FILE:-/var/log/wireproxy-${INSTANCE}.log}"
WIREPROXY_BIN="/usr/local/bin/wireproxy"
SOCKS_ADDR="${WARP_SOCKS_ADDR:-127.0.0.1:1081}"
TRACE_URL="https://www.cloudflare.com/cdn-cgi/trace"

pid_is_wireproxy() {
    pid="$1"
    [ -r "/proc/${pid}/comm" ] || return 1
    [ "$(tr -d '\n' < "/proc/${pid}/comm")" = "wireproxy" ]
}

read_pid() {
    [ -s "$PID_FILE" ] || return 1
    pid=$(tr -dc '0-9' < "$PID_FILE")
    [ -n "$pid" ] || return 1
    printf '%s\n' "$pid"
}

write_wireproxy_config() {
    mkdir -p "$BASE_DIR"

    # Keep WARP itself IPv4-only. Remove any IPv6 fields even when a manually
    # supplied/generated profile contains them.
    sed -E '/^(Address|AllowedIPs|DNS) = / {
        s/, *[^, ]*:[^, ]*//g
    }' "$CONFIG_FILE" > "$WIREPROXY_CONFIG"

    # Resolve the WireGuard endpoint to an IPv4 address as well. This keeps
    # the control-plane handshake from selecting an IPv6 endpoint implicitly.
    endpoint=$(sed -n 's/^Endpoint = //p' "$WIREPROXY_CONFIG" | head -n 1)
    endpoint_host=${endpoint%:*}
    endpoint_port=${endpoint##*:}
    endpoint_ipv4=$(getent ahostsv4 "$endpoint_host" 2>/dev/null | awk 'NR == 1 { print $1 }')
    if [ -n "$endpoint_ipv4" ] && [ -n "$endpoint_port" ]; then
        sed -i "s/^Endpoint = .*/Endpoint = ${endpoint_ipv4}:${endpoint_port}/" "$WIREPROXY_CONFIG"
    fi

    printf '\n[Socks5]\nBindAddress = %s\n' "$SOCKS_ADDR" >> "$WIREPROXY_CONFIG"
    chmod 600 "$WIREPROXY_CONFIG"
}

start_wireproxy() {
    if pid=$(read_pid) && pid_is_wireproxy "$pid"; then
        echo "wireproxy ${INSTANCE} already running (pid ${pid}, socks ${SOCKS_ADDR})."
        return 0
    fi

    rm -f "$PID_FILE"
    [ -x "$WIREPROXY_BIN" ] || { echo "wireproxy binary not found." >&2; return 1; }
    [ -f "$CONFIG_FILE" ] || { echo "WireGuard config not found: ${CONFIG_FILE}." >&2; return 1; }
    mkdir -p "$BASE_DIR"
    write_wireproxy_config

    if ! "$WIREPROXY_BIN" -n -c "$WIREPROXY_CONFIG" >/dev/null 2>&1; then
        echo "wireproxy ${INSTANCE} config validation failed." >&2
        rm -f "$WIREPROXY_CONFIG"
        return 1
    fi

    "$WIREPROXY_BIN" -c "$WIREPROXY_CONFIG" >>"$LOG_FILE" 2>&1 &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"
    echo "Started wireproxy ${INSTANCE} (pid ${pid}, socks ${SOCKS_ADDR})."
}

stop_wireproxy() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        rm -f "$PID_FILE"
        rm -f "$WIREPROXY_CONFIG"
        return 0
    fi

    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && pid_is_wireproxy "$pid"; do
        sleep 1
        i=$((i + 1))
    done

    if pid_is_wireproxy "$pid"; then
        kill -KILL "$pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$PID_FILE"
    rm -f "$WIREPROXY_CONFIG"
}

probe_warp() {
    pid=$(read_pid 2>/dev/null || true)
    if [ -z "$pid" ] || ! pid_is_wireproxy "$pid"; then
        echo "WARP ${INSTANCE} probe: wireproxy process is down." >&2
        return 1
    fi

    trace=$(curl --socks5 "$SOCKS_ADDR" -fsS \
        --connect-timeout 3 --max-time 8 "$TRACE_URL" 2>&1) || {
        echo "WARP ${INSTANCE} probe: SOCKS traffic failed: $trace" >&2
        return 1
    }

    printf '%s\n' "$trace" | grep -Eq '^warp=(on|plus)$' || {
        echo "WARP ${INSTANCE} probe: Cloudflare did not report warp=on/plus." >&2
        return 1
    }
    printf '%s\n' "$trace" | grep -Eq '^ip=[0-9.]+$' || {
        echo "WARP ${INSTANCE} probe: egress is not IPv4-only." >&2
        return 1
    }
    printf '%s\n' "$trace"
}

case "${1:-status}" in
    start)
        start_wireproxy
        ;;
    stop)
        stop_wireproxy
        ;;
    restart)
        stop_wireproxy
        start_wireproxy
        ;;
    probe)
        probe_warp
        ;;
    status)
        pid=$(read_pid 2>/dev/null || true)
        if [ -n "$pid" ] && pid_is_wireproxy "$pid"; then
            echo "wireproxy ${INSTANCE} running (pid ${pid}, socks ${SOCKS_ADDR})."
            exit 0
        fi
        exit 1
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|probe|status}" >&2
        exit 2
        ;;
esac
