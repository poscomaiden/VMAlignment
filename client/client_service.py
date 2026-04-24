"""
VM Alignment Client Service
Subscribes to RabbitMQ queues and runs git pull on matching repos.
Runs as a Windows service or standalone.
"""

import os
import sys
import json
import time
import subprocess
import threading
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import pika
    from pika.exceptions import AMQPConnectionError, AMQPChannelError
    HAS_PIKA = True
except ImportError:
    HAS_PIKA = False

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from common import load_client_config, save_client_config, build_deploy_message, VMLogging


__version__ = "1.0.0"
CONFIG_DIR = Path.home() / ".vm_alignment"
STATE_FILE = CONFIG_DIR / "client_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_update": None, "updates": []}


def save_state(state: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


class VMAlignmentClient:
    def __init__(self, config_path: str = None):
        self.log = VMLogging("vm_alignment_client")
        self.config = load_client_config()
        if config_path:
            with open(config_path) as f:
                self.config.update(json.load(f))
        
        self.vm_id = self.config.get("vm_id", "unknown")
        self.vm_name = self.config.get("vm_name", self.vm_id)
        self.coordinator_url = self.config.get("coordinator_url", "http://localhost:5000")
        self.rabbitmq_host = self.config.get("rabbitmq_host", "localhost")
        self.rabbitmq_port = self.config.get("rabbitmq_port", 5672)
        self.rabbitmq_user = self.config.get("rabbitmq_user", "guest")
        self.rabbitmq_password = self.config.get("rabbitmq_password", "guest")
        self.repos = self.config.get("repos", [])
        self.health_interval = self.config.get("health_interval_seconds", 30)
        self.config_report_interval = self.config.get("config_report_interval_seconds", 300)
        
        self._connection = None
        self._channel = None
        self._should_stop = False
        self._heartbeat_thread = None
        self._config_thread = None

    def save_config(self):
        """Save current config to disk (for applying pushed config changes)."""
        save_client_config(self.config)

    def apply_config_update(self, new_config: dict) -> bool:
        """Apply a config pushed from the coordinator."""
        if not new_config:
            return False
        
        old_config = self.config.copy()
        # Merge in pushed values
        for key in ["vm_name", "repos", "health_interval_seconds", "rabbitmq_host",
                    "rabbitmq_port", "rabbitmq_user", "rabbitmq_password"]:
            if key in new_config:
                self.config[key] = new_config[key]
        
        # If repos were updated, reload them
        if "repos" in new_config:
            self.repos = self.config["repos"] = new_config["repos"]
        
        self.save_config()
        self.log.info(f"Applied config update from coordinator: {list(new_config.keys())}")
        
        # Record in state
        state = load_state()
        state.setdefault("config_updates", []).insert(0, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "keys": list(new_config.keys()),
            "old": old_config,
            "new": self.config
        })
        state["config_updates"] = state["config_updates"][:20]
        save_state(state)
        return True

    def git_pull(self, repo_path: str) -> tuple[bool, str]:
        """Run git pull on the given repo path."""
        if not Path(repo_path).exists():
            return False, f"Path does not exist: {repo_path}"
        
        try:
            result = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                return True, result.stdout.strip() or "Pull successful"
            else:
                return False, result.stderr.strip() or f"Git pull failed (code {result.returncode})"
        except subprocess.TimeoutExpired:
            return False, "Git pull timed out"
        except FileNotFoundError:
            return False, "Git not found in PATH"
        except Exception as e:
            return False, str(e)

    def handle_message(self, body: bytes) -> bool:
        """Process a deployment message from RabbitMQ."""
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            self.log.error("Invalid JSON message received")
            return False

        repo_name = msg.get("repo", "")
        commit_sha = msg.get("commit_sha", "")
        commit_msg = msg.get("commit_message", "")
        author = msg.get("author", "")
        timestamp = msg.get("timestamp", "")

        self.log.info(f"Received deploy for repo='{repo_name}' commit={commit_sha}")

        # Find matching repo config
        repo_cfg = next((r for r in self.repos if r.get("name") == repo_name and r.get("enabled", True)), None)
        if not repo_cfg:
            self.log.info(f"Repo '{repo_name}' not configured or disabled, skipping")
            return True  # Ack anyway

        repo_path = repo_cfg.get("path")
        if not repo_path:
            self.log.error(f"No path configured for repo '{repo_name}'")
            return True

        success, output = self.git_pull(repo_path)
        
        if success:
            self.log.info(f"Updated {repo_name} at {repo_path}: {output}")
            # Record update
            state = load_state()
            state["last_update"] = datetime.now(timezone.utc).isoformat()
            state["updates"].insert(0, {
                "repo": repo_name,
                "commit_sha": commit_sha,
                "commit_message": commit_msg,
                "author": author,
                "timestamp": timestamp,
                "status": "success"
            })
            state["updates"] = state["updates"][:50]
            save_state(state)
        else:
            self.log.error(f"Failed to update {repo_name}: {output}")
            state = load_state()
            state["updates"].insert(0, {
                "repo": repo_name,
                "commit_sha": commit_sha,
                "status": "failed",
                "error": output
            })
            state["updates"] = state["updates"][:50]
            save_state(state)

        return success

    def on_message(self, channel, method, properties, body):
        """RabbitMQ message callback."""
        try:
            ok = self.handle_message(body)
            if ok:
                channel.basic_ack(delivery_tag=method.delivery_tag)
            else:
                # Requeue on failure
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
        except Exception as e:
            self.log.error(f"Error processing message: {e}")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def connect_rabbitmq(self):
        """Connect to RabbitMQ and subscribe to repo queues."""
        credentials = pika.PlainCredentials(self.rabbitmq_user, self.rabbitmq_password)
        params = pika.ConnectionParameters(
            host=self.rabbitmq_host,
            port=self.rabbitmq_port,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300
        )
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._channel.exchange_declare(exchange="deployments", exchange_type="topic", durable=True)
        
        # Subscribe to queues for each enabled repo
        for repo in self.repos:
            if repo.get("enabled", True):
                queue_name = f"{repo['name']}.queue"
                self._channel.queue_declare(queue=queue_name, durable=True)
                self._channel.queue_bind(
                    exchange="deployments",
                    queue=queue_name,
                    routing_key=f"{repo['name']}.push"
                )
                self._channel.basic_consume(queue=queue_name, on_message_callback=self.on_message)
                self.log.info(f"Subscribed to queue: {queue_name}")

    def send_heartbeat(self):
        """Send heartbeat to coordinator dashboard."""
        try:
            state = load_state()
            payload = {
                "name": self.vm_name,
                "repos": [r["name"] for r in self.repos if r.get("enabled", True)],
                "hostname": os.environ.get("COMPUTERNAME", ""),
                "last_update": state.get("last_update"),
                "coordinator_url": self.coordinator_url,
            }
            resp = requests.post(
                f"{self.coordinator_url}/api/vms/{self.vm_id}",
                json=payload,
                timeout=5
            )
            if resp.status_code in (200, 201):
                self.log.debug(f"Heartbeat sent: {self.vm_id}")
            else:
                self.log.warning(f"Heartbeat failed: {resp.status_code}")
        except requests.RequestException as e:
            self.log.debug(f"Heartbeat error: {e}")

    def check_pending_config(self):
        """Poll coordinator for any pending config updates and apply them."""
        try:
            resp = requests.get(
                f"{self.coordinator_url}/api/vms/{self.vm_id}/pending-config",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("has_config"):
                    cfg = data.get("config", {})
                    self.apply_config_update(cfg)
                    self.log.info("Received and applied config push from coordinator")
        except requests.RequestException as e:
            self.log.debug(f"Config poll error: {e}")

    def report_config_to_coordinator(self):
        """Report current client config back to coordinator for visibility."""
        try:
            payload = {
                "coordinator_url": self.coordinator_url,
                "rabbitmq_host": self.rabbitmq_host,
                "rabbitmq_port": self.rabbitmq_port,
                "repos": self.repos,
                "health_interval_seconds": self.health_interval,
                "version": __version__,
            }
            requests.post(
                f"{self.coordinator_url}/api/vms/{self.vm_id}/report-config",
                json=payload,
                timeout=5
            )
        except requests.RequestException as e:
            self.log.debug(f"Config report error: {e}")

    def config_loop(self):
        """Periodically check for pushed config and report config to coordinator."""
        while not self._should_stop:
            time.sleep(self.config_report_interval)
            if self._should_stop:
                break
            try:
                self.check_pending_config()
                self.report_config_to_coordinator()
            except Exception as e:
                self.log.debug(f"Config loop error: {e}")

    def heartbeat_loop(self):
        """Periodically send heartbeats."""
        while not self._should_stop:
            time.sleep(self.health_interval)
            if self._should_stop:
                break
            try:
                self.send_heartbeat()
            except Exception as e:
                self.log.debug(f"Heartbeat loop error: {e}")

    def run(self):
        """Main loop — connect and consume."""
        if not HAS_PIKA:
            self.log.error("pika library not installed. Run: pip install pika requests")
            return

        self.log.info(f"Starting VM Alignment Client v{__version__}")
        self.log.info(f"VM ID: {self.vm_id}, Name: {self.vm_name}")
        self.log.info(f"Coordinator: {self.coordinator_url}, RabbitMQ: {self.rabbitmq_host}:{self.rabbitmq_port}")

        # Start heartbeat thread
        self._heartbeat_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        # Start config sync thread (polls for pushed config, reports current config)
        self._config_thread = threading.Thread(target=self.config_loop, daemon=True)
        self._config_thread.start()

        # Do initial config report
        self.report_config_to_coordinator()

        while not self._should_stop:
            try:
                self.connect_rabbitmq()
                self.log.info("Connected to RabbitMQ, waiting for messages...")
                self._channel.start_consuming()
            except AMQPConnectionError as e:
                self.log.warning(f"RabbitMQ connection lost: {e}. Reconnecting in 10s...")
                time.sleep(10)
            except KeyboardInterrupt:
                self.log.info("Interrupted by user")
                break
            except Exception as e:
                self.log.error(f"Unexpected error: {e}")
                time.sleep(10)

    def stop(self):
        """Stop the client."""
        self._should_stop = True
        if self._channel:
            try:
                self._channel.stop_consuming()
            except Exception:
                pass
        if self._connection and self._connection.is_open:
            try:
                self._connection.close()
            except Exception:
                pass
        self.log.info("Client stopped")


def setup_default_config():
    """Create a default client config if none exists."""
    config = load_client_config()
    if not config:
        config = {
            "coordinator_url": "http://localhost:5000",
            "rabbitmq_host": "localhost",
            "rabbitmq_port": 5672,
            "rabbitmq_user": "guest",
            "rabbitmq_password": "guest",
            "vm_id": "vm-001",
            "vm_name": "VM 1",
            "repos": [],
            "health_interval_seconds": 30
        }
        save_client_config(config)
    return config


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--configure":
        config = setup_default_config()
        print("Client config written to ~/.vm_alignment/client_config.json")
        print(json.dumps(config, indent=2))
        return

    client = VMAlignmentClient()
    try:
        client.run()
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()
