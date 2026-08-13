#!/bin/bash

set -e

SCRIPT_DIR=$(dirname "$(realpath "$0")")

TOOL="$1"

case "$TOOL" in
    scaphandre|joularjx|otajae|tools)
        ;;
    *)
        echo "[✗] Invalid MEASUREMENT_TOOL: $TOOL"
        exit 1
        ;;
esac

VM_NAME="vm1"

if [ "$TOOL" = "otajae" ] || [ "$TOOL" = "tools" ]; then
    MOUNT_PATH=""
else
    MOUNT_PATH="/var/lib/libvirt/${TOOL}/${VM_NAME}"
fi

VM_SSH_PORT=2222

echo "[~] Cleanup..."

sudo pkill -9 -f qemu-system-x86_64 2>/dev/null || true

sleep 2

for port in 2222 8181 8080 8111; do
    pid=$(sudo lsof -ti :$port 2>/dev/null || true)

    if [ -n "$pid" ]; then
        sudo kill -9 $pid 2>/dev/null || true
    fi
done

sleep 2

echo "[~] Waiting for QEMU processes..."

while pgrep -f qemu-system-x86_64 >/dev/null; do
    sleep 1
done

#
# Nur für Tools mit virtfs-Mount
#
if [ "$TOOL" != "otajae" ] && [ "$TOOL" != "tools" ]; then

    echo "[~] Cleanup mount..."

    sudo umount "$MOUNT_PATH" 2>/dev/null || true

    echo "[~] Prepare mount..."

    sudo mkdir -p "$MOUNT_PATH"
    sudo mount -t tmpfs tmpfs "$MOUNT_PATH" -o size=5m

fi

echo "[~] Start VM..."

sudo bash "${SCRIPT_DIR}/start_vm1.sh" "$TOOL"

echo "[~] Waiting for VM SSH..."

MAX_ATTEMPTS=120
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do

    if echo "" | nc -w 5 localhost $VM_SSH_PORT 2>/dev/null | grep -q "SSH"; then
        break
    fi

    ATTEMPT=$((ATTEMPT + 1))
    sleep 1

done

echo "[~] VM SSH port open"

case "$TOOL" in

    scaphandre)
        echo "[✓] Scaphandre mode"
        ;;

    joularjx)
        echo "[✓] JoularJX mode"
        ;;

    otajae)
        echo "[✓] OTJAE mode"
        ;;

    tools)
        echo "[✓] Tools mode"
        ;;

esac

echo "[✓] Host ready"