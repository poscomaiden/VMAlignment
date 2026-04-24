"""
VM Alignment Coordinator Server
Receives GitHub webhooks and publishes to RabbitMQ for distribution to VMs.
"""

__version__ = "1.0.0"

import os
import sys
import json
import hmac
import hashlib
import threading
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

# Add project root to path so 'common' and 'coordinator' can both import it
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pika
from pika.exceptions import AMQPConnectionError
from flask import Flask, request, jsonify, abort


# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = BASE_DIR / "coordinator" / "coordinator_config.json"
STATE_FILE = BASE_DIR / "coordinator" / "state.json"

DEFAULT_CONFIG = {
    "coordinator_host": "0.0.0.0",
    "coordinator_port": 5000,
    "rabbitmq_host": "localhost",
    "rabbitmq_port": 5672,
    "rabbitmq_user": "guest",
    "rabbitmq_password": "guest",
    "github_secret": "",
    "repos": []
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    else:
        cfg = DEFAULT_CONFIG.copy()
    # Ensure all default keys exist
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


def load_state() -> dict:
    if STATE_FILE.exists():
        if STATE_FILE.is_dir():
            # Corrupt state — a directory was created instead of a file
            import shutil
            shutil.rmtree(str(STATE_FILE))
            STATE_FILE.mkdir(parents=True)
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"repos": {}, "vms": {}, "activity": []}


def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# ─── RabbitMQ ─────────────────────────────────────────────────────────────────

class RabbitMQManager:
    EXCHANGE = "deployments"
    EXCHANGE_TYPE = "topic"

    def __init__(self, config: dict):
        self.config = config
        self._connection = None
        self._channel = None

    def _get_credentials(self):
        return pika.PlainCredentials(
            self.config["rabbitmq_user"],
            self.config["rabbitmq_password"]
        )

    def _get_connection_params(self):
        return pika.ConnectionParameters(
            host=self.config["rabbitmq_host"],
            port=self.config["rabbitmq_port"],
            credentials=self._get_credentials(),
            heartbeat=600,
            blocked_connection_timeout=300
        )

    def connect(self):
        if self._connection and self._connection.is_open:
            return
        self._connection = pika.BlockingConnection(self._get_connection_params())
        self._channel = self._connection.channel()
        self._channel.exchange_declare(
            exchange=self.EXCHANGE,
            exchange_type=self.EXCHANGE_TYPE,
            durable=True
        )
        # Declare queues for each configured repo
        for repo in self.config.get("repos", []):
            queue_name = f"{repo['name']}.queue"
            self._channel.queue_declare(queue=queue_name, durable=True)
            self._channel.queue_bind(
                exchange=self.EXCHANGE,
                queue=queue_name,
                routing_key=f"{repo['name']}.push"
            )

    def disconnect(self):
        if self._connection and self._connection.is_open:
            self._connection.close()

    def publish(self, routing_key: str, message: dict) -> bool:
        try:
            self.connect()
            self._channel.basic_publish(
                exchange=self.EXCHANGE,
                routing_key=routing_key,
                body=json.dumps(message),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # persistent
                    content_type="application/json"
                )
            )
            return True
        except AMQPConnectionError as e:
            app.logger.error(f"RabbitMQ publish error: {e}")
            return False

    def get_queue_info(self) -> dict:
        """Get queue depths for all repo queues."""
        try:
            self.connect()
            info = {}
            for repo in self.config.get("repos", []):
                queue_name = f"{repo['name']}.queue"
                try:
                    q = self._channel.queue_declare(queue=queue_name, passive=True)
                    info[repo['name']] = {
                        "queue": queue_name,
                        "messages": q.method.message_count,
                        "consumers": q.method.consumer_count
                    }
                except Exception:
                    info[repo['name']] = {
                        "queue": queue_name,
                        "messages": 0,
                        "consumers": 0,
                        "error": "queue not found"
                    }
            return info
        except Exception as e:
            return {"error": str(e)}

    def get_exchange_info(self) -> dict:
        try:
            self.connect()
            e = self._channel.exchange_declare(exchange=self.EXCHANGE, passive=True)
            return {"exchange": self.EXCHANGE, "type": e.method.exchange_type}
        except Exception:
            return {"exchange": self.EXCHANGE, "type": self.EXCHANGE_TYPE}


# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "vm-alignment-secret")
_config = load_config()
_state = load_state()
_rmq = RabbitMQManager(_config)
_state_lock = threading.Lock()


def verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    if not signature or not secret:
        return True  # Skip verification if no secret configured
    mac = hmac.new(secret.encode(), payload, hashlib.sha256)
    expected = f"sha256={mac.hexdigest()}"
    return hmac.compare_digest(expected, signature)


def record_activity(event_type: str, repo: str, detail: str):
    with _state_lock:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "repo": repo,
            "detail": detail
        }
        _state.setdefault("activity", []).insert(0, entry)
        _state["activity"] = _state["activity"][:100]  # keep last 100
        save_state(_state)


@app.route("/webhook/<repo_name>", methods=["POST"])
def webhook(repo_name):
    # Find repo config
    repo_cfg = next((r for r in _config["repos"] if r["name"] == repo_name), None)
    if not repo_cfg:
        abort(404, f"Repo '{repo_name}' not configured")

    # Verify signature
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    secret = _config.get("github_secret", "")
    if secret and not verify_github_signature(payload, signature, secret):
        abort(403, "Invalid signature")

    # Parse payload
    data = request.get_json()
    if not data:
        abort(400, "Invalid JSON")

    ref = data.get("ref", "")
    branch = ref.replace("refs/heads/", "") if ref.startswith("refs/heads/") else ref
    
    # Only process main branch pushes
    if branch != "main":
        return jsonify({"status": "ignored", "reason": f"branch '{branch}' not main"})

    commit = data.get("head_commit", {})
    author = commit.get("author", {}).get("name", data.get("pusher", {}).get("name", "unknown"))
    
    tag = None
    if ref.startswith("refs/tags/"):
        tag = ref.replace("refs/tags/", "")

    message = {
        "repo": repo_name,
        "branch": branch,
        "commit_sha": (data.get("after") or "")[:8],
        "commit_message": commit.get("message", ""),
        "author": author,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag": tag
    }

    routing_key = f"{repo_name}.push"
    success = _rmq.publish(routing_key, message)

    if success:
        # Update repo state
        with _state_lock:
            _state.setdefault("repos", {})[repo_name] = {
                "last_push": message["timestamp"],
                "last_commit_sha": message["commit_sha"],
                "last_commit_message": message["commit_message"],
                "last_author": author
            }
            save_state(_state)
        record_activity("push", repo_name, f"{author}: {message['commit_message'][:50]}")
        app.logger.info(f"Published deploy message for {repo_name}")
        return jsonify({"status": "ok", "routing_key": routing_key})
    else:
        record_activity("error", repo_name, "RabbitMQ publish failed")
        return jsonify({"status": "error", "message": "Failed to publish to queue"}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route("/api/state", methods=["GET"])
def get_state():
    with _state_lock:
        return jsonify({
            "repos": _state.get("repos", {}),
            "vms": _state.get("vms", {}),
            "activity": _state.get("activity", [])
        })


@app.route("/api/queues", methods=["GET"])
def get_queues():
    return jsonify(_rmq.get_queue_info())


@app.route("/api/config", methods=["GET"])
def get_config():
    # Don't expose github_secret
    cfg = {k: v for k, v in _config.items() if k != "github_secret"}
    return jsonify(cfg)


@app.route("/api/config/repos", methods=["POST"])
def add_repo():
    # Accept JSON or form-encoded data
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json()
    else:
        data = request.form.to_dict()
    if not data or "name" not in data or "repo_full_name" not in data:
        abort(400, "name and repo_full_name required")
    
    name = data["name"].lower().replace(" ", "-")
    if any(r["name"] == name for r in _config["repos"]):
        abort(409, f"Repo '{name}' already configured")
    
    new_repo = {
        "name": name,
        "display_name": data.get("display_name", name),
        "repo_full_name": data["repo_full_name"],
        "webhook_path": f"/webhook/{name}"
    }
    _config["repos"].append(new_repo)
    save_config(_config)
    
    # Reconnect RabbitMQ to declare new queue
    _rmq.disconnect()
    
    record_activity("repo_added", name, f"Added repo {data['repo_full_name']}")
    return jsonify({"status": "ok", "repo": new_repo}), 201


@app.route("/api/config/repos/<repo_name>", methods=["DELETE", "POST"])
def remove_repo(repo_name):
    # POST with _method=DELETE for browser form submissions
    if request.method == "POST" and request.form.get("_method") != "DELETE":
        abort(405)
    global _config
    idx = next((i for i, r in enumerate(_config["repos"]) if r["name"] == repo_name), None)
    if idx is None:
        abort(404, f"Repo '{repo_name}' not found")
    
    _config["repos"].pop(idx)
    save_config(_config)
    _rmq.disconnect()
    
    record_activity("repo_removed", repo_name, f"Removed repo")
    return jsonify({"status": "ok"})


@app.route("/api/vms", methods=["GET"])
def get_vms():
    with _state_lock:
        return jsonify(_state.get("vms", {}))


@app.route("/api/vms/<vm_id>", methods=["GET", "POST"])
def vm_endpoint(vm_id):
    """GET: Return VM info. POST: VMs call this to register/update their heartbeat."""
    global _state
    if request.method == "GET":
        with _state_lock:
            vm = _state.get("vms", {}).get(vm_id)
            if not vm:
                abort(404, f"VM '{vm_id}' not found")
            return jsonify(vm)
    
    # POST — register/update heartbeat
    data = request.get_json() or {}
    with _state_lock:
        vm = _state.setdefault("vms", {})[vm_id]
        vm["name"] = data.get("name", vm_id)
        vm["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
        vm["repos"] = data.get("repos", vm.get("repos", []))
        vm["hostname"] = data.get("hostname", vm.get("hostname", ""))
        vm["ip"] = request.remote_addr
        vm["coordinator_url"] = data.get("coordinator_url", "")
        save_state(_state)
    return jsonify({"status": "ok"})


@app.route("/api/vms/<vm_id>/config", methods=["GET"])
def get_vm_config(vm_id):
    """Return the stored config for a specific VM (for bi-directional sync)."""
    with _state_lock:
        vm = _state.get("vms", {}).get(vm_id)
        if not vm:
            abort(404, f"VM '{vm_id}' not found")
        # Return all config fields the coordinator tracks
        return jsonify({
            "vm_id": vm_id,
            "name": vm.get("name", ""),
            "repos": vm.get("repos", []),
            "coordinator_url": vm.get("coordinator_url", ""),
            "hostname": vm.get("hostname", ""),
        })


@app.route("/api/vms/<vm_id>/config", methods=["PUT", "POST"])
def push_vm_config(vm_id):
    """Coordinator (or dashboard) pushes config TO a VM via the client's report endpoint."""
    data = request.get_json() or {}
    with _state_lock:
        vm = _state.get("vms", {}).get(vm_id)
        if not vm:
            abort(404, f"VM '{vm_id}' not found")
    
    # The coordinator stores the desired config; the client will poll/receive this
    # We store it as pending_config so the client can fetch it
    with _state_lock:
        _state["vms"][vm_id]["pending_config"] = data
        _state["vms"][vm_id]["config_timestamp"] = datetime.now(timezone.utc).isoformat()
        save_state(_state)
    
    record_activity("config_push", vm_id, f"Config update queued for VM: {list(data.keys())}")
    return jsonify({"status": "ok", "message": "Config queued for VM"})


@app.route("/api/vms/<vm_id>/pending-config", methods=["GET"])
def get_pending_config(vm_id):
    """Client polls this to check if coordinator has config changes for it."""
    with _state_lock:
        vm = _state.get("vms", {}).get(vm_id)
        if not vm:
            abort(404, f"VM '{vm_id}' not found")
        pending = vm.get("pending_config")
        if pending:
            # Clear it so next poll doesn't re-deliver the same config
            del _state["vms"][vm_id]["pending_config"]
            save_state(_state)
            return jsonify({"has_config": True, "config": pending})
        return jsonify({"has_config": False})


@app.route("/api/vms/<vm_id>/report-config", methods=["POST"])
def report_vm_config(vm_id):
    """Client reports its current config BACK to the coordinator (bi-directional sync)."""
    data = request.get_json() or {}
    with _state_lock:
        vm = _state.get("vms", {}).get(vm_id)
        if not vm:
            # Auto-register if VM hasn't been seen
            vm = _state.setdefault("vms", {})[vm_id]
        vm["reported_config"] = data
        vm["last_config_report"] = datetime.now(timezone.utc).isoformat()
        save_state(_state)
    
    record_activity("config_report", vm_id, f"VM reported config: {list(data.keys())}")
    return jsonify({"status": "ok"})


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>VM Alignment — Coordinator</title>
    <meta http-equiv="refresh" content="10">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        h1 { color: #58a6ff; margin-bottom: 5px; }
        .subtitle { color: #8b949e; margin-bottom: 30px; }
        h2 { color: #c9d1d9; margin: 25px 0 15px; border-bottom: 1px solid #30363d; padding-bottom: 5px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 15px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; }
        .card h3 { color: #58a6ff; font-size: 14px; margin-bottom: 8px; }
        .card .value { font-size: 28px; font-weight: bold; color: #c9d1d9; }
        .card .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
        .status-ok { color: #3fb950; }
        .status-err { color: #f85149; }
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; }
        th { color: #8b949e; font-size: 12px; font-weight: 600; text-transform: uppercase; }
        td { font-size: 14px; }
        .mono { font-family: monospace; }
        .tag { display: inline-block; background: #1f6feb; color: white; padding: 2px 8px; border-radius: 12px; font-size: 11px; }
        .tag.warning { background: #d29922; }
        .nav { display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }
        .nav a { color: #58a6ff; text-decoration: none; font-size: 14px; }
        .nav a:hover { text-decoration: underline; }
        .activity-item { padding: 8px 0; border-bottom: 1px solid #21262d; font-size: 13px; }
        .activity-item:last-child { border-bottom: none; }
        .time { color: #8b949e; font-size: 11px; }
        pre { background: #161b22; padding: 15px; border-radius: 6px; overflow-x: auto; font-size: 13px; }
        .add-form { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-top: 15px; }
        .add-form input { background: #0d1117; border: 1px solid #30363d; color: #e0e0e0; padding: 8px 12px; border-radius: 4px; width: 250px; }
        .add-form button { background: #238636; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; margin-left: 10px; }
        .add-form button:hover { background: #2ea043; }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">Overview</a>
            <a href="/repos">Repos</a>
            <a href="/vms">VMs</a>
            <a href="/activity">Activity</a>
        </div>
        <h1>VM Alignment Coordinator</h1>
        <p class="subtitle">Deployment coordination via RabbitMQ</p>
    </div>
</body>
</html>
"""

# ─── Dashboard ────────────────────────────────────────────────────────────────

def make_dashboard():
    try:
        queue_info = _rmq.get_queue_info()
        total_messages = sum(
            v.get("messages", 0) for v in queue_info.values() if isinstance(v, dict)
        )
    except Exception:
        queue_info = {}
        total_messages = 0

    with _state_lock:
        vms = _state.get("vms", {})
        repos_state = _state.get("repos", {})
        activity = _state.get("activity", [])
        configured_repos = _config.get("repos", [])

    def repo_display_name(name):
        for r in configured_repos:
            if r["name"] == name:
                return r.get("display_name", name)
        return name

    rows = ""
    for name, info in queue_info.items():
        rows += f"""<tr>
            <td>{repo_display_name(name)}</td>
            <td class="mono">{info.get('queue', '')}</td>
            <td>{info.get('messages', 0)}</td>
            <td>{info.get('consumers', 0)}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="4" style="color:#8b949e">No queues available</td></tr>'

    activity_rows = ""
    for entry in activity[:10]:
        etype = entry.get("type", "")
        tag_cls = "warning" if etype == "error" else ""
        detail = entry.get("detail", "")
        ts = entry.get("timestamp", "")
        repo_nm = entry.get("repo", "")
        activity_rows += f"""<div class="activity-item">
            <span class="tag {tag_cls}">{etype}</span>
            <strong>{repo_nm}</strong> — {detail}
            <span class="time">{ts}</span>
        </div>"""
    if not activity_rows:
        activity_rows = '<p style="color:#8b949e">No recent activity</p>'

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>VM Alignment — Coordinator</title>
    <meta http-equiv="refresh" content="10">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #58a6ff; margin-bottom: 5px; }}
        .subtitle {{ color: #8b949e; margin-bottom: 30px; }}
        h2 {{ color: #c9d1d9; margin: 25px 0 15px; border-bottom: 1px solid #30363d; padding-bottom: 5px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 15px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; }}
        .card h3 {{ color: #58a6ff; font-size: 14px; margin-bottom: 8px; }}
        .card .value {{ font-size: 28px; font-weight: bold; color: #c9d1d9; }}
        .card .sub {{ font-size: 12px; color: #8b949e; margin-top: 4px; }}
        .status-ok {{ color: #3fb950; }}
        .status-err {{ color: #f85149; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; }}
        th {{ color: #8b949e; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
        td {{ font-size: 14px; }}
        .mono {{ font-family: monospace; }}
        .tag {{ display: inline-block; background: #1f6feb; color: white; padding: 2px 8px; border-radius: 12px; font-size: 11px; }}
        .tag.warning {{ background: #d29922; }}
        .nav {{ display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }}
        .nav a {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
        .nav a:hover {{ text-decoration: underline; }}
        .activity-item {{ padding: 8px 0; border-bottom: 1px solid #21262d; font-size: 13px; }}
        .activity-item:last-child {{ border-bottom: none; }}
        .time {{ color: #8b949e; font-size: 11px; }}
        .heartbeat-ok {{ color: #3fb950; }}
        .heartbeat-old {{ color: #d29922; }}
        .heartbeat-dead {{ color: #f85149; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">Overview</a>
            <a href="/repos">Repos</a>
            <a href="/vms">VMs</a>
            <a href="/activity">Activity</a>
        </div>
        <h1>VM Alignment Coordinator</h1>
        <p class="subtitle">Deployment coordination via RabbitMQ</p>

        <h2>System Status</h2>
        <div class="grid">
            <div class="card">
                <h3>Repos</h3>
                <div class="value">{len(configured_repos)}</div>
                <div class="sub">configured</div>
            </div>
            <div class="card">
                <h3>Registered VMs</h3>
                <div class="value">{len(vms)}</div>
                <div class="sub">active</div>
            </div>
            <div class="card">
                <h3>Total Messages</h3>
                <div class="value">{total_messages}</div>
                <div class="sub">in queues</div>
            </div>
            <div class="card">
                <h3>Coordinator</h3>
                <div class="value status-ok">Online</div>
                <div class="sub">v{__version__}</div>
            </div>
        </div>

        <h2>Queue Health</h2>
        <table>
            <tr><th>Repo</th><th>Queue</th><th>Messages</th><th>Consumers</th></tr>
            {rows}
        </table>

        <h2>Recent Activity</h2>
        {activity_rows}
    </div>
</body>
</html>"""
    return html


# Override the dashboard route with our custom function
@app.route("/")
def dashboard():
    return make_dashboard()


@app.route("/repos")
def repos_page():
    with _state_lock:
        repos_state = _state.get("repos", {})
        configured_repos = _config.get("repos", [])
    
    rows = ""
    for r in configured_repos:
        name = r["name"]
        state = repos_state.get(name, {})
        last_push = state.get("last_push", "—")
        last_msg = state.get("last_commit_message", "")
        last_sha = state.get("last_commit_sha", "—")
        rows += f"""<tr>
            <td><strong>{r.get('display_name', name)}</strong></td>
            <td class="mono">{name}</td>
            <td class="mono">{r.get('repo_full_name', '')}</td>
            <td><code>{r.get('webhook_path', '')}</code></td>
            <td>{last_push[:19] if last_push else '—'}</td>
            <td>{last_msg[:40] if last_msg else '—'}</td>
            <td>
                <form method="post" action="/api/config/repos/{name}" onsubmit="return confirm('Remove {name}?')" style="display:inline">
                    <input type="hidden" name="_method" value="DELETE">
                    <button style="background:#b62324;color:white;border:none;padding:4px 10px;border-radius:4px;cursor:pointer">Remove</button>
                </form>
            </td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" style="color:#8b949e">No repos configured. Add one below.</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Repos — VM Alignment</title>
    <meta http-equiv="refresh" content="15">
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }}
        .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #58a6ff; }}
        .nav {{ display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }}
        .nav a {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
        .nav a:hover {{ text-decoration: underline; }}
        .nav a.active {{ color: #c9d1d9; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; }}
        th {{ color: #8b949e; font-size: 12px; text-transform: uppercase; }}
        .mono {{ font-family: monospace; }}
        h2 {{ color: #c9d1d9; margin: 25px 0 15px; }}
        .add-form {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-top: 20px; }}
        .add-form input {{ background: #0d1117; border: 1px solid #30363d; color: #e0e0e0; padding: 8px 12px; border-radius: 4px; margin-right: 10px; width: 220px; }}
        .add-form button {{ background: #238636; color: white; border: none; padding: 8px 20px; border-radius: 4px; cursor: pointer; }}
        .add-form label {{ color: #8b949e; font-size: 12px; display: block; margin-bottom: 5px; }}
        .form-group {{ display: inline-block; margin-right: 15px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">Overview</a>
            <a href="/repos" class="active">Repos</a>
            <a href="/vms">VMs</a>
            <a href="/activity">Activity</a>
        </div>
        <h1>Repositories</h1>

        <table>
            <tr>
                <th>Display Name</th><th>Name</th><th>GitHub Repo</th><th>Webhook Path</th>
                <th>Last Push</th><th>Last Message</th><th>Actions</th>
            </tr>
            {rows}
        </table>

        <h2>Add Repository</h2>
        <form class="add-form" method="post" action="/api/config/repos">
            <div class="form-group">
                <label>Display Name</label>
                <input name="display_name" placeholder="Keyboard Visualiser">
            </div>
            <div class="form-group">
                <label>Repo Name (slug)</label>
                <input name="name" placeholder="keyboardvisualiser">
            </div>
            <div class="form-group">
                <label>GitHub (owner/repo)</label>
                <input name="repo_full_name" placeholder="poscomaiden/Keyboardvisualiser">
            </div>
            <div style="margin-top:10px">
                <button type="submit">Add Repository</button>
            </div>
        </form>
    </div>
</body>
</html>"""
    return html


@app.route("/vms")
def vms_page():
    with _state_lock:
        vms = _state.get("vms", {})

    if not vms:
        rows = '<tr><td colspan="6" style="color:#8b949e">No VMs registered yet. VMs will appear here once they connect.</td></tr>'
    else:
        rows = ""
        for vm_id, vm in vms.items():
            last_hb = vm.get("last_heartbeat", "")
            repos = ", ".join(vm.get("repos", []))
            ip = vm.get("ip", "—")
            try:
                hb_time = datetime.fromisoformat(last_hb.replace("Z", "+00:00")) if last_hb else None
                age = (datetime.now(timezone.utc) - hb_time).total_seconds() if hb_time else None
                if age and age < 120:
                    hb_cls = "heartbeat-ok"
                    hb_text = f"{int(age)}s ago"
                elif age and age < 3600:
                    hb_cls = "heartbeat-old"
                    hb_text = f"{int(age//60)}m ago"
                else:
                    hb_cls = "heartbeat-dead"
                    hb_text = f"{int(age//3600)}h ago"
            except Exception:
                hb_cls = "heartbeat-dead"
                hb_text = last_hb[:19] if last_hb else "—"

            rows += f"""<tr>
                <td><a href="/vms/{vm_id}" style="color:#58a6ff;text-decoration:none"><strong>{vm.get('name', vm_id)}</strong></a></td>
                <td class="mono">{vm_id}</td>
                <td>{ip}</td>
                <td>{repos}</td>
                <td class="{hb_cls}">{hb_text}</td>
                <td><code>{vm.get('hostname', '')}</code></td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>VMs — VM Alignment</title>
    <meta http-equiv="refresh" content="15">
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }}
        .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #58a6ff; }}
        .nav {{ display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }}
        .nav a {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
        .nav a:hover {{ text-decoration: underline; }}
        .nav a.active {{ color: #c9d1d9; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #30363d; }}
        th {{ color: #8b949e; font-size: 12px; text-transform: uppercase; }}
        .mono {{ font-family: monospace; }}
        .heartbeat-ok {{ color: #3fb950; }}
        .heartbeat-old {{ color: #d29922; }}
        .heartbeat-dead {{ color: #f85149; }}
        .info-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 20px; color: #8b949e; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">Overview</a>
            <a href="/repos">Repos</a>
            <a href="/vms" class="active">VMs</a>
            <a href="/activity">Activity</a>
        </div>
        <h1>Virtual Machines</h1>

        <div class="info-box">
            <strong>{len(vms)}</strong> VM(s) registered. VMs appear here once they send a heartbeat to the coordinator.
        </div>

        <table>
            <tr>
                <th>Name</th><th>VM ID</th><th>IP</th><th>Subscribed Repos</th><th>Last Heartbeat</th><th>Hostname</th>
            </tr>
            {rows}
        </table>
    </div>
</body>
</html>"""
    return html


@app.route("/activity")
def activity_page():
    with _state_lock:
        activity = _state.get("activity", [])

    rows = ""
    for entry in activity[:50]:
        etype = entry.get("type", "")
        tag_cls = "warning" if etype in ("error", "repo_removed") else ""
        rows += f"""<div class="activity-item">
            <span class="time">{entry.get('timestamp', '')[:19]}</span>
            <span class="tag {tag_cls}">{etype}</span>
            <strong>{entry.get('repo', '')}</strong> — {entry.get('detail', '')}
        </div>"""

    if not rows:
        rows = '<p style="color:#8b949e;padding:20px">No activity recorded yet.</p>'

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Activity — VM Alignment</title>
    <meta http-equiv="refresh" content="15">
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }}
        .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
        h1 {{ color: #58a6ff; }}
        .nav {{ display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }}
        .nav a {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
        .nav a:hover {{ text-decoration: underline; }}
        .nav a.active {{ color: #c9d1d9; }}
        .activity-item {{ padding: 10px 0; border-bottom: 1px solid #21262d; font-size: 14px; }}
        .time {{ color: #8b949e; font-size: 12px; margin-right: 10px; }}
        .tag {{ display: inline-block; background: #1f6feb; color: white; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-right: 8px; }}
        .tag.warning {{ background: #d29922; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">Overview</a>
            <a href="/repos">Repos</a>
            <a href="/vms">VMs</a>
            <a href="/activity" class="active">Activity</a>
        </div>
        <h1>Activity Log</h1>
        {rows}
    </div>
</body>
</html>"""
    return html


# ─── VM Detail Page ─────────────────────────────────────────────────────────

@app.route("/vms/<vm_id>")
def vm_detail_page(vm_id):
    with _state_lock:
        vm = _state.get("vms", {}).get(vm_id)
        if not vm:
            return f"<html><body><h1>VM '{vm_id}' not found</h1><p><a href='/vms'>Back to VMs</a></p></body></html>", 404

    reported_cfg = vm.get("reported_config", {})
    pending_cfg = vm.get("pending_config", {})
    repos = vm.get("repos", [])

    # Build repo list HTML
    if repos:
        repos_html = ", ".join(f"<span class='tag'>{r}</span>" for r in repos)
    else:
        repos_html = "<span style='color:#8b949e'>none</span>"

    # Reported config as JSON
    reported_json = json.dumps(reported_cfg, indent=2)
    reported_json = reported_json.replace("\n", "<br>").replace(" ", "&nbsp;")

    # Pending config
    if pending_cfg:
        pending_json = json.dumps(pending_cfg, indent=2)
        pending_note = f"<div style='background:#2d2d00;border:1px solid #d29922;border-radius:6px;padding:10px;margin:10px 0'><strong style='color:#d29922'>Pending config queued:</strong><pre style='color:#d29922;margin:5px 0;font-size:12px'>{json.dumps(pending_cfg, indent=2)}</pre></div>"
    else:
        pending_note = ""

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>VM {vm_id} — VM Alignment</title>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
        .nav {{ display: flex; gap: 20px; padding: 15px 0; border-bottom: 1px solid #30363d; margin-bottom: 20px; }}
        .nav a {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
        .nav a:hover {{ text-decoration: underline; }}
        .back {{ color: #8b949e; font-size: 13px; margin-bottom: 15px; }}
        h1 {{ color: #58a6ff; margin-bottom: 5px; }}
        .vm-id {{ color: #8b949e; font-family: monospace; font-size: 13px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .card h2 {{ color: #c9d1d9; font-size: 16px; margin: 0 0 15px; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
        .info-grid {{ display: grid; grid-template-columns: 140px auto; gap: 8px; font-size: 14px; }}
        .info-key {{ color: #8b949e; }}
        .info-val {{ color: #e0d0d0; font-family: monospace; }}
        .tag {{ display: inline-block; background: #1f6feb; color: white; padding: 2px 8px; border-radius: 12px; font-size: 11px; margin-right: 4px; }}
        pre {{ background: #0d1117; padding: 12px; border-radius: 6px; font-size: 13px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }}
        .config-form textarea {{ background: #0d1117; border: 1px solid #30363d; color: #e0e0e0; padding: 12px; border-radius: 6px; width: 100%; font-family: monospace; font-size: 13px; min-height: 200px; }}
        .config-form button {{ background: #238636; color: white; border: none; padding: 10px 24px; border-radius: 6px; cursor: pointer; font-size: 14px; margin-top: 10px; }}
        .config-form button:hover {{ background: #2ea043; }}
        .config-form label {{ display: block; color: #c9d1d9; font-size: 14px; margin-bottom: 5px; }}
        .success {{ background: #2d2d00; border: 1px solid #3fb950; border-radius: 6px; padding: 10px; color: #3fb950; margin-top: 10px; display: none; }}
        .heartbeat-ok {{ color: #3fb950; }}
        .heartbeat-old {{ color: #d29922; }}
        .heartbeat-dead {{ color: #f85149; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="back"><a href="/vms">&larr; Back to VMs</a></div>
        <h1>{vm.get('name', vm_id)}</h1>
        <div class="vm-id">ID: {vm_id}</div>

        <div class="card">
            <h2>Status</h2>
            <div class="info-grid">
                <span class="info-key">IP Address</span><span class="info-val">{vm.get('ip', '—')}</span>
                <span class="info-key">Hostname</span><span class="info-val">{vm.get('hostname', '—')}</span>
                <span class="info-key">Coordinator</span><span class="info-val">{vm.get('coordinator_url', '—')}</span>
                <span class="info-key">Last Heartbeat</span><span class="info-val">{vm.get('last_heartbeat', '—')[:19] if vm.get('last_heartbeat') else '—'}</span>
                <span class="info-key">Subscribed Repos</span><span class="info-val">{repos_html}</span>
                <span class="info-key">Last Config Report</span><span class="info-val">{vm.get('last_config_report', '—')[:19] if vm.get('last_config_report') else '—'}</span>
            </div>
        </div>

        <div class="card">
            <h2>Reported Configuration</h2>
            <pre>{json.dumps(reported_cfg, indent=2) if reported_cfg else 'No config reported yet.'}</pre>
        </div>

        {pending_note}

        <div class="card config-form">
            <h2>Push Configuration to VM</h2>
            <p style="color:#8b949e;font-size:13px;margin-bottom:10px">
                Paste a JSON object below to push new config to this VM. The client will receive it on its next config poll.
                Leave empty to push only the fields you specify.
            </p>
            <form id="pushForm" method="post" action="/api/vms/{vm_id}/config">
                <label>Config JSON (partial updates are fine):</label>
                <textarea name="config_json" id="configJson" placeholder='{{"repos": [{{"name": "myrepo", "path": "C:\\\\Dev\\\\MyProject", "enabled": true}}], "health_interval_seconds": 60}}'></textarea>
                <button type="submit">Push Config to VM</button>
            </form>
            <div class="success" id="successMsg">Config queued and will be delivered to VM on next poll!</div>
        </div>

        <div class="card">
            <h2>Config Update History</h2>
            <pre>{"No config updates recorded yet." if not vm.get('last_config_report') else 'See Reported Configuration above for latest.'}</pre>
        </div>
    </div>
    <script>
        document.getElementById('pushForm').addEventListener('submit', async function(e) {{
            e.preventDefault();
            let jsonText = document.getElementById('configJson').value.trim();
            if (!jsonText) {{
                alert('Please enter config JSON');
                return;
            }}
            let config;
            try {{
                config = JSON.parse(jsonText);
            }} catch(e) {{
                alert('Invalid JSON: ' + e.message);
                return;
            }}
            let resp = await fetch('/api/vms/{vm_id}/config', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(config)
            }});
            if (resp.ok) {{
                document.getElementById('successMsg').style.display = 'block';
                document.getElementById('configJson').value = '';
                setTimeout(() => location.reload(), 1500);
            }} else {{
                let err = await resp.text();
                alert('Failed: ' + err);
            }}
        }});
    </script>
</body>
</html>"""
    return html


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    port = config.get("coordinator_port", 5000)
    host = config.get("coordinator_host", "0.0.0.0")
    app.logger.info(f"Starting VM Alignment Coordinator v{__version__} on {host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
