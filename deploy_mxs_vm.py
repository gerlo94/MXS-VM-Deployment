from shutil import copytree, rmtree  
from urllib.request import urlretrieve
from urllib.parse import urlsplit
from typing import List, Tuple, Union
import traceback
import getpass
import grp
import os
import subprocess
import shutil
import sys
import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory
 
class EntireScript:
    def log(self, level, message):
        logging.log(level, message)

class Stage1(EntireScript):
    MIN_REQUIRED_SPACE_GB = 7
    VIRTIO_ISO_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win.iso"
    LIBVIRT_CONFIG_PATH = "/etc/libvirt/libvirtd.conf"
    QEMU_CONFIG_PATH = "/etc/libvirt/qemu.conf"
    REQUIRED_PACKAGES = [
        "qemu-system-x86", "libvirt-clients", "libvirt-daemon-system",
        "libvirt-daemon-config-network", "bridge-utils", "virt-manager", "ovmf", "wimtools"
    ]

    def __init__(self):
        """Initialize the script."""
        self.init_temp_dirs()  

    def init_temp_dirs(self):
        """Initialize temporary directories."""
        self.temp_dir = TemporaryDirectory()
        self.wimtemp_dir = TemporaryDirectory()
        self.drivers_dir = TemporaryDirectory()
        self.windows_dir = TemporaryDirectory()
        self.virtio_mount_dir = TemporaryDirectory()
        self.windows_mount_dir = TemporaryDirectory()

    def main(self) -> None:
        self.log(logging.INFO, "Starting the main sequence of the script.")
        self.log(logging.INFO, "Prompting for ISO choice.")
        provided_win_iso, _, _, user_choice = self.prompt_for_iso_choice()

        self.log(logging.INFO, "Installing required packages.")
        self.install_packages(self.REQUIRED_PACKAGES)

        self.log(logging.INFO, "Handling user's ISO choice.")
        if user_choice == "1":
            final_win_iso_path = self.handle_user_provided_iso(provided_win_iso)
        elif user_choice == "2":
            final_win_iso_path = self.create_iso_with_virtio_from_user_iso(provided_win_iso)
        elif user_choice == "3":
            final_win_iso_path = self.handle_downloaded_iso()
        else:
            self.fail("Invalid choice.")

        self.setup_libvirt()

        self.create_vm("MyVM", final_win_iso_path)

    def fail(self, msg: str, exception: Exception = None) -> None:
        if exception:
            logging.error(f"{msg}. Exception: {exception}. Traceback: {traceback.format_exc()}")
        else:
            logging.error(msg)
        sys.exit(1)


    def run_subprocess(self, cmd: List[str], fail_msg: str) -> None:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Command failed. Error: {e.stderr}")
            raise Exception(fail_msg)

    def clear_directory(self, dir_path: str) -> None:
        for filename in os.listdir(dir_path):
            file_path = os.path.join(dir_path, filename)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logging.error(f"Failed to delete {file_path}. Reason: {e}")
                
    def sudo_tee_write(self, file_path, content):
        try:
            process = subprocess.run(["sudo", "tee", file_path], input=content, text=True, check=True, capture_output=True)
            if process.returncode != 0:
                raise Exception(f"Failed to write to {file_path} using sudo tee")
        except Exception as e:
            self.fail(f"Failed to modify {file_path}. Error: {e}")
            raise Exception(f"Exiting due to failure in modifying {file_path}")

    def sudo_cat_read(self, file_path):
        try:
            process = subprocess.run(["sudo", "cat", file_path], text=True, capture_output=True, check=True)
            return process.stdout
        except Exception as e:
            self.fail(f"Failed to read {file_path}. Error: {e}")
            raise Exception(f"Exiting due to failure in reading {file_path}")
        
    def cleanup_temp_dirs(self):
        """Cleanup the temporary directories."""
        for temp_dir in [self.temp_dir, self.drivers_dir, self.windows_dir, self.virtio_mount_dir, self.windows_mount_dir]:
            if self.is_mounted(Path(temp_dir.name)):
                self.unmount(Path(temp_dir.name))
            temp_dir.cleanup()
        self.init_temp_dirs()

    def prompt_for_iso_choice(self) -> Tuple[str, str, str, str]:
        """Prompt the user to select an ISO option and return the selected values."""
        options = [
            "Use your own Windows 10 ISO that has VirtIO drivers installed.",
            "Use your own Windows 10 ISO that needs to have VirtIO drivers installed.",
            "Securely download a new Windows 10 ISO and install VirtIO drivers."
        ]

        for i, option in enumerate(options, start=1):
            print(f"{i}. {option}")

        while True:
            try:
                choice = input("Select an option: ")
                if choice not in ["1", "2", "3"]:
                    raise ValueError("Invalid choice, please try again.")
                break
            except ValueError as e:
                print(e)

        iso_ref = ""
        if choice in ["1", "2"]:
            iso_ref = input("Please enter the path to the existing ISO: ")

            iso_ref = os.path.expanduser(iso_ref)

            if not os.path.isfile(iso_ref):
                self.fail(f"{iso_ref} does not exist. Exiting.")

        skip_ref = "true" if choice == "1" else "false"
        download_ref = "false" if choice == "2" else "true"

        return iso_ref, skip_ref, download_ref, choice

    def handle_user_provided_iso(self, provided_win_iso: str) -> str:
        """Handle the case where the user provides their own ISO with VirtIO drivers."""
        return provided_win_iso

    def create_iso_with_virtio_from_user_iso(self, provided_win_iso: str) -> str:
        self.log(logging.INFO, "Create custom ISO with VirtIO drivers.")
        self.cleanup_temp_dirs()
        try:
            actual_virtio_url = self.get_redirected_url(self.VIRTIO_ISO_URL)
            virtio_iso = os.path.basename(urlsplit(actual_virtio_url).path)

            self.download_file(actual_virtio_url, virtio_iso)
            custom_iso_path = self.create_custom_iso(provided_win_iso, virtio_iso)

            return custom_iso_path
        except Exception as e:
            self.fail(f"Failed to create custom ISO. Error: {e}", e)
 
    def handle_downloaded_iso(self) -> str:
        """Handle the case where the user opts to download a new ISO."""
        self.log(logging.INFO, "Starting to handle downloaded ISO.")
        #self.check_disk_space()
        self.download_file("https://raw.githubusercontent.com/ElliotKillick/Mido/main/Mido.sh", "Mido.sh")
        self.log(logging.INFO, "Custom ISO created successfully.")
        os.chmod("Mido.sh", 0o755)
 
        if subprocess.call(["./Mido.sh", "win10x64"]) != 0:
            self.fail("Failed to download Windows 10 ISO.")
 
        downloaded_win_iso = "win10x64.iso"
 
        if not os.path.isfile(downloaded_win_iso):
            self.fail(f"{downloaded_win_iso} not found. Something went wrong.")
 
        virtio_iso_url = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win.iso"
        actual_virtio_url = self.get_redirected_url(virtio_iso_url)
        virtio_iso = os.path.basename(urlsplit(actual_virtio_url).path)
 
        self.download_file(actual_virtio_url, virtio_iso)
 
        custom_iso_path = self.create_custom_iso(downloaded_win_iso, virtio_iso)
        print("Custom ISO Created")
        return custom_iso_path
 
    def get_redirected_url(self, url: str) -> str:
        try:
            self.log(logging.DEBUG, f"Debug: Getting redirected URL for {url}.")
            return subprocess.check_output(["curl", "-sIL", "-o", "/dev/null", "-w", "%{url_effective}", url]).decode().strip()
        except Exception as e:
            self.log(logging.ERROR, f"Error in get_redirected_url: {e}")
            self.fail(f"Failed to get redirected URL. Error: {e}", e)
 
    def create_custom_iso(self, win_iso: str, virtio_iso: str) -> str:
        mounted_isos = []
        try:
            self.prepare_directories_for_custom_iso()
            self.copy_virtio_drivers(virtio_iso)
            mounted_isos.append(self.virtio_mount_dir.name)
            self.copy_windows_files(win_iso)
            mounted_isos.append(self.windows_mount_dir.name)
            self.add_drivers_to_windows_boot_images()
            return self.generate_custom_iso()
        except Exception as e:
            self.fail(f"Failed to create custom ISO. Error: {e}", e)
        finally:
            for iso in mounted_isos:
                self.unmount(iso)
 
    def prepare_directories_for_custom_iso(self) -> None:
        """Prepare directories needed for creating a custom ISO."""
        for temp_dir in [
            self.temp_dir,
            self.drivers_dir,
            self.windows_dir,
            self.virtio_mount_dir,
            self.windows_mount_dir,
            self.wimtemp_dir,
        ]:
            dir_path = Path(temp_dir.name)
            if dir_path.exists():
                shutil.rmtree(dir_path)
            os.makedirs(dir_path, exist_ok=True)
 
    def copy_virtio_drivers(self, virtio_iso: str) -> None:
        """Copy VirtIO drivers from the VirtIO ISO."""
        self.log(logging.INFO, "Copying over VirtIO drivers.")
        try:
            self.mount_iso(virtio_iso, self.virtio_mount_dir.name)
            self.copy_tree(self.virtio_mount_dir.name, self.drivers_dir.name)
            self.unmount(self.virtio_mount_dir.name)
        except Exception as e:
            self.log(logging.ERROR, f"Error in copy_virtio_drivers: {e}")
            self.fail(f"Failed to copy VirtIO drivers. Error: {e}", e)
 
    def copy_windows_files(self, win_iso: str) -> None:
        """Copy Windows files from the Windows ISO."""
        self.log(logging.INFO, "Copying over Windows files.")
        try:
            self.mount_iso(win_iso, self.windows_mount_dir.name) 
            self.copy_tree(self.windows_mount_dir.name, self.windows_dir.name)
            self.unmount(self.windows_mount_dir.name)
        except Exception as e:
            self.log(logging.ERROR, f"Error in copy_windows_files: {e}")
            self.fail(f"Failed to copy Windows files. Error: {e}", e)
 
    def add_drivers_to_windows_boot_images(self) -> None:
        """Add drivers to Windows boot images."""
        self.log(logging.INFO, "Adding drivers to Windows boot images.")
        for image_index in [1, 2]:
            self.mount_wim(f"{self.windows_dir.name}/sources/boot.wim", image_index)
            self.copy_tree(self.drivers_dir.name, self.wimtemp_dir.name)
            self.unmount_wim()
 
    def generate_custom_iso(self) -> str:
        """Generate a custom ISO containing both Windows and VirtIO drivers."""
        self.log(logging.INFO, "Generating custom ISO.")
        mkisofs_command = [
            "sudo", "mkisofs", "-allow-limited-size", "-o", "CustomWin10.iso", "-b", "boot/etfsboot.com", "-no-emul-boot",
            "-boot-load-seg", "0x07C0", "-boot-load-size", "8", "-iso-level", "2", "-J", "-l", "-D", "-N", "-joliet-long",
            "-relaxed-filenames", "-V", "Custom Win10", "-allow-lowercase", "-hide", "boot.catalog", self.windows_dir.name
        ]
        self.run_subprocess(mkisofs_command, "Failed to create custom ISO.")
        self.log(logging.INFO, "Custom ISO generated successfully.")
        return "CustomWin10.iso"
 
    def download_file(self, url: str, dest: str) -> None: 
        try:
            dest_path = Path(dest)  
            self.log(logging.DEBUG, f"Debug: Downloading file from {url} to {dest_path}.")
            urlretrieve(url, dest_path)
            if not dest_path.exists():
                self.fail(f"Download failed, file {dest_path} does not exist.")
            self.log(logging.INFO, f"Successfully downloaded from {url} to {dest_path}.")
        except Exception as e:
            self.log(logging.ERROR, f"Error in download_file: {e}")
            self.fail(f"Failed to download {dest_path}. Error: {e}", e)
 
    def mount_iso(self, iso_path: Union[str, Path], mount_point: Union[str, Path]) -> None:
        """Enhanced ISO mount method with improved error handling."""
 
        iso_path = Path(iso_path)
        mount_point = Path(mount_point) 
        
        if not isinstance(iso_path, Path):
            self.log(logging.ERROR, f"iso_path is not a Path object. It's a {type(iso_path)}.")
            return
 
        if not isinstance(mount_point, Path):
            self.log(logging.ERROR, f"mount_point is not a Path object. It's a {type(mount_point)}.")
            return
 
        if not iso_path.exists():
            self.log(logging.ERROR, f"ISO path {iso_path} does not exist.")
            return
 
        if not mount_point.exists():
            self.log(logging.ERROR, f"Mount point {mount_point} does not exist.")
            return
 
        cmd = ["sudo", "mount", "-o", "loop", str(iso_path), str(mount_point)]
 
        try:
            self.run_subprocess(cmd, f"Failed to mount {iso_path} to {mount_point}")
            if not any(mount_point.iterdir()):
                self.fail(f"Failed to mount {iso_path} to {mount_point}. The directory is empty.")
            self.log(logging.INFO, f"Successfully mounted {iso_path} to {mount_point}.")
        except Exception as e:

            self.log(logging.ERROR, f"Debug Info: ISO Path exists: {iso_path.exists()}, Mount Point exists: {mount_point.exists()}")
            self.fail(f"Failed to mount {iso_path} to {mount_point}", e)
 
    def unmount(self, mount_point: Path) -> None:
        """Enhanced unmount method with improved error handling."""
 
        if not isinstance(mount_point, Path):
            mount_point = Path(mount_point)
 
        cmd = ["sudo", "umount", str(mount_point)]
        if not self.is_mounted(mount_point):  
            self.log(logging.WARNING, f"{mount_point} is not mounted.")
            return
        try:
            self.run_subprocess(cmd, f"Failed to unmount {mount_point}")
            if any(mount_point.iterdir()):
                self.fail(f"Failed to unmount {mount_point}. The directory is not empty.")
            self.log(logging.INFO, f"Successfully unmounted {mount_point}.")
        except PermissionError:
            self.log(logging.ERROR, f"Permission error occurred while unmounting {mount_point}")
            self.fail(f"Failed to unmount {mount_point} due to permission error.")
        except Exception as e:
            self.fail(f"Failed to unmount {mount_point}", e)
 
 
    def is_mounted(self, path: Path) -> bool:
        """Check if a path is a mount point."""
        return os.path.ismount(path)
 
 
    def mount_wim(self, wim_path: str, index: int) -> None:
        """Mount a WIM image to a temporary directory."""
        self.log(logging.INFO, f"Mounting WIM image from {wim_path} at index {index} to {self.wimtemp_dir.name}")
 
        # Verify that WIM file exists
        if not os.path.exists(wim_path):
            self.log(logging.ERROR, f"WIM file {wim_path} does not exist.")
            return
 
        # Construct the command
        cmd = ["sudo", "wimmountrw", wim_path, str(index), self.wimtemp_dir.name]
 
        try:
            # Run the command
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.log(logging.INFO, "Successfully mounted WIM image.")
 
        except subprocess.CalledProcessError as e:
            self.log(logging.ERROR, f"Failed to mount WIM image. Error: {e.stderr.decode().strip()}")
 
    def unmount_wim(self) -> None:
        """Unmount the WIM image and commit changes."""
        cmd = ["sudo", "wimunmount", "--commit", self.wimtemp_dir.name]
        self.run_subprocess(cmd, "Failed to unmount WIM image.")
 
    def copy_tree(self, src, dest):
        try:
            copytree(src, dest, dirs_exist_ok=True)
        except FileExistsError:
            logging.error(f"{dest} already exists.")
        except PermissionError:
            logging.error(f"Do not have the necessary permissions to copy to {dest}.")
        except Exception as e:
            for src, dst, msg in e.args[0]:
                # src is source name
                logging.error(f"Error occurred when copying {src} to {dst}: {msg}")
        except:
            logging.error(f"An unexpected error occurred: {e}") 
            
    def check_disk_space(self, directory: Path) -> None:
        """Check if there's enough disk space in a given directory."""
        try:
            statvfs = os.statvfs(directory)
            available_space = statvfs.f_frsize * statvfs.f_bavail
            min_required_space = self.MIN_REQUIRED_SPACE_GB * 1024 * 1024 * 1024  # Convert to bytes
 
            if available_space < min_required_space:
                self.fail(f"Not enough disk space. Please clear at least {self.MIN_REQUIRED_SPACE_GB}GB.")
        except Exception as e:
            self.fail(f"Failed to check disk space. Error: {e}")

    def install_packages(self, packages: List[str]) -> None:
        self.log(logging.INFO, "Starting package installation.") 
        """Install required packages if they are not already installed."""
        self.log(logging.INFO, "Checking if required packages are already installed...")
        all_installed = all(subprocess.call(["dpkg-query", "-W", "-f=${Status}", package], stdout=subprocess.DEVNULL) == 0 for package in packages)
 
        if not all_installed:
            self.log(logging.INFO, "Installing required packages...")
            self.run_subprocess(["sudo", "apt", "update"], "Failed to update package list.")
            self.run_subprocess(["sudo", "apt", "install", "-y"] + packages, "Failed to install packages.")
        else:
            self.log(logging.INFO, "All required packages are already installed.")
        self.log(logging.INFO, "Finished package installation.")
       
    def setup_libvirt(self) -> None:
        """Configure the libvirt service and related settings."""
        self.log(logging.INFO, "Configuring libvirt.")        
        self.add_user_to_libvirt_and_kvm_groups()
        self.modify_and_backup_libvirt_config()
        self.manage_libvirtd_service("enable", "start")
        self.modify_and_backup_qemu_config()
        self.restart_libvirtd_service()
        self.enable_virsh_default_network()
        self.log(logging.INFO, "libvirt configured successfully.")
 
    def add_user_to_libvirt_and_kvm_groups(self) -> None:
        """Add the current user to the kvm and libvirt groups."""
        try:
            user = getpass.getuser()
            self.run_subprocess(["sudo", "usermod", "-a", "-G", "kvm,libvirt", user],
                                "Failed to add the user to kvm and libvirt groups.")
        except Exception as e:
            self.fail(f"Failed to add user to kvm and libvirt groups. Error: {e}") 
            
    def restart_libvirtd_service(self) -> None:
        """Restart the libvirtd service."""
        self.manage_libvirtd_service("restart")
 
    def enable_virsh_default_network(self) -> None:
        """Enable the default network for virsh."""
        self.run_subprocess(["sudo", "virsh", "net-autostart", "default"],
                            "Failed to enable the default network for virsh.")

    def backup_file(self, file_path: str) -> None:
        """Create a backup of a given file."""
        try:
            if os.path.exists(file_path):
                backup_path = f"{file_path}.backup"
                subprocess.run(["sudo", "cp", file_path, backup_path])
                self.log(logging.INFO, f"Backup of {file_path} created.")
            else:
                self.log(logging.WARNING, f"File {file_path} does not exist, skipping backup.")
        except Exception as e:
            self.fail(f"Failed to create backup of {file_path}. Error: {e}")

    def modify_config(self, file_path, search_string, replace_string):
        try:
            filedata = self.sudo_cat_read(file_path) 

            if search_string in filedata:
                filedata = filedata.replace(search_string, replace_string)
                self.sudo_tee_write(file_path, filedata)
                self.log(logging.INFO, f"Modified {file_path}.")
            else:
                self.log(logging.WARNING, f"Search string '{search_string}' not found in {file_path}")

        except Exception as e:
            self.fail(f"Failed to modify {file_path}. Error: {e}")
            raise Exception(f"Exiting due to failure in modifying {file_path}")
    
    def modify_and_backup_libvirt_config(self) -> None:
        try:
            libvirt_config_path = "/etc/libvirt/libvirtd.conf"
            backup_path = f"{libvirt_config_path}.backup"

            self.log(logging.INFO, "Checking if libvirt configuration file exists...")
            if not os.path.isfile(libvirt_config_path):
                self.fail(f"{libvirt_config_path} not found. Exiting.")

            self.log(logging.INFO, "Checking if backup file already exists...")
            if not os.path.isfile(backup_path):
                self.log(logging.INFO, "Backup file does not exist. Creating backup...")
                self.backup_file(libvirt_config_path)
            else:
                self.log(logging.INFO, "Backup file already exists. Skipping backup.")

            self.log(logging.INFO, "Modifying libvirt configuration...")
            self.modify_config(libvirt_config_path, "#unix_sock_group = \"libvirt\"", "unix_sock_group = \"libvirt\"")
            self.modify_config(libvirt_config_path, "#unix_sock_rw_perms = \"0770\"", "unix_sock_rw_perms = \"0770\"")

            self.log(logging.INFO, "Checking if additional settings already exist in libvirt configuration...")
            additional_settings_exist = True
            additional_settings = 'log_filters="3:qemu 1:libvirt"\nlog_outputs="2:file:/var/log/libvirt/libvirtd.log"\n'

            with open(libvirt_config_path, 'r') as file:
                file_contents = file.read()
                for line in additional_settings.strip().split('\n'):
                    if line not in file_contents:
                        additional_settings_exist = False
                        break

            if not additional_settings_exist:
                self.log(logging.INFO, "Appending additional settings to libvirt configuration...")
                with open("/tmp/additional_libvirt_settings", "w") as temp_file:
                    temp_file.write(additional_settings)
                subprocess.run(["sudo", "tee", "-a", libvirt_config_path], input=open("/tmp/additional_libvirt_settings").read(), text=True, check=True)
                os.remove("/tmp/additional_libvirt_settings")
            else:
                self.log(logging.INFO, "Additional settings already exist in libvirt configuration. Skipping.")

            self.log(logging.INFO, "Libvirt configuration successfully modified and backed up.")
        except Exception as e:
            self.fail(f"Failed to modify and backup libvirt configuration. Error: {e}")
            raise Exception("Exiting due to failure in modifying and backing up libvirt configuration")
    
    def manage_libvirtd_service(self, *actions: str) -> None:
        """Manage the libvirtd service with the given actions."""
        for action in actions:
            self.run_subprocess(["sudo", "systemctl", action, "libvirtd"],
                                f"Failed to {action} the libvirtd service.")
            
    def modify_and_backup_qemu_config(self) -> None:
        try:
            qemu_config_path = "/etc/libvirt/qemu.conf"
            backup_path = f"{qemu_config_path}.backup"

            self.log(logging.INFO, "Checking if qemu configuration file exists...")
            if not os.path.isfile(qemu_config_path):
                self.fail(f"{qemu_config_path} not found. Exiting.")

            self.log(logging.INFO, "Checking if backup file already exists...")
            if not os.path.isfile(backup_path):
                self.log(logging.INFO, "Backup file does not exist. Creating backup...")
                self.backup_file(qemu_config_path)
            else:
                self.log(logging.INFO, "Backup file already exists. Skipping backup.")

            self.log(logging.INFO, "Modifying qemu configuration...")
            user = getpass.getuser()

            self.modify_config(qemu_config_path, "#user = \"root\"", f"user = \"{user}\"")
            self.modify_config(qemu_config_path, "#group = \"root\"", "group = \"libvirt\"")

            self.log(logging.INFO, "qemu configuration successfully modified and backed up.")
        except Exception as e:
            self.fail(f"Failed to modify and backup qemu configuration. Error: {e}")
            raise Exception("Exiting due to failure in modifying and backing up qemu configuration")

    
    def enable_default_network_for_virsh(self) -> None:
        """Enable the default network for virsh."""
        self.run_subprocess(["sudo", "virsh", "net-autostart", "default"],
                            "Failed to enable the default network for virsh.")
 
    def verify_user_groups(self) -> None:
        """Verify and log the groups the current user belongs to."""
        user = getpass.getuser()
        user_groups = ', '.join([g.gr_name for g in grp.getgrall() if user in g.gr_mem])
        self.log(logging.INFO, f"User groups: {user_groups}")
 
    def resource_assessment(self) -> Tuple[int, int, int]:
        try:
            with open('/proc/meminfo') as f:
                meminfo = {i.split()[0].rstrip(':'): int(i.split()[1]) for i in f.readlines()}

            available_ram_mb = meminfo['MemAvailable'] // 1024
            available_cpus = os.cpu_count()
            statvfs = os.statvfs('/var/lib/libvirt/images/')
            available_disk_gb = (statvfs.f_frsize * statvfs.f_bavail) // (1024 * 1024 * 1024)

        except Exception as e:
            self.fail(f"Failed to assess system resources. Error: {e}")

        return available_ram_mb, available_cpus, available_disk_gb

    def auto_or_manual_config(self) -> str:
        """Prompt the user to decide between automatic or manual resource allocation."""
        return input("Do you want to automatically allocate resources? (y/n): ")
 
    def auto_allocation(self, available_ram_mb: int, available_cpus: int, available_disk_gb: int) -> Tuple[int, int, int]:
        """Automatically allocate system resources based on availability."""
        try:
            self.log(logging.INFO, "Automatically allocating resources...")
 
            allocated_ram = available_ram_mb // 2
            allocated_cpus = available_cpus // 2
            allocated_disk = available_disk_gb // 2
 
            self.log(logging.INFO, f"Allocated RAM: {allocated_ram}MB")
            self.log(logging.INFO, f"Allocated CPU cores: {allocated_cpus}")
            self.log(logging.INFO, f"Allocated Disk Space: {allocated_disk}GB")
        except Exception as e:
            self.fail(f"Failed to automatically allocate resources. Error: {e}")
 
        return allocated_ram, allocated_cpus, allocated_disk
 
    def manual_allocation(self, available_ram_mb: int, available_cpus: int, available_disk_gb: int) -> Tuple[int, int, int]:
        """Manually allocate system resources based on user input."""
        try:
            allocated_ram = int(input(f"Enter the amount of RAM to allocate (suggested: {available_ram_mb // 2}MB): "))
            allocated_cpus = int(input(f"Enter the number of CPU cores to allocate (suggested: {available_cpus // 2}): "))
            allocated_disk = int(input(f"Enter the amount of disk space to allocate (suggested: {available_disk_gb // 2}GB): "))
 
            if allocated_ram > available_ram_mb or allocated_cpus > available_cpus or allocated_disk > available_disk_gb:
                self.fail("Invalid resource allocation.")
        except Exception as e:
            self.fail(f"Failed to manually allocate resources. Error: {e}")
        return allocated_ram, allocated_cpus, allocated_disk
 
    def get_uefi_path(self) -> str:
        """Get the UEFI firmware path based on the Linux distribution."""
        DISTRO_NAME = re.search(r'^ID=(.*)$', open('/etc/os-release').read(), re.MULTILINE).group(1)
        if DISTRO_NAME in ["ubuntu", "pop", "debian", "linuxmint"]:
            return "/usr/share/OVMF/OVMF_CODE_4M.fd"
        else:
            self.fail("Unsupported Linux distribution for UEFI firmware.")
 
    def get_cpu_topology(self) -> Tuple[int, int, int]:
        """Get CPU topology information from the system."""
        lscpu_output = subprocess.check_output(["lscpu"]).decode()
        threads_per_core = int(re.search(r'Thread\(s\) per core:\s*(\d+)', lscpu_output).group(1))
        sockets = int(re.search(r'Socket\(s\):\s*(\d+)', lscpu_output).group(1))
        numa_nodes = int(re.search(r'NUMA node\(s\):\s*(\d+)', lscpu_output).group(1))
        return threads_per_core, sockets, numa_nodes
 
    def validate_allocation(self, allocated_ram: int, allocated_cpus: int, allocated_disk: int,
                            available_ram_mb: int, available_cpus: int, available_disk_gb: int) -> None:
        """Validate the resource allocation."""
        if allocated_ram > available_ram_mb or allocated_cpus > available_cpus or allocated_disk > available_disk_gb:
            self.fail("Invalid resource allocation.")
 
    def validate_uefi_path(self) -> None:
        """Validate the UEFI path based on the Linux distribution."""
        UEFI_PATH = self.get_uefi_path()
        if not os.path.isfile(UEFI_PATH):
            self.fail(f"UEFI firmware not found at specified path: {UEFI_PATH}")
 
    def allocate_resources(self) -> Tuple[int, int, int]:
        """Allocate system resources for the VM."""
        available_ram_mb, available_cpus, available_disk_gb = self.resource_assessment()
        auto_allocate = self.auto_or_manual_config()
 
        if auto_allocate.lower() == 'y':
            return self.auto_allocation(available_ram_mb, available_cpus, available_disk_gb)
        else:
            return self.manual_allocation(available_ram_mb, available_cpus, available_disk_gb)
 
    def validate_resource_allocation(self, allocated_ram: int, allocated_cpus: int, allocated_disk: int) -> None:
        """Validate the resource allocation."""
        available_ram_mb, available_cpus, available_disk_gb = self.resource_assessment()
        if allocated_ram > available_ram_mb or allocated_cpus > available_cpus or allocated_disk > available_disk_gb:
            self.fail("Invalid resource allocation.")
 
    def create_vm(self, vm_name: str, iso_path: str) -> None:
        """Create a new VM with the specified configurations."""
        self.log(logging.INFO, f"Attempting to create VM with name: {vm_name}, iso_path: {iso_path}")
 
        UEFI_PATH = self.get_uefi_path()
        if not os.path.isfile(UEFI_PATH):
            self.fail(f"UEFI firmware not found at specified path: {UEFI_PATH}")
 
        threads_per_core, sockets, numa_nodes = self.get_cpu_topology()
 
        available_ram_mb, available_cpus, available_disk_gb = self.resource_assessment()
        auto_allocate = self.auto_or_manual_config()
 
        if auto_allocate.lower() == 'y':
            allocated_ram, allocated_cpus, allocated_disk = self.auto_allocation(available_ram_mb, available_cpus, available_disk_gb)
        else:
            allocated_ram, allocated_cpus, allocated_disk = self.manual_allocation(available_ram_mb, available_cpus, available_disk_gb)
 
        self.validate_allocation(allocated_ram, allocated_cpus, allocated_disk, available_ram_mb, available_cpus, available_disk_gb)
 
        virt_install_cmd = [
            "sudo", "virt-install",
            "--name", vm_name,
            "--ram", str(allocated_ram),
            "--vcpus", str(allocated_cpus),
            "--cpu", f"host,topology.sockets={sockets},topology.cores={allocated_cpus // threads_per_core // sockets},topology.threads={threads_per_core}",
            "--os-type", "windows",
            "--os-variant", "win10",
            "--network", "network=default",
            "--graphics", "spice",
            "--cdrom", iso_path,
            "--disk", f"path=/var/lib/libvirt/images/{vm_name}.img,size={allocated_disk},bus=scsi,format=qcow2,cache=writeback,discard=unmap",
            "--controller", "type=scsi,model=virtio-scsi",
            "--machine", "type=pc-q35-6.2", 
            "--boot", f"uefi={UEFI_PATH},cdrom,hd",
            "--memballoon", "model=virtio"
        ]
 
        self.run_subprocess(virt_install_cmd, "Failed to create the VM.")
        self.log(logging.INFO, f"Successfully created VM with name: {vm_name}")
 
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stage1 = Stage1()
    stage1.main()
