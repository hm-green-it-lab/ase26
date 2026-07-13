from __future__ import annotations
from helper.hooks import _connect
import os
import socket
import time
from pathlib import Path


def sync_files_to_host(config: dict) -> None:
    """
    Synchronize the local docker directory to SUT_BASE_DIR on the SUT host,
    ensuring a clean state before each experiment run.

    The contents of SUT_BASE_DIR are deleted first, then all files from the
    local './docker' directory are uploaded to SUT_BASE_DIR.

    Raises
    ------
    RuntimeError
        If the cleanup or upload fails.
    """
    hostname = config["experiment"]["target_host"]
    username = os.environ["SUT_SSH_USER"]
    password = os.environ["SUT_SSH_PASSWORD"]
    sut_base_dir = os.environ["SUT_BASE_DIR"]

    # Resolve local docker dir relative to this file's project root
    local_docker_dir = Path(__file__).resolve().parent.parent / "docker"

    print(
        f"[SUT] Syncing '{local_docker_dir}' "
        f"to '{username}@{hostname}:{sut_base_dir}' ..."
    )

    if not local_docker_dir.exists():
        raise RuntimeError(
            f"Local docker directory not found: {local_docker_dir}"
        )

    client = _connect(hostname, username, password)

    try:
        # Delete all contents of SUT_BASE_DIR for a clean state
        cleanup_cmd = f"rm -rf {sut_base_dir} && mkdir -p {sut_base_dir}"
        _, stdout, stderr = client.exec_command(cleanup_cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode().strip()
            raise RuntimeError(
                f"Failed to clean SUT_BASE_DIR '{sut_base_dir}' on host: {error_output}"
            )

        print(f"[SUT] ✓ Cleaned '{sut_base_dir}' on host.")

        # Upload local docker directory contents to {SUT_BASE_DIR}/spring-rest-service/
        # All configuration files reference this subdirectory on the host.
        remote_target_dir = f"{sut_base_dir}/spring-rest-service"
        sftp = client.open_sftp()
        try:
            _upload_directory(sftp, local_docker_dir, remote_target_dir)
        finally:
            sftp.close()

        print("[SUT] ✓ Files synchronized to host successfully.")

        # Upload the vms/ directory to {SUT_BASE_DIR}/vm/ so that
        # host-side scripts (prepare_and_run_vm_scaphandre.sh, etc.) are
        # available at a known, configuration-driven location instead of a
        # hardcoded developer path.
        local_vms_dir = Path(__file__).resolve().parent.parent / "vms"
        remote_vm_dir = f"{sut_base_dir}/vm"
        if local_vms_dir.exists():
            sftp = client.open_sftp()
            try:
                _upload_directory(sftp, local_vms_dir, remote_vm_dir)
            finally:
                sftp.close()

            # Make all uploaded shell scripts executable
            chmod_cmd = f"chmod +x {remote_vm_dir}/*.sh 2>/dev/null || true"
            _, stdout, _ = client.exec_command(chmod_cmd)
            stdout.channel.recv_exit_status()

            print(f"[SUT] ✓ VM scripts uploaded to '{remote_vm_dir}' and marked executable.")
        else:
            print(f"[SUT] [!] Local vms/ directory not found, skipping VM script upload: {local_vms_dir}")

        # Upload remote reader JARs (procfs-reader, powercap-reader) to
        # remote_dir so the SSH-based readers can find them there.
        remote_dir = config["experiment"].get("remote_dir", "").strip()
        if remote_dir:
            local_tools = Path(__file__).resolve().parent.parent / "tools"
            jar_keys = ["procfs_jar_filename", "powercap_jar_filename"]
            jars_uploaded = []
            for key in jar_keys:
                jar_name = config["experiment"].get(key, "").strip()
                if not jar_name:
                    continue
                local_jar = local_tools / jar_name
                if not local_jar.is_file():
                    print(f"[SUT] [!] Local JAR not found, skipping upload: {local_jar}")
                    continue
                remote_jar_path = f"{remote_dir.rstrip('/')}/{jar_name}"
                # Ensure remote_dir exists
                _, stdout, _ = client.exec_command(f"mkdir -p {remote_dir}")
                stdout.channel.recv_exit_status()
                sftp2 = client.open_sftp()
                try:
                    sftp2.put(str(local_jar), remote_jar_path)
                finally:
                    sftp2.close()
                jars_uploaded.append(jar_name)
            if jars_uploaded:
                print(f"[SUT] ✓ Uploaded reader JARs to '{remote_dir}': {', '.join(jars_uploaded)}")

    finally:
        client.close()


def prepare_vm(config: dict, measurement_tool: str) -> None:
    """
        Prepare the VM environment on the SUT host.
        Runs the preparation script as root synchronously.
        """
    hostname = config["experiment"]["target_host"]
    username = os.environ["SUT_SSH_USER"]
    password = os.environ["SUT_SSH_PASSWORD"]
    sut_base_dir = os.environ["SUT_BASE_DIR"]

    script_path = f"{sut_base_dir}/vm/prepare_and_run_vm.sh"

    print(f"[VM] Preparing VM via '{script_path}' ...")

    client = _connect(hostname, username, password)

    try:
        # Execute synchronously as root
        # The script handles all cleanup, mounting, and waiting loops
        # cmd = f'sudo sh -c "{script_path}"'
        cmd = (
            f"sudo sh -c "
            f"'\"{script_path}\" \"{measurement_tool}\"'"
        )
        print(f"[~] Executing: {cmd}")

        _, stdout, stderr = client.exec_command(cmd)

        # Wait for the script to complete
        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error_output = stderr.read().decode().strip()
            print(f"[✗] VM preparation failed with exit code {exit_status}")
            print(f"[~] Error output: {error_output}")
            raise RuntimeError(f"VM preparation failed: {error_output}")

        print("[VM] ✓ Host-side preparation completed.")

    finally:
        client.close()

    vm_host = config["experiment"]["vm_guest_host"]
    vm_port = int(config["experiment"]["vm_guest_ssh_port"])

    print(
        f"[VM] Waiting for VM SSH "
        f"({vm_host}:{vm_port}) ..."
    )

    _wait_for_vm_ssh(vm_host, vm_port)

    print("[VM] ✓ VM prepared successfully.")


def sync_files(config: dict) -> None:
    """
    Synchronize the local docker directory to the VM guest.

    Copies all files and folders contained in the local
    './docker' directory to the VM guest home directory.

    Raises
    ------
    RuntimeError
        If the synchronization fails.
    """

    vm_host = config["experiment"]["vm_guest_host"]
    vm_port = int(config["experiment"]["vm_guest_ssh_port"])

    vm_user = os.environ["VM_SSH_USER"]
    vm_password = os.environ["VM_SSH_PASSWORD"]

    local_docker_dir = Path("docker")

    # TODO: later replace with VM_BASE_DIR from configuration
    remote_base_dir = "/home/userv"

    print(
        f"[VM] Syncing '{local_docker_dir}' "
        f"to '{vm_user}@{vm_host}:{vm_port}:{remote_base_dir}' ..."
    )

    if not local_docker_dir.exists():
        raise RuntimeError(
            f"Local directory not found: {local_docker_dir}"
        )

    client = _connect(
        vm_host,
        vm_user,
        vm_password,
        port=vm_port,
    )

    try:
        sftp = client.open_sftp()

        remote_docker_dir = f"{remote_base_dir}/spring-rest-service"

        _upload_directory(
            sftp,
            local_docker_dir,
            remote_docker_dir,
        )

        # Upload reader JARs
        local_tools = Path(__file__).resolve().parent.parent / "tools"

        jar_keys = [
        "procfs_jar_filename",
        ]

        for key in jar_keys:
            jar_name = config["experiment"].get(key, "").strip()

            if not jar_name:
                continue

            local_jar = local_tools / jar_name

            if not local_jar.is_file():
                print(f"[VM] [!] Local JAR not found: {local_jar}")
                continue

            remote_jar_path = f"{remote_base_dir}/{jar_name}"

            sftp.put(
                str(local_jar),
                remote_jar_path,
            )

            print(f"[VM] ✓ Uploaded {jar_name}")

        sftp.close()

        print("[VM] ✓ Files synchronized successfully.")

    finally:
        client.close()

def mount_share(config: dict, measurement_tool) -> None:
    """
    Mount the host-provided 9p shared directory inside the VM.

    The QEMU VM exposes the share via the mount tag 'scaphandre'.
    This method mounts it to '/var/scaphandre' inside the VM.

    Raises
    ------
    RuntimeError
        If the mount command fails.
    """

    vm_host = config["experiment"]["vm_guest_host"]
    vm_port = int(config["experiment"]["vm_guest_ssh_port"])

    vm_user = os.environ["VM_SSH_USER"]
    vm_password = os.environ["VM_SSH_PASSWORD"]

    mount_config = {
        "scaphandre": {
            "tag": "scaphandre",
            "path": "/var/scaphandre",
        },
        "joularjx": {
            "tag": "joularjx",
            "path": "/var/joular",
        },
    }

    if measurement_tool in ("otajae", "tools"):
        print(f"[VM] No shared directory mount required for {measurement_tool}.")
        return

    if measurement_tool not in mount_config:
        raise RuntimeError(f"Unsupported measurement tool for mount: {measurement_tool}")

    tag = mount_config[measurement_tool]["tag"]
    path = mount_config[measurement_tool]["path"]

    mount_cmd = (
        f"sudo mkdir -p {path} && "
        f"(mountpoint -q {path} || "
        f"sudo mount -t 9p -o trans=virtio {tag} {path})"
    )

    print(f"[VM] Mounting shared {measurement_tool} directory ...")

    client = _connect(
        vm_host,
        vm_user,
        vm_password,
        port=vm_port,
    )

    try:
        _, stdout, stderr = client.exec_command(mount_cmd)

        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error_output = stderr.read().decode().strip()

            print("[!] Failed to mount shared directory.")
            print(error_output)

            raise RuntimeError("VM share mount failed.")

        print("[VM] ✓ Shared directory mounted.")

    finally:
        client.close()

def cleanup_vm_docker(config: dict, measurement_tool) -> None:
    """
    Remove leftover Docker resources from previous VM experiments.

    This ensures that stale containers and orphaned compose resources
    do not interfere with the next experiment run.

    Raises
    ------
    RuntimeError
        If the cleanup command fails.
    """

    vm_host = config["experiment"]["vm_guest_host"]
    vm_port = int(config["experiment"]["vm_guest_ssh_port"])

    vm_user = os.environ["VM_SSH_USER"]
    vm_password = os.environ["VM_SSH_PASSWORD"]

    print("[VM] Cleaning up Docker environment ...")

    cleanup_cmd = """
        cd ~/spring-rest-service && \
        sudo chown -R userv:userv ~/spring-rest-service && \
        docker-compose down --remove-orphans || true && \
        docker rm -f spring-rest-energy-test 2>/dev/null || true
    """

    if measurement_tool == "joularjx":
        cleanup_cmd += """
    docker system prune -f || true
    """

    client = _connect(
        vm_host,
        vm_user,
        vm_password,
        port=vm_port,
    )

    try:
        _, stdout, stderr = client.exec_command(cleanup_cmd)

        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error_output = stderr.read().decode().strip()

            print("[!] VM Docker cleanup failed.")
            print(error_output)

            raise RuntimeError(
                "VM Docker cleanup failed."
            )

        print("[VM] ✓ Docker cleanup completed.")

    finally:
        client.close()

def validate_pipeline(config: dict) -> None:
    # TODO: optional future validation step
    pass






def _wait_for_vm_ssh(
    host: str,
    port: int,
    timeout: int = 60,
) -> None:
    """
    Wait until the VM SSH endpoint becomes reachable.

    Parameters
    ----------
    host : str
        VM guest host address.
    port : int
        VM guest SSH port.
    timeout : int, optional
        Maximum wait time in seconds.

    Raises
    ------
    RuntimeError
        If the VM does not become reachable within the timeout.
    """

    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            with socket.create_connection(
                (host, port),
                timeout=2,
            ):
                print(f"[VM] ✓ SSH reachable at {host}:{port}")
                return

        except OSError:
            time.sleep(1)

    raise RuntimeError(
        f"VM SSH not reachable after {timeout} seconds "
        f"({host}:{port})."
    )



def _upload_directory(sftp, local_dir: Path, remote_dir: str) -> None:
    """
    Recursively upload a local directory to a remote location via SFTP.
    """

    try:
        sftp.mkdir(remote_dir)
    except OSError:
        # Directory already exists
        pass

    for item in local_dir.iterdir():

        remote_path = f"{remote_dir}/{item.name}"

        if item.is_dir():
            _upload_directory(
                sftp,
                item,
                remote_path,
            )
        else:
            sftp.put(
                str(item),
                remote_path,
            )



def cleanup_joularjx_vm(config: dict) -> None:
    """
    Remove old JoularJX artifacts inside the VM before a new run.

    Cleans:
    - JoularJX result directory
    - Shared VM power files (/var/joular/*.csv)

    Raises
    ------
    RuntimeError
        If cleanup fails.
    """

    vm_host = config["experiment"]["vm_guest_host"]
    vm_port = int(config["experiment"]["vm_guest_ssh_port"])

    vm_user = os.environ["VM_SSH_USER"]
    vm_password = os.environ["VM_SSH_PASSWORD"]

    print("[VM] Cleaning JoularJX environment ...")

    cleanup_cmd = """
        rm -rf /home/userv/spring-rest-service/joularjx/joularjx-result/* || true
        rm -f /var/joular/*.csv || true
    """

    client = _connect(
        vm_host,
        vm_user,
        vm_password,
        port=vm_port,
    )

    try:
        _, stdout, stderr = client.exec_command(cleanup_cmd)

        exit_status = stdout.channel.recv_exit_status()

        if exit_status != 0:
            error_output = stderr.read().decode().strip()

            print("[!] JoularJX cleanup failed.")
            print(error_output)

            raise RuntimeError(
                "JoularJX VM cleanup failed."
            )

        print("[VM] ✓ JoularJX cleanup completed.")

    finally:
        client.close()
