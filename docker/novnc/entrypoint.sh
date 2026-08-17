#!/bin/sh
set -eu

: "${SPU_NOVNC_PASSWORD:?SPU_NOVNC_PASSWORD deve ser definida}"
[ "${#SPU_NOVNC_PASSWORD}" -eq 8 ] || {
    echo "SPU_NOVNC_PASSWORD deve ter exatamente 8 caracteres." >&2
    exit 1
}

display="${DISPLAY:-:99}"
screen="${SPU_NOVNC_SCREEN:-1600x900x24}"
display_number="${display#:}"
x11_socket="/tmp/.X11-unix/X${display_number}"
x11_lock="/tmp/.X${display_number}-lock"
runtime_dir=/run/spu-novnc
passwd_file="$runtime_dir/.vnc/passwd"
websockify_pid=""
x11vnc_pid=""
openbox_pid=""
xvfb_pid=""

mkdir -p "$runtime_dir/.vnc" /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

# O socket fica em um volume compartilhado com o scheduler. Depois que o
# sidecar é recriado, o volume pode conservar o socket do Xvfb anterior; nesse
# caso um novo Xvfb aborta com "Server is already active" e o noVNC fica de pé
# sem servidor VNC. O sidecar é o único dono do display, portanto limpa os
# artefatos antigos antes de iniciá-lo.
rm -f "$x11_socket" "$x11_lock"

HOME="$runtime_dir" x11vnc -storepasswd \
    "$SPU_NOVNC_PASSWORD" "$passwd_file" >/dev/null
unset SPU_NOVNC_PASSWORD

Xvfb "$display" -screen 0 "$screen" -ac -nolisten tcp &
xvfb_pid=$!

cleanup() {
    for pid in "$websockify_pid" "$x11vnc_pid" "$openbox_pid" "$xvfb_pid"; do
        [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

attempt=0
while [ ! -S "$x11_socket" ]; do
    attempt=$((attempt + 1))
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        wait "$xvfb_pid" || true
        echo "Xvfb encerrou antes de disponibilizar o display $display" >&2
        exit 1
    fi
    if [ "$attempt" -ge 50 ]; then
        echo "Xvfb nao criou o socket do display $display" >&2
        exit 1
    fi
    sleep 0.1
done

DISPLAY="$display" openbox >/tmp/openbox.log 2>&1 &
openbox_pid=$!

x11vnc \
    -display "$display" \
    -rfbauth "$passwd_file" \
    -rfbport 5900 \
    -localhost \
    -forever \
    -shared \
    -noxdamage \
    >/tmp/x11vnc.log 2>&1 &
x11vnc_pid=$!

websockify \
    --web /usr/share/novnc \
    0.0.0.0:6080 \
    localhost:5900 \
    >/tmp/websockify.log 2>&1 &
websockify_pid=$!

wait "$websockify_pid"
