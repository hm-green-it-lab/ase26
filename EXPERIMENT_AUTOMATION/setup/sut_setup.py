"""
sut_setup.py

One-off setup of the system under test (SUT) for the energy measurement
experiments. Run this once per machine before starting any experiment via
``main.py``; the per-run file synchronization is handled by the automation
itself and does not need this script.

The script connects to the SUT over SSH using the credentials in ``.env`` and
the host/paths in ``paths.env`` (both in the parent directory), and then:

1. verifies that docker, docker compose, git, java (17 or 21), python3, and
   lm-sensors are available, aborting if anything is missing;
2. uploads ``../docker/`` to ``${SUT_BASE_DIR}/spring-rest-service``;
3. clones the OTJAE repository at tag ``v0.0.17-alpha`` and builds the
   ``spring-rest-service:feature`` container image;
4. downloads the PowercapReader and ProcFSReader JARs into
   ``${SUT_BASE_DIR}/work``;
5. uploads ``../vms/`` to ``${SUT_BASE_DIR}/vm`` and makes the scripts
   executable.

Usage::

    python setup/sut_setup.py [host]

The optional positional argument overrides ``SUT_HOST`` from ``paths.env``.

Note that the SUT needs outbound internet access, since steps 3 and 4 clone
from GitHub and download the reader JARs on the SUT itself. There is currently
no offline variant of this setup.
"""

from __future__ import annotations
import os
import sys
import paramiko
from pathlib import Path

def run_system_check(ssh):
    """Verify that every tool the experiments depend on is present on the SUT.

    Checks for docker, docker compose, git, java, python3, and lm-sensors, and
    additionally requires the Java major version to be 17 or 21. Results are
    printed per tool so a failing setup is diagnosable from the console output.

    Parameters
    ----------
    ssh : paramiko.SSHClient
        An established connection to the SUT.

    Raises
    ------
    RuntimeError
        If any tool is missing or the Java version is unsupported. Raised
        before anything is uploaded or built, so a misconfigured SUT fails
        fast rather than part-way through the setup.
    """
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

    # Java version check (minimal, but worth doing)
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
    """Parse a ``KEY=VALUE`` env file into a dict.

    Blank lines and ``#`` comments are skipped; only the first ``=`` splits a
    line, so values may contain further ``=`` characters. Used for both
    ``.env`` (credentials) and ``paths.env`` (hosts and directories).
    """
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
    """Open a password-authenticated SSH connection to *host*.

    Unknown host keys are auto-accepted, since the experiment machines are
    reinstalled between measurement campaigns and their keys change often.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=user, password=password)
    return ssh

def mkdir_remote(sftp, path: str):
    """Create a remote directory, ignoring the error if it already exists.

    SFTP has no ``mkdir -p`` equivalent, so ``upload_tree`` calls this for
    every directory it walks and relies on the already-exists case being a
    no-op.
    """
    try:
        sftp.mkdir(path)
    except Exception:
        pass

def upload_tree(sftp, local_root: str, remote_root: str):
    """Recursively upload *local_root* to *remote_root* over SFTP.

    Directories are created as they are encountered and every uploaded file is
    printed, so a partially failed upload can be located in the console output.
    Existing remote files are overwritten.
    """
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
    """Clone and build the Spring REST service container image on the SUT.

    Clones (or fetches, if already present) the OTJAE repository into
    ``{base_dir}/src/otjae``, checks out tag ``v0.0.17-alpha`` — the version
    evaluated in the paper — and runs the Maven build, which produces the
    ``spring-rest-service:feature`` image the docker-compose files refer to.

    Raises
    ------
    RuntimeError
        If no ``spring-rest-service`` image exists afterwards.
    """
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
    """Download the remote measurement reader JARs into ``{base_dir}/work``.

    Fetches PowercapReader (RAPL energy) and ProcFSReader (CPU, memory, I/O,
    network) directly on the SUT, skipping any JAR that is already present.
    This mirrors what ``main.py`` does before each run, so it is only a
    convenience for verifying the setup up front.

    Raises
    ------
    RuntimeError
        If no ``*runner.jar`` is present in the work directory afterwards.
    """
    print("[~] Setting up reader JARs on SUT...")

    work_dir = f"{base_dir}/work"

    ssh.exec_command(f'mkdir -p "{work_dir}"')

    # PowercapReader: pinned to the exact release used for the measurements in
    # the paper, since the RAPL energy values are the ground truth everything
    # else is compared against.
    powercap_url = (
        "https://github.com/hm-green-it-lab/powercap-reader/releases/download/"
        "v0.0.1-beta/powercap-reader-1.0-runner.jar"
    )

    # ProcFSReader: unpinned; its output format is stable, so the latest
    # release is fine here.
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
    """Upload the QEMU/KVM helper scripts from ``../vms/`` to ``{base_dir}/vm``.

    These scripts start and control the guest VM used by the ``spring_vm_*``
    configurations. They expect an already-created VM disk image — see
    ``VM_setup.md`` for how to build one.

    Raises
    ------
    RuntimeError
        If the local ``vms`` folder is missing.
    """
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
    """Run the full SUT setup: check, upload, build, and download.

    Reads credentials from ``../.env`` and hosts/paths from ``../paths.env``.
    A single positional command line argument overrides ``SUT_HOST``.

    The system check runs before the SFTP session is opened, so a SUT that is
    missing dependencies is rejected before any remote state is modified. Note
    that the target directory ``${SUT_BASE_DIR}/spring-rest-service`` is
    deleted and recreated on every invocation.
    """
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
        # SFTP only AFTER a successful check
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
