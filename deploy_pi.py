                                                    #!/usr/bin/env python3
import os
import subprocess
import sys
import shutil
import platform

# --- Configuration ---
REPO_URL = "https://github.com/robertchristophersmith/reactor-controller.git"
APP_NAME = "reactor-controller"
INSTALL_ROOT = os.path.expanduser("~")
INSTALL_DIR = os.path.join(INSTALL_ROOT, APP_NAME)
REQUIREMENTS_PATH = "supervisory/requirements.txt"
VENV_NAME = "venv"

def print_step(msg):
    print(f"\n\033[1;36m[DEPLOY] {msg}\033[0m")

def print_success(msg):
    print(f"\033[1;32m[SUCCESS] {msg}\033[0m")

def print_error(msg):
    print(f"\033[1;31m[ERROR] {msg}\033[0m")

def run_command(cmd, cwd=None, shell=False, sudo=False):
    """Run a shell command."""
    if sudo:
        if isinstance(cmd, list):
            cmd = ["sudo"] + cmd
        else:
            cmd = "sudo " + cmd
            
    try:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        print(f"Running: {cmd_str}")
        subprocess.check_call(cmd, cwd=cwd, shell=shell)
    except subprocess.CalledProcessError as e:
        print_error(f"Command failed: {e}")
        sys.exit(1)

def is_installed(package):
    """Check if a system package is installed."""
    return shutil.which(package) is not None

def main():
    if platform.system() != "Linux":
        print("WARNING: This script is designed for Linux (Raspberry Pi).")
        print("Proceeding anyway, but some steps may fail.\n")

    # 1. Install System Dependencies (Git, Python Venv)
    print_step("Checking system dependencies...")
    
    deps_to_install = []
    if not is_installed("git"):
        deps_to_install.append("git")
    
    # We always ensure python3-venv and python3-pip are present on Pi
    # Usually easier to just try installing them to be safe
    deps_to_install.extend(["python3-venv", "python3-pip"])

    if deps_to_install:
        print(f"Installing system packages: {', '.join(deps_to_install)}")
        run_command(["apt-get", "update"], sudo=True)
        run_command(["apt-get", "install", "-y"] + deps_to_install, sudo=True)
    else:
        print("Basic dependencies found.")

    # 2. Clone or Update Repository
    print_step(f"Setting up repository in {INSTALL_DIR}...")
    
    if os.path.exists(INSTALL_DIR):
        print("Directory exists. Pulling latest changes...")
        # Check if it is a git repo
        if os.path.exists(os.path.join(INSTALL_DIR, ".git")):
            run_command(["git", "pull"], cwd=INSTALL_DIR)
        else:
            print_error(f"{INSTALL_DIR} exists but is not a git repository.")
            print("Please back up and remove it, then run this script again.")
            sys.exit(1)
    else:
        print("Cloning repository...")
        run_command(["git", "clone", REPO_URL, INSTALL_DIR])

    # 3. Setup Virtual Environment
    print_step("Setting up Python virtual environment...")
    
    venv_path = os.path.join(INSTALL_DIR, VENV_NAME)
    if not os.path.exists(venv_path):
        run_command([sys.executable, "-m", "venv", VENV_NAME], cwd=INSTALL_DIR)
        print("Virtual environment created.")
    else:
        print("Virtual environment already exists.")

    # 4. Install Python Requirements
    print_step("Installing Python dependencies...")
    
    pip_exe = os.path.join(venv_path, "bin", "pip")
    req_file_abs = os.path.join(INSTALL_DIR, REQUIREMENTS_PATH)
    
    if os.path.exists(req_file_abs):
        run_command([pip_exe, "install", "--upgrade", "pip"], cwd=INSTALL_DIR)
        run_command([pip_exe, "install", "-r", REQUIREMENTS_PATH], cwd=INSTALL_DIR)
    else:
        print_error(f"Requirements file not found at {REQUIREMENTS_PATH}")

    # 5. Permission Setup (Dialout for Serial)
    print_step("Configuring permissions...")
    user = os.environ.get("USER")
    if user:
        print(f"Adding user '{user}' to dialout group for serial access...")
        try:
            run_command(["usermod", "-a", "-G", "dialout", user], sudo=True)
        except Exception as e:
            print(f"Could not add user to dialout group: {e}")
            
    # 6. Configure UDEV rules for dual Arduinos
    print_step("Configuring UDEV rules for Arduino predictability...")
    udev_rules = (
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0042", SYMLINK+="sensor_arduino"\n'
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0043", SYMLINK+="pump_arduino"\n'
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="pump_arduino"\n'
    )
    try:
        rule_path = "/tmp/99-arduinos.rules"
        with open(rule_path, "w") as f:
            f.write(udev_rules)
        run_command(["cp", rule_path, "/etc/udev/rules.d/99-arduinos.rules"], sudo=True)
        run_command(["udevadm", "control", "--reload-rules"], sudo=True)
        run_command(["udevadm", "trigger"], sudo=True)
        print("UDEV rules applied.")
    except Exception as e:
        print(f"Could not configure UDEV rules: {e}")
    
    # 6. Final Instructions
    print_step("Installation Complete!")
    print_success("The Reactor Controller has been successfully deployed.")
    print("\nTo run the application:")
    print(f"  cd {INSTALL_DIR}")
    print(f"  {os.path.join(VENV_NAME, 'bin', 'uvicorn')} supervisory.main:app --host 0.0.0.0 --port 8000")
    print("\nOr manually activate venv:")
    print(f"  source {os.path.join(venv_path, 'bin', 'activate')}")

if __name__ == "__main__":
    main()
