# Comment: currently Setup only works, if SUT machine has access to the Internet. Maybe we should add later on an implementation, which runs without internet on SUT Server

from __future__ import annotations
import os
import sys
import paramiko
from pathlib import Path

def run_system_check(ssh):
    print("[~] Running system requirement checks on SUT...")

    checks = {
        "docker": "command -v docker",
        "docker compose": "docker compose version",
        "git": "command -v git",
        "java": "command -v java",
        "python3": "command -v python3",
        "lm-sensors": "command -v sensors",
    }

    missing = []

    for name, cmd in checks.items():
        stdin, stdout, stderr = ssh.exec_command(
            f"{cmd} >/dev/null 2>&1 && echo OK || echo MISSING"
        )
        result = stdout.read().decode().strip()

        if result == "OK":
            print(f"[✓] {name}")
        else:
            print(f"[✗] {name} MISSING")
            missing.append(name)

    # Java version check (minimal aber sinnvoll)
    stdin, stdout, stderr = ssh.exec_command(
        "java -version 2>&1 | head -n 1"
    )
    java_version_output = stdout.read().decode().strip()
    print(f"[~] Java version: {java_version_output}")

    if not any(v in java_version_output for v in ["17", "21"]):
        print("[✗] Unsupported Java version (need >= 17)")
        missing.append("java-version")

    # Final decision
    if missing:
        print("\n[!] System check FAILED")
        print("Missing required dependencies:")
        for m in missing:
            print(f"  - {m}")
        raise RuntimeError("System requirements not met on SUT")

    print("\n[✓] System check PASSED")

def read_env_file(path: str) -> dict:
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values

def ssh_connect(host: str, user: str, password: str):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=user, password=password)
    return ssh

def mkdir_remote(sftp, path: str):
    try:
        sftp.mkdir(path)
    except Exception:
        pass

def upload_tree(sftp, local_root: str, remote_root: str):
    for root, dirs, files in os.walk(local_root):
        rel = os.path.relpath(root, local_root)
        target = remote_root
        if rel != ".":
            target = f"{remote_root}/{rel}"
        mkdir_remote(sftp, target)
        for file in files:
            local_file = os.path.join(root, file)
            remote_file = f"{target}/{file}"
            print(f"    → {remote_file}")
            sftp.put(local_file, remote_file)

def build_spring_service(ssh, base_dir: str):
    print("[~] Building Spring REST service on SUT...")

    src_dir = f"{base_dir}/src"
    repo_dir = f"{src_dir}/otjae"

    # 1. ensure src dir exists
    ssh.exec_command(f'mkdir -p "{src_dir}"')

    # 2. clone or update repo
    stdin, stdout, stderr = ssh.exec_command(f'[ -d "{repo_dir}/.git" ] && echo EXISTS || echo MISSING')
    result = stdout.read().decode().strip()

    if result == "EXISTS":
        print("    → Repo exists, updating...")
        ssh.exec_command(f'cd "{repo_dir}" && git fetch --all --tags')
    else:
        print("    → Cloning OTJAE repo...")
        ssh.exec_command(
            f'git clone https://github.com/RETIT/opentelemetry-javaagent-extension.git "{repo_dir}"'
        )

    # 3. checkout correct tag
    ssh.exec_command(
        f'cd "{repo_dir}" && git checkout tags/v0.0.17-alpha'
    )

    # 4. build project (creates docker image)
    print("    → Running Maven build (this may take a while)...")
    ssh.exec_command(
        f'cd "{repo_dir}" && chmod +x ./mvnw && ./mvnw -DskipTests package'
    )

    # 5. verify docker image exists
    stdin, stdout, stderr = ssh.exec_command(
        "docker images | grep spring-rest-service || echo MISSING"
    )
    result = stdout.read().decode()

    if "spring-rest-service" not in result:
        raise RuntimeError("Spring REST service image build failed")

    print("[✓] Spring REST service image ready")

