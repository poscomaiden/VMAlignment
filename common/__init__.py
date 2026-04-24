"""Common shared code for VM Alignment system."""

__version__ = "1.0.0"

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


CONFIG_DIR = Path.home() / ".vm_alignment"
CONFIG_FILE = CONFIG_DIR / "client_config.json"


def load_client_config() -> dict:
    """Load client configuration from disk."""
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_client_config(config: dict):
    """Save client configuration to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def build_deploy_message(repo: str, branch: str, commit_sha: str, commit_message: str, author: str, tag: Optional[str] = None) -> dict:
    """Build a deployment message payload."""
    return {
        "repo": repo,
        "branch": branch,
        "commit_sha": commit_sha[:8] if commit_sha else "",
        "commit_message": commit_message,
        "author": author,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag": tag
    }


def parse_github_payload(repo_name: str, payload: dict, headers: dict) -> dict:
    """Parse a GitHub webhook payload into a deployment message."""
    ref = payload.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
    
    commit = payload.get("head_commit", {})
    author = commit.get("author", {}).get("name", payload.get("pusher", {}).get("name", "unknown"))
    
    # Check for version tag
    tag = None
    if ref.startswith("refs/tags/"):
        tag = ref.replace("refs/tags/", "")
    
    return build_deploy_message(
        repo=repo_name,
        branch=branch,
        commit_sha=payload.get("after", ""),
        commit_message=commit.get("message", ""),
        author=author,
        tag=tag
    )


class VMLogging:
    """Simple logging to file."""
    
    def __init__(self, name: str, log_dir: Optional[Path] = None):
        self.name = name
        self.log_dir = log_dir or CONFIG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{name}.log"
    
    def log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level}] {msg}\n"
        with open(self.log_file, "a") as f:
            f.write(line)
    
    def info(self, msg: str):
        self.log("INFO", msg)
    
    def warning(self, msg: str):
        self.log("WARNING", msg)
    
    def error(self, msg: str):
        self.log("ERROR", msg)
    
    def debug(self, msg: str):
        self.log("DEBUG", msg)
