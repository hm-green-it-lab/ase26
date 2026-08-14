"""
helper/joularjx.py

Collection of JoularJX measurement artifacts after a ``spring_*_joularjx`` run.

JoularJX writes a large number of small per-method and per-thread CSV files
into its result directory on the measured machine. Transferring those
individually over SFTP is prohibitively slow, so this module first zips the
result directory remotely and then downloads the archive.

The archives land in ``{local_output_root}/joularjx-result_{HHMMSS}/`` and are
kept packed: the analysis scripts extract them on demand (see
``extract_joularjx_zip`` in ``EXPERIMENT_RESULTS/shared.py``).

The remote host is the guest VM for ``spring_vm_*`` experiments and the SUT
otherwise. In the RS2/RS3 load distribution experiments both containers are
collected, with the second one's artifacts suffixed ``-2``.
"""

from __future__ import annotations

import os
from stat import S_ISDIR
from datetime import datetime

import paramiko

def fetch_joularjx_artifacts(
    config: dict,
    local_output_root: str,
    cleanup_remote: bool = True
) -> None:
    """Zip JoularJX's results on the remote host and download them.

    For each result directory (one per application container; RS2/RS3 runs have
    two), the directory is chowned to the SSH user, zipped remotely into
    ``joularjx_zip_dir``, and then fetched via SFTP.

    Parameters
    ----------
    config :
        The ``experiment`` section of the run configuration. Determines the
        remote host (guest VM for ``spring_vm_*`` types, SUT otherwise) and
        supplies ``joularjx_result_dir`` and ``joularjx_zip_dir``.
    local_output_root :
        Local run directory the archives are placed in, under a
        ``joularjx-result_{HHMMSS}`` subfolder.
    cleanup_remote :
        When True (default), the contents of the remote result directories are
        deleted afterwards, so the next repetition starts from an empty
        directory and cannot pick up stale measurements.

    The function returns without doing anything if the host or remote
    directory is not configured, and raises RuntimeError if the remote zip
    fails.
    """
    experiment = config

    if experiment["type"].startswith("spring_vm_"):
        sut_host = experiment["vm_guest_host"]
        port = int(experiment["vm_guest_ssh_port"])

        j_user = os.environ["VM_SSH_USER"]
        j_pass = os.environ["VM_SSH_PASSWORD"]

        remote_dir = experiment.get("vm_remote_dir")
    else:
        sut_host = experiment["target_host"]
        port = 22

        j_user = os.environ["SUT_SSH_USER"]
        j_pass = os.environ["SUT_SSH_PASSWORD"]

        remote_dir = experiment.get("remote_dir")

    joularjx_result_dir = experiment.get("joularjx_result_dir")
    joularjx_zip_dir = experiment.get("joularjx_zip_dir")
    result_dirs = [joularjx_result_dir]

    if experiment["type"].endswith(("_rs2", "_rs3")):
        result_dirs.append(f"{joularjx_result_dir}-2")

    if not sut_host or not remote_dir:
        print("[JoularJX][fetch] Missing target_host/remote_dir; skip fetch.")
        return

    # Determine destination folder based on the dt used for filenames
    dt = config.get("__jmeter_dt__") or _ts("%Y%m%d_%H%M%S")
    day_tag, time_tag = dt.split("_", 1)

    # Use the orchestrator-provided root as-is (absolute or relative)
    dest_dir = os.path.join(local_output_root, f"joularjx-result_{time_tag}")

    print(f"[JoularJX][fetch] Downloading artifacts to: {dest_dir}")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=sut_host, username=j_user, password=j_pass, port=port)

        for idx, result_dir in enumerate(result_dirs, start=1):

            ssh.exec_command(f"sudo chown -R {j_user} {result_dir}")

            suffix = "" if idx == 1 else "-2"

            zip_file = (
                f"{joularjx_zip_dir}/joularjx-result{suffix}_{time_tag}.zip"
            )

            stdin, stdout, stderr = ssh.exec_command(
                f"mkdir -p {joularjx_zip_dir} && "
                f"cd {result_dir} && "
                f"zip -r {zip_file} ."
            )

            exit_status = stdout.channel.recv_exit_status()

            if exit_status != 0:
                error_output = stderr.read().decode().strip()
                raise RuntimeError(error_output)

        download_and_cleanup_sftp_recursive(
            hostname=sut_host,
            username=j_user,
            password=j_pass,
            joularjx_result_dir=joularjx_zip_dir,
            local_output_dir=dest_dir,
            cleanup_remote=True,
            port=port,
        )

        if cleanup_remote:
            for result_dir in result_dirs:
                ssh.exec_command(f"rm -rf {result_dir}/*")
                print(f"[JoularJX][fetch] Deleted content from joularjx: {result_dir}")
            