def setup_readers(ssh, base_dir: str):
    print("[~] Setting up reader JARs on SUT...")

    work_dir = f"{base_dir}/work"

    ssh.exec_command(f'mkdir -p "{work_dir}"')

    # PowercapReader (FIXED VERSION!)
    powercap_url = (
        "https://github.com/hm-green-it-lab/powercap-reader/releases/download/"
        "v0.0.1-beta/powercap-reader-1.0-runner.jar"
    )

    # ProcFSReader (latest ok)
    procfs_url = (
        "https://github.com/hm-green-it-lab/procfs-reader/releases/latest/download/"
        "procfs-reader-1.0-runner.jar"
    )

    # Download if missing
    ssh.exec_command(
        f'[ -f "{work_dir}/powercap-reader-1.0-runner.jar" ] || '
        f'wget -q "{powercap_url}" -O "{work_dir}/powercap-reader-1.0-runner.jar"'
    )

    ssh.exec_command(
        f'[ -f "{work_dir}/procfs-reader-1.0-runner.jar" ] || '
        f'wget -q "{procfs_url}" -O "{work_dir}/procfs-reader-1.0-runner.jar"'
    )

    # Verify
    stdin, stdout, stderr = ssh.exec_command(
        f'ls "{work_dir}" | grep runner.jar || echo MISSING'
    )
    result = stdout.read().decode()

    if "MISSING" in result:
        raise RuntimeError("Reader setup failed")

    print("[✓] Reader JARs ready")

def upload_vm_scripts(ssh, sftp, base_dir: str):
    print("[~] Uploading VM scripts to SUT...")

    local_vm_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "vms"
        )
    )

    if not os.path.isdir(local_vm_dir):
        raise RuntimeError("Missing local vms folder")

    remote_vm_dir = f"{base_dir}/vm"

    ssh.exec_command(f'mkdir -p "{remote_vm_dir}"')

    upload_tree(
        sftp=sftp,
        local_root=local_vm_dir,
        remote_root=remote_vm_dir,
    )

    print("[✓] VM scripts uploaded")

def main():
    base_path = Path(__file__).resolve().parent.parent

    creds = read_env_file(base_path / ".env")
    cfg = read_env_file(base_path / "paths.env")

    if len(sys.argv) == 2:
        host = sys.argv[1]
    else:
        host = cfg["SUT_HOST"]

    user = creds["SUT_SSH_USER"]
    password = creds["SUT_SSH_PASSWORD"]

    base_dir = cfg["SUT_BASE_DIR"].rstrip("/")
    remote_dir = f"{base_dir}/spring-rest-service"

    local_docker_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "docker"
        )
    )

    if not os.path.isdir(local_docker_dir):
        raise RuntimeError("Missing local docker folder")

    print("[~] Host:", host)
    print("[~] User:", user)
    print("[~] Target:", remote_dir)

    # ─────────────────────────────
    # SSH CONNECT + LOGIN CHECK
    # ─────────────────────────────
    ssh = ssh_connect(host, user, password)

    try:
        stdin, stdout, stderr = ssh.exec_command("echo CONNECTED")
        result = stdout.read().decode().strip()

        if result != "CONNECTED":
            raise RuntimeError("SSH login failed (unexpected response)")

        print("[✓] SSH connection established")

        # ─────────────────────────────
        # SYSTEM CHECK (FAIL FAST)
        # ─────────────────────────────
        run_system_check(ssh)

        # ─────────────────────────────
        # SFTP erst NACH erfolgreichem Check
        # ─────────────────────────────
        sftp = ssh.open_sftp()

        try:
            print("[~] Preparing remote directory...")
            ssh.exec_command(f'rm -rf "{remote_dir}"')
            ssh.exec_command(f'mkdir -p "{remote_dir}"')

            print("[~] Uploading docker files...")
            upload_tree(
                sftp=sftp,
                local_root=local_docker_dir,
                remote_root=remote_dir,
            )

            build_spring_service(ssh, base_dir)

            setup_readers(ssh, base_dir)

            upload_vm_scripts(ssh, sftp, base_dir)
            # make VM scripts executable
            print("[~] Setting executable permissions for VM scripts...")

            ssh.exec_command(f'chmod +x "{base_dir}/vm/"*.sh')

            print("[✓] VM scripts are executable")

            print("[✓] Setup complete")

        finally:
            sftp.close()

    finally:
        ssh.close()

if __name__ == "__main__":
    main()
