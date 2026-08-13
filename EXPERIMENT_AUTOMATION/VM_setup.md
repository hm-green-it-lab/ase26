Based on: https://linuxvox.com/blog/ubuntu-qemu/

# VM Setup

sudo apt install qemu-kvm virt-manager libvirt-daemon-system libvirt-clients bridge-utils

qemu-img create -f raw ubuntu_disk.img 100G

sudo usermod -aG kvm user
newgrp kvm

# mount ISO to inspect files (run as root or with sudo)
mkdir /tmp/iso && sudo mount -o loop ubuntu-24.04.3-live-server-amd64.iso /tmp/iso

# find kernel/initrd (example paths)
ls /tmp/iso/casper/vmlinuz     # kernel
ls /tmp/iso/casper/initrd      # initrd (may be initrd or initrd.lz)

# Start VM with kernel/initrd and disk image (adjust memory/smp as needed)

qemu-system-x86_64 -enable-kvm -kernel /tmp/iso/casper/vmlinuz -initrd /tmp/iso/casper/initrd -append "boot=casper console=ttyS0,115200 --" -hda ubuntu_disk.img -cdrom ubuntu-24.04.3-live-server-amd64.iso -m 196608 -smp 20 -nographic -serial mon:stdio

# After installation, you can start the VM with the disk image alone:

qemu-system-x86_64 -enable-kvm -hda ubuntu_disk.img -m 196608 -smp 20 -nographic -serial mon:stdio

userv/userv

# SSH Port forward:

qemu-system-x86_64 -net user,hostfwd=tcp::2222-:22 -net nic -enable-kvm -hda ubuntu_disk.img -m 196608 -smp 20 -nographic -serial mon:stdio

# Set Cgroup Name for PowerAPI

systemd-run --unit=ubuntu-energy-test-vm --scope  qemu-system-x86_64 -enable-kvm -kernel /tmp/iso/casper/vmlinuz ...

# Enable RAPL:

qemu-system-x86_64 -net user,hostfwd=tcp::2222-:22 -net nic -enable-kvm -accel kvm,rapl=true,rapl-helper-socket=/path/sock.sock -hda ubuntu_disk.img -m 196608 -smp 20 -nographic -serial mon:stdio


# Background process (daemonize):
systemd-run --unit=ubuntu-energy-test-vm --scope qemu-system-x86_64 -daemonize -net user,hostfwd=tcp::2222-:22,hostfwd=tcp::8181-:8081 -net nic -enable-kvm -hda ubuntu_disk.img -m 196608 -smp 20 -display none
