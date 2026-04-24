"""
VM Alignment Client Setup Wizard
Interactive TUI installer that:
  1. Asks for Coordinator IP/URL
  2. Asks for RabbitMQ host (defaults to Coordinator IP)
  3. Asks for VM name and ID
  4. Optionally adds repos (local paths to watch)
  5. Writes config and optionally installs the Windows service

Can run standalone (python setup_wizard.py) or as a frozen EXE (PyInstaller).
"""

import os
import sys
import json
import uuid
import socket
import subprocess
from pathlib import Path

try:
    import win32serviceutil
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".vm_alignment"
CONFIG_FILE = CONFIG_DIR / "client_config.json"


def _print(msg="", color=None):
    """Print with basic ANSI color support in cmd/powershell."""
    colors = {
        "cyan": "\033[96m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "red": "\033[91m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }
    c = colors.get(color, "") if color else ""
    r = colors["reset"]
    print(f"{c}{msg}{r}")


def _input(prompt, default=""):
    """Ask user input, showing default in brackets."""
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def _validate_url(url):
    """Basic URL/host:port validation."""
    if not url:
        return False
    if url.startswith("http://") or url.startswith("https://"):
        return True
    # bare host or host:port
    try:
        if ":" in url:
            host, port = url.rsplit(":", 1)
            int(port)
        else:
            host = url
        socket.gethostbyname(host)
        return True
    except Exception:
        return False


def _detect_rabbitmq_host(coordinator_url):
    """Try to extract RabbitMQ host from the coordinator URL for defaults."""
    # Strip trailing parts
    url = coordinator_url.rstrip("/")
    if url.startswith("http://"):
        host = url[7:]
    elif url.startswith("https://"):
        host = url[8:]
    else:
        host = url
    # Remove any path
    if "/" in host:
        host = host.split("/")[0]
    return host


def _test_connection(url, path="/api/health", timeout=5):
    """Test HTTP connectivity to coordinator."""
    import urllib.request
    import urllib.error
    try:
        target = f"{url.rstrip('/')}{path}"
        req = urllib.request.Request(target)
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def _discover_local_repos():
    """Scan common locations for git repos."""
    repos = []
    # Check common development folders
    candidates = [
        Path.home() / "Dev",
        Path.home() / "Projects",
        Path.home() / "Documents",
        Path.home() / "Desktop",
        Path("C:/"),
    ]
    for base in candidates:
        if not base.exists():
            continue
        try:
            for item in base.iterdir():
                if item.is_dir() and (item / ".git").exists():
                    repos.append({
                        "name": item.name,
                        "path": str(item),
                        "enabled": True
                    })
        except PermissionError:
            pass
    return repos


def _add_repo_interactive():
    """Ask user for a single repo path."""
    print()
    path = _input("  Local repo path (e.g. C:\\Dev\\MyProject)", default="")
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        _print(f"  Path does not exist: {path}", "red")
        return None
    if not (p / ".git").exists():
        _print(f"  Not a git repo (no .git folder found)", "yellow")
        confirm = _input("  Add anyway?", default="n")
        if confirm.lower() != "y":
            return None
    
    name = _input("  Repo short name", default=p.name)
    return {"name": name, "path": str(p), "enabled": True}


def _install_service():
    """Install the Windows service using pywin32."""
    if not HAS_WIN32:
        _print("  pywin32 not installed — skipping service install", "yellow")
        _print("  Run: pip install pywin32", "yellow")
        return False
    
    service_py = BASE_DIR / "client" / "service.py"
    if not service_py.exists():
        _print(f"  service.py not found at {service_py}", "red")
        return False
    
    try:
        subprocess.run(
            [sys.executable, str(service_py), "install"],
            check=True, capture_output=True
        )
        subprocess.run(
            [sys.executable, str(service_py), "start"],
            check=True, capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        _print(f"  Service install failed: {e.stderr.decode() if e.stderr else str(e)}", "red")
        return False


def run_wizard():
    _print()
    _print("=" * 55, "cyan")
    _print("  VM Alignment Client Setup Wizard", "bold cyan")
    _print("=" * 55, "cyan")
    _print()
    _print("This wizard will help you configure the VM Alignment", "yellow")
    _print("Client and optionally install it as a Windows service.", "yellow")
    _print()

    # ── Step 1: Coordinator URL ─────────────────────────────────
    _print("STEP 1: Coordinator Connection", "bold")
    _print("-" * 35)
    coordinator_url = ""
    for attempt in range(3):
        raw = _input("Coordinator IP or URL", default="http://192.168.1.100:5000")
        # Normalize
        if raw and not raw.startswith("http"):
            raw = "http://" + raw
        if _validate_url(raw):
            coordinator_url = raw.rstrip("/")
            break
        _print(f"  Could not resolve: {raw}", "red")
        _print("  (Must be an IP/hostname reachable from this machine)", "yellow")
    if not coordinator_url:
        _print("No valid coordinator URL after 3 attempts. Aborting.", "red")
        return

    # Test connection
    _print("  Testing connection...", "yellow", end="")
    if _test_connection(coordinator_url):
        _print(" OK", "green")
    else:
        _print(" UNREACHABLE — client may not start until coordinator is reachable", "yellow")
    
    # Extract RabbitMQ default from coordinator URL
    rabbitmq_host = _detect_rabbitmq_host(coordinator_url)
    _print(f"  Derived RabbitMQ host: {rabbitmq_host}", "yellow")

    print()
    _print("STEP 2: RabbitMQ Connection", "bold")
    _print("-" * 35)
    rabbitmq_host = _input("RabbitMQ host", default=rabbitmq_host)
    rabbitmq_port = _input("RabbitMQ port", default="5672")
    rabbitmq_user = _input("RabbitMQ username", default="guest")
    rabbitmq_password = _input("RabbitMQ password", default="guest")

    print()
    _print("STEP 3: VM Identity", "bold")
    _print("-" * 35)
    vm_name = _input("VM display name", default=socket.gethostname())
    # Generate a stable VM ID from hostname + random suffix
    vm_id = _input("VM unique ID", default=f"vm-{socket.gethostname().lower()[:8]}-{uuid.uuid4().hex[:4]}")
    health_interval = _input("Heartbeat interval (seconds)", default="30")

    print()
    _print("STEP 4: Repo Configuration", "bold")
    _print("-" * 35)
    _print("Add the local git repos this VM should keep updated.", "yellow")
    _print("(Leave path empty to skip)", "yellow")
    
    repos = []
    # Offer discovered repos
    discovered = _discover_local_repos()
    if discovered:
        print()
        _print("Discovered git repos on this machine:", "green")
        for i, r in enumerate(discovered):
            _print(f"  [{i+1}] {r['name']} — {r['path']}")
        use_discovery = _input("Add all discovered repos?", default="y")
        if use_discovery.lower() == "y":
            repos.extend(discovered)
    
    print()
    while True:
        repo = _add_repo_interactive()
        if repo is None:
            break
        repos.append(repo)
        more = _input("Add another repo?", default="n")
        if more.lower() != "y":
            break

    print()
    _print("STEP 5: Windows Service", "bold")
    _print("-" * 35)
    if not HAS_WIN32:
        _print("pywin32 not detected — will configure standalone only", "yellow")
        _print("Install with: pip install pywin32", "yellow")
    else:
        install_service = _input("Install as Windows service?", default="y")
        do_install = install_service.lower() == "y"
    print()

    # ── Build config ──────────────────────────────────────────────
    config = {
        "coordinator_url": coordinator_url,
        "rabbitmq_host": rabbitmq_host,
        "rabbitmq_port": int(rabbitmq_port),
        "rabbitmq_user": rabbitmq_user,
        "rabbitmq_password": rabbitmq_password,
        "vm_id": vm_id,
        "vm_name": vm_name,
        "health_interval_seconds": int(health_interval),
        "repos": repos,
    }

    # Ensure config dir exists and write
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

    _print(f"Config written to: {CONFIG_FILE}", "green")
    _print()
    _print("Configuration:", "bold")
    print(json.dumps(config, indent=2))
    print()

    # ── Install service ────────────────────────────────────────────
    if HAS_WIN32 and do_install:
        _print("Installing Windows service...", "yellow")
        if _install_service():
            _print("Service installed and started!", "green")
        else:
            _print("Service install failed. See errors above.", "red")
            _print("You can run manually with:", "yellow")
            _print(f"  python {BASE_DIR}/client/client_service.py", "yellow")
    else:
        _print("To start manually, run:", "yellow")
        _print(f"  python {BASE_DIR}/client/client_service.py", "yellow")
    
    print()
    _print("=" * 55, "cyan")
    _print("Setup complete!", "bold green")
    _print("=" * 55, "cyan")
    print()


if __name__ == "__main__":
    run_wizard()