#       Extract the ZIP file
#       with zipfile.ZipFile(dest_dir + f"\\joularjx-result_{time_tag}.zip", 'r') as zip_ref:
#       longest = max(
#           len(os.path.join(dest_dir, name))
#           for name in zip_ref.namelist()
#       )
#
#       print(f"[JoularJX] Longest path length: {longest}")
#
#       zip_ref.extractall(dest_dir)

#   Delete the ZIP file after extraction
#   os.remove(dest_dir + f"\\joularjx-result_{time_tag}.zip")
    finally:
        ssh.close()

def download_and_cleanup_sftp_recursive(hostname, username, password, joularjx_result_dir, local_output_dir, cleanup_remote=False,port=22,):
    """Recursively download a remote directory tree over SFTP.

    Mirrors *joularjx_result_dir* into *local_output_dir*, creating local
    directories as needed. When *cleanup_remote* is True, each file is removed
    right after it has been downloaded and each directory once it is empty, so
    the remote side is left clean for the next repetition.

    Every processed path is printed, so an interrupted transfer can be traced
    in the console output.

    Raises
    ------
    Exception
        Any SFTP or filesystem error is re-raised after being reported; the
        transport is always closed.
    """
    try:
        # Establish the SFTP connection
        transport = paramiko.Transport((hostname, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        # Make sure the local directory exists
        os.makedirs(local_output_dir, exist_ok=True)

        def process_directory(remote_dir, local_dir):
            """Recursively download one remote directory into *local_dir*."""
            try:
                # List all entries in the current remote directory
                items = sftp.listdir_attr(remote_dir)

                for item in items:
                    remote_path = os.path.join(remote_dir, item.filename).replace("\\", "/")
                    local_path = os.path.join(local_dir, item.filename)

                    if S_ISDIR(item.st_mode):
                        # Directory: create it locally and recurse into it
                        print(f"Processing directory: {remote_path}")
                        os.makedirs(local_path, exist_ok=True)
                        process_directory(remote_path, local_path)

                        # Remove the now-empty remote directory if cleanup is enabled
                        if cleanup_remote:
                            sftp.rmdir(remote_path)
                    else:
                        # File: download it
                        print(f"Downloading: {remote_path}")
                        sftp.get(remote_path, local_path)

                        # Remove the remote file if cleanup_remote is enabled
                        if cleanup_remote:
                            print(f"Deleting remote file: {remote_path}")
                            sftp.remove(remote_path)

            except Exception as e:
                print(f"Error while processing directory {remote_dir}: {str(e)}")
                raise

        try:
            # Start the recursive download
            process_directory(joularjx_result_dir, local_output_dir)

            if cleanup_remote:
                print(f"All files from {joularjx_result_dir} were downloaded recursively and deleted")
            else:
                print(f"All files from {joularjx_result_dir} were downloaded recursively")

        except Exception as e:
            print(f"Error while processing the files: {str(e)}")
            raise

    except Exception as e:
        print(f"Error while establishing the connection: {str(e)}")
        raise

    finally:
        # Close the connection
        if 'sftp' in locals():
            sftp.close()
        if 'transport' in locals():
            transport.close()


def _ts(fmt: str = "%Y%m%d_%H%M%S") -> str:
    """Return current local time formatted as string."""
    return datetime.now().strftime(fmt)