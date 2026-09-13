#!/bin/bash
export PYTHONPATH=/app

WARP_LICENSE_KEY="${WARP_LICENSE_KEY:-}"
WARP_GENERATOR="/usr/local/bin/warp-register"
WARPCTL="/app/scripts/warp_userspace_ctl.sh"

# Two independent WARP registrations/tunnels. The application-facing endpoint
# remains 127.0.0.1:1080; a tiny TCP relay below forwards it to whichever
# healthy backend is active. This is availability failover, not IP rotation.
PRIMARY_CONFIG="${WARP_PRIMARY_CONFIG_FILE:-/data/warp-primary.conf}"
SECONDARY_CONFIG="${WARP_SECONDARY_CONFIG_FILE:-/data/warp-secondary.conf}"
PRIMARY_SOCKS="127.0.0.1:1081"
SECONDARY_SOCKS="127.0.0.1:1082"
FRONT_SOCKS="127.0.0.1:1080"
ACTIVE_BACKEND_FILE="/tmp/easyproxy-warp-active"
RELAY_PID_FILE="/tmp/easyproxy-warp-relay.pid"

register_warp_config() {
    name="$1"
    config_file="$2"
    mkdir -p "$(dirname "$config_file")"
    if [ -s "$config_file" ]; then
        echo "Reusing saved WARP ${name} config: ${config_file}."
        return 0
    fi

    echo "No saved WARP ${name} config; registering and saving to ${config_file}."
    temp_config="${config_file}.tmp.$$"
    generator_args=()
    if [ -n "$WARP_LICENSE_KEY" ]; then
        generator_args+=(--license "$WARP_LICENSE_KEY")
    fi
    if ! WARP_DNS="1.1.1.1, 1.0.0.1" \
         WARP_MTU="1280" \
         WARP_ALLOWED_IPS="0.0.0.0/0" \
         WARP_PERSISTENT_KEEPALIVE="25" \
         WARP_DEVICE_TYPE="Linux" \
         WARP_LOCALE="en_US" \
         "$WARP_GENERATOR" "${generator_args[@]}" > "$temp_config"; then
        rm -f "$temp_config"
        echo "WARP ${name} registration failed." >&2
        return 1
    fi
    chmod 600 "$temp_config"
    mv -f "$temp_config" "$config_file"
    echo "Saved WARP ${name} config in ${config_file}."
}

warp_ctl() {
    name="$1"
    config="$2"
    socks="$3"
    action="$4"
    WARP_INSTANCE="$name" \
    WARP_CONFIG_FILE="$config" \
    WARP_SOCKS_ADDR="$socks" \
    WARP_RUNTIME_DIR="/tmp/easyproxy-warp-${name}" \
    "$WARPCTL" "$action"
}

start_backend() {
    name="$1"; config="$2"; socks="$3"
    register_warp_config "$name" "$config" || return 1
    warp_ctl "$name" "$config" "$socks" start || return 1
    for _ in $(seq 1 20); do
        if warp_ctl "$name" "$config" "$socks" probe >/dev/null 2>&1; then
            echo "WARP ${name} ready on ${socks}."
            return 0
        fi
        sleep 1
    done
    echo "WARP ${name} failed health check." >&2
    return 1
}

backend_healthy() {
    case "$1" in
        primary) warp_ctl primary "$PRIMARY_CONFIG" "$PRIMARY_SOCKS" probe >/dev/null 2>&1 ;;
        secondary) warp_ctl secondary "$SECONDARY_CONFIG" "$SECONDARY_SOCKS" probe >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

choose_backend() {
    current="$(cat "$ACTIVE_BACKEND_FILE" 2>/dev/null || true)"
    if [ -n "$current" ] && backend_healthy "$current"; then
        printf '%s\n' "$current"
        return 0
    fi
    if backend_healthy primary; then
        printf '%s\n' primary
        return 0
    fi
    if backend_healthy secondary; then
        printf '%s\n' secondary
        return 0
    fi
    return 1
}

