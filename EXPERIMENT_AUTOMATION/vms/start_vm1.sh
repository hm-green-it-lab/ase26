#!/bin/bash

DISK="/home/user/ubuntu_disk.img"

TOOL="$1"

case "$TOOL" in
    scaphandre|joularjx|otajae|tools)
        ;;
    *)
        echo "Invalid MEASUREMENT_TOOL: $TOOL"
        exit 1
        ;;
esac

PASST_SOCKET="/tmp/passt-vm1.socket"
PASST_PID_FILE="/tmp/passt-vm1.pid"

if [ "$TOOL" = "otajae" ] || [ "$TOOL" = "tools" ]; then
    MOUNT_PATH=""
    MOUNT_TAG=""
else
    MOUNT_PATH="/var/lib/libvirt/${TOOL}/vm1"
    MOUNT_TAG="${TOOL}"
fi

# Kill existing passt instance if running
if [ -f "$PASST_PID_FILE" ]; then
    OLD_PID=$(cat "$PASST_PID_FILE")

    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing passt instance (PID $OLD_PID)..."
        kill "$OLD_PID"
        sleep 1
    fi

    rm -f "$PASST_PID_FILE"
fi

# Remove stale socket if present
rm -f "$PASST_SOCKET"

# Start passt
echo "Starting passt..."

passt \
    --socket "$PASST_SOCKET" \
    -t 2222:22 \
    -t 8080:8080 \
    -t 8181:8081 \
    -t 8111:8111 \
    --pid "$PASST_PID_FILE" \
    --foreground &

# Wait until socket is ready
for i in $(seq 1 10); do
    [ -S "$PASST_SOCKET" ] && break
    echo "Waiting for passt socket... ($i/10)"
    sleep 0.5
done

if [ ! -S "$PASST_SOCKET" ]; then
    echo "ERROR: passt socket not available after timeout. Aborting."
    exit 1
fi

echo "passt started (PID $(cat "$PASST_PID_FILE"))"

if [ "$TOOL" = "otajae" ] || [ "$TOOL" = "tools" ]; then

    qemu-system-x86_64 \
      -name guest=vm1 \
      -daemonize \
      -netdev stream,id=net0,server=off,addr.type=unix,addr.path="$PASST_SOCKET" \
      -device virtio-net-pci,netdev=net0,vectors=10,mq=on,rx_queue_size=1024,tx_queue_size=256 \
      -cpu host \
      -enable-kvm \
      -drive file="$DISK",format=raw,cache=none,aio=native,discard=unmap,if=none,id=maindrive \
      -device virtio-blk-pci,drive=maindrive \
      -m 384G \
      -smp 80,sockets=2,cores=20,threads=2 \
      -object memory-backend-ram,id=m0,size=192G,host-nodes=0,policy=bind,merge=off \
      -object memory-backend-ram,id=m1,size=192G,host-nodes=1,policy=bind,merge=off \
      -numa node,cpus=0-39,nodeid=0,memdev=m0 \
      -numa node,cpus=40-79,nodeid=1,memdev=m1 \
      -display none \
      -global kvm-pit.lost_tick_policy=discard

else

    qemu-system-x86_64 \
      -name guest=vm1 \
      -daemonize \
      -netdev stream,id=net0,server=off,addr.type=unix,addr.path="$PASST_SOCKET" \
      -device virtio-net-pci,netdev=net0,vectors=10,mq=on,rx_queue_size=1024,tx_queue_size=256 \
      -cpu host \
      -enable-kvm \
      -drive file="$DISK",format=raw,cache=none,aio=native,discard=unmap,if=none,id=maindrive \
      -device virtio-blk-pci,drive=maindrive \
      -m 384G \
      -smp 80,sockets=2,cores=20,threads=2 \
      -object memory-backend-ram,id=m0,size=192G,host-nodes=0,policy=bind,merge=off \
      -object memory-backend-ram,id=m1,size=192G,host-nodes=1,policy=bind,merge=off \
      -numa node,cpus=0-39,nodeid=0,memdev=m0 \
      -numa node,cpus=40-79,nodeid=1,memdev=m1 \
      -display none \
      -global kvm-pit.lost_tick_policy=discard \
      -virtfs local,path="$MOUNT_PATH",mount_tag="$MOUNT_TAG",security_model=none

fi