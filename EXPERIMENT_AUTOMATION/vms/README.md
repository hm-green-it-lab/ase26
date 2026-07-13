## ⚡ Scaphandre VM Setup (QEMU/KVM)

### ✅ Prerequisites (Host)
- QEMU/KVM installed and working  
- VM disk image available (e.g. `/home/<user>/ubuntu_disk.img`)  
- Scaphandre built with QEMU support:
```bash
cargo build --release --features qemu
```
## 🚀 Run on Host (SUT)
This starts the VM.
```bash
sudo /home/<user>/vm/run_experiment_scaphandre.sh
```
## 🖥️ Setup inside VM
This starts the Service in the VM and exposes the Scaphandre Energy metrics.
```bash
sudo mkdir -p /var/scaphandre
sudo mount -t 9p -o trans=virtio scaphandre /var/scaphandre

cd ~/spring-rest-service
docker-compose up -d

cd ~
./scaphandre --vm prometheus
```

## 📊 Metrics
```bash
curl localhost:8080/metrics
```









# JoularJX und PowerJoular

Requirements
PowerJoular on host avaiable

Host (skript starten)
sudo /home/<user>/test_setup_v2/vm/run_experiment_joularjx.sh

VM
sudo chown -R userv:userv ~/spring-rest-service
sudo mkdir -p /var/joular
sudo mount -t 9p -o trans=virtio joular /var/joular

Host
Cd ..
scp -P 2222 -r spring-rest-service userv@localhost:~

VM
cd ~/spring-rest-service

docker rm -f spring-rest-energy-test 2>/dev/null || true
docker-compose down --remove-orphans

docker system prune -f

docker-compose -f docker-compose.yaml -f docker-compose.override.joularjx-vm.yaml up -d --force-recreate