start_relay() {
    # Python is already part of the image. Keep a stable SOCKS endpoint for the
    # existing EasyProxy code while selecting a healthy backend per connection.
    python - <<'PY' &
import asyncio, os

ACTIVE = "/tmp/easyproxy-warp-active"
BACKENDS = {"primary": ("127.0.0.1", 1081), "secondary": ("127.0.0.1", 1082)}

async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def handle(client_r, client_w):
    try:
        try:
            name = open(ACTIVE, "r", encoding="utf-8").read().strip()
        except OSError:
            name = "primary"
        host, port = BACKENDS.get(name, BACKENDS["primary"])
        upstream_r, upstream_w = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
        await asyncio.gather(pipe(client_r, upstream_w), pipe(upstream_r, client_w))
    except Exception:
        try:
            client_w.close()
            await client_w.wait_closed()
        except Exception:
            pass

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 1080)
    async with server:
        await server.serve_forever()

asyncio.run(main())
PY
    relay_pid=$!
    printf '%s\n' "$relay_pid" > "$RELAY_PID_FILE"
    for _ in $(seq 1 10); do
        nc -z 127.0.0.1 1080 && { echo "Dual-WARP relay ready on ${FRONT_SOCKS}."; return 0; }
        sleep 1
    done
    echo "Dual-WARP relay failed to start." >&2
    return 1
}

watch_failover() {
    while true; do
        current="$(cat "$ACTIVE_BACKEND_FILE" 2>/dev/null || echo primary)"
        if ! backend_healthy "$current"; then
            if [ "$current" = primary ] && backend_healthy secondary; then
                echo secondary > "$ACTIVE_BACKEND_FILE"
                echo "WARP failover: primary unhealthy, switched to secondary."
            elif [ "$current" = secondary ] && backend_healthy primary; then
                echo primary > "$ACTIVE_BACKEND_FILE"
                echo "WARP failover: secondary unhealthy, switched to primary."
            else
                echo "WARP health: no healthy backend available." >&2
            fi
        elif [ "$current" = secondary ] && backend_healthy primary; then
            echo primary > "$ACTIVE_BACKEND_FILE"
            echo "WARP failback: primary recovered."
        fi
        sleep 30
    done
}

cleanup() {
    [ -s "$RELAY_PID_FILE" ] && kill "$(cat "$RELAY_PID_FILE")" >/dev/null 2>&1 || true
    warp_ctl primary "$PRIMARY_CONFIG" "$PRIMARY_SOCKS" stop >/dev/null 2>&1 || true
    warp_ctl secondary "$SECONDARY_CONFIG" "$SECONDARY_SOCKS" stop >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting dual Cloudflare WARP userspace backends..."
if ! command -v "$WARP_GENERATOR" >/dev/null 2>&1 || ! command -v wireproxy >/dev/null 2>&1; then
    echo "WARP generator or wireproxy not found. EasyProxy will continue without WARP." >&2
else
    primary_ok=0; secondary_ok=0
    start_backend primary "$PRIMARY_CONFIG" "$PRIMARY_SOCKS" && primary_ok=1 || true
    start_backend secondary "$SECONDARY_CONFIG" "$SECONDARY_SOCKS" && secondary_ok=1 || true

    if [ "$primary_ok" -eq 1 ]; then
        echo primary > "$ACTIVE_BACKEND_FILE"
    elif [ "$secondary_ok" -eq 1 ]; then
        echo secondary > "$ACTIVE_BACKEND_FILE"
    fi

    if [ "$primary_ok" -eq 1 ] || [ "$secondary_ok" -eq 1 ]; then
        start_relay || true
        watch_failover &
    else
        echo "Both WARP backends unavailable; EasyProxy will continue without WARP." >&2
    fi
fi

echo "Starting EasyProxy..."
cd /app || exit 1
python app.py
