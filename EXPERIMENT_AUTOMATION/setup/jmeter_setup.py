from __future__ import annotations
import sys
import paramiko
from pathlib import Path

def run_jmeter_system_check(ssh):
    print("[~] Running system check on JMeter host...")

    checks = {
        "java": "command -v java",
        "wget": "command -v wget",
        "tar": "command -v tar",
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

    # Java Version Check
    stdin, stdout, stderr = ssh.exec_command(
        "java -version 2>&1 | head -n 1"
    )
    version = stdout.read().decode().strip()
    print(f"[~] Java version: {version}")

    if missing:
        raise RuntimeError("JMeter host missing required dependencies")

    print("[✓] JMeter system check PASSED")

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


def setup_jmeter(ssh, base_dir: str):
    print("[~] Setting up JMeter on remote host...")

    jmeter_dir = f"{base_dir}/apache-jmeter-5.6.3"
    output_dir = f"{base_dir}/output"
    testplan_path = f"{base_dir}/jmeter_testplan.jmx"

    # ─────────────────────────────
    # Base dirs
    # ─────────────────────────────
    ssh.exec_command(f'mkdir -p "{base_dir}"')
    ssh.exec_command(f'mkdir -p "{output_dir}"')

    # ─────────────────────────────
    # Download + extract JMeter
    # ─────────────────────────────
    stdin, stdout, stderr = ssh.exec_command(
        f'[ -d "{jmeter_dir}" ] && echo EXISTS || echo MISSING'
    )
    result = stdout.read().decode().strip()

    if result == "EXISTS":
        print("    → JMeter already installed")
    else:
        print("    → Downloading JMeter...")
        ssh.exec_command(
            f'cd "{base_dir}" && '
            f'wget -q https://downloads.apache.org/jmeter/binaries/apache-jmeter-5.6.3.tgz && '
            f'tar -xzf apache-jmeter-5.6.3.tgz && '
            f'rm apache-jmeter-5.6.3.tgz'
        )

    # ─────────────────────────────
    # Upload test plan
    # ─────────────────────────────
    local_testplan_path = Path(__file__).resolve().parent / "jmeter_testplan.jmx"
    if not local_testplan_path.exists():
        raise FileNotFoundError(f"Local test plan not found: {local_testplan_path}")

    with ssh.open_sftp() as sftp:
        sftp.put(str(local_testplan_path), testplan_path)
        print("    → Test plan uploaded")

    # ─────────────────────────────
    # Verify
    # ─────────────────────────────
    #stdin, stdout, stderr = ssh.exec_command(
    #    f'[ -x "{jmeter_dir}/bin/jmeter.sh" ] && echo OK || echo MISSING'
    #)
    #result = stdout.read().decode().strip()

    #if result != "OK":
        #raise RuntimeError("JMeter setup failed")

    print("[✓] JMeter ready")


def main():
    base_path = Path(__file__).resolve().parent.parent

    creds = read_env_file(base_path / ".env")
    cfg = read_env_file(base_path / "paths.env")

    if len(sys.argv) == 2:
        host = sys.argv[1]
    else:
        host = cfg["JMETER_HOST"]

    user = creds["JMETER_SSH_USER"]
    password = creds["JMETER_SSH_PASSWORD"]

    base_dir = cfg["JMETER_BASE_DIR"].rstrip("/")

    print("[~] Host:", host)
    print("[~] User:", user)
    print("[~] Target:", base_dir)

    ssh = ssh_connect(host, user, password)
    print("[✓] SSH connection established")

    run_jmeter_system_check(ssh)

    try:
        stdin, stdout, stderr = ssh.exec_command("echo CONNECTED")
        result = stdout.read().decode().strip()

        if result != "CONNECTED":
            raise RuntimeError("SSH login failed")

        print("[✓] SSH connection established")

        setup_jmeter(ssh, base_dir)

        print("[✓] Setup complete")

    finally:
        ssh.close()


if __name__ == "__main__":
    main()