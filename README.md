# VM Alignment — Deployment Coordinator

Centralized deployment coordination system. Receives GitHub webhooks and fans out update signals to VMs via RabbitMQ.

## Architecture

```
GitHub → Webhook → Coordinator Server → RabbitMQ → All subscribed VMs
                                    ↓
                              Web Dashboard (status, queue health, VM health)
```

## Components

- **Coordinator** — Flask webhook receiver + RabbitMQ publisher + web dashboard
- **Client** — RabbitMQ subscriber + git pull runner + Windows service
- **RabbitMQ** — Message broker (topic exchange, one queue per repo)

## Setup

### 1. Coordinator (one machine — needs to be reachable from VMs)

```bash
# Install dependencies
pip install -r requirements.txt

# Configure — edit coordinator/coordinator_config.json
# Set your github_secret and coordinator host

# Run
python coordinator/coordinator_server.py
```

Or with Docker:

```bash
docker compose up -d
```

**Dashboard**: http://localhost:5000

### 2. Connect GitHub Webhooks

For each repo, go to Settings → Webhooks → Add webhook:
- Payload URL: `http://YOUR_COORDINATOR_IP:5000/webhook/REPO_NAME`
- Content type: `application/json`
- Events: Just the `push` event

### 3. Client (on each VM)

```bash
# Install dependencies
pip install pika requests pywin32

# Configure — edit client/client_config.json
# Set coordinator_url, rabbitmq_host, vm_id, repos

# Install as Windows service
python client/service.py install
python client/service.py start
```

To run without installing as a service (debug mode):
```bash
python client/client_service.py
```

## Configuring Repos via Dashboard

Navigate to `http://COORDINATOR:5000/repos` to add/remove repos. The webhook paths are:
- `/webhook/keyboardvisualiser`
- `/webhook/swift2sen`
- `/webhook/bulkdepartment`

## Client Config

```json
{
  "coordinator_url": "http://192.168.1.100:5000",
  "rabbitmq_host": "192.168.1.100",
  "rabbitmq_port": 5672,
  "vm_id": "vm-001",
  "vm_name": "VM 1 - POS Server",
  "repos": [
    {"name": "keyboardvisualiser", "path": "C:\\KeyboardVisualiser", "enabled": true},
    {"name": "swift2sen", "path": "C:\\Swift2SEN", "enabled": true}
  ],
  "health_interval_seconds": 30
}
```

## Endpoints

- `GET /` — Dashboard
- `GET /repos` — Repos management page
- `GET /vms` — VM status page
- `GET /activity` — Activity log
- `POST /webhook/<repo>` — GitHub webhook receiver
- `GET /api/state` — Full state JSON
- `GET /api/queues` — Queue depths
- `GET /api/health` — Health check
- `POST /api/config/repos` — Add repo
- `DELETE /api/config/repos/<name>` — Remove repo
- `POST /api/vms/<vm_id>` — VM heartbeat

## RabbitMQ Management UI

Available at `http://COORDINATOR_IP:15672` (guest/guest)
