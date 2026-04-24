# VM Alignment — Deployment Coordinator

## Overview

A centralized deployment coordination system that receives GitHub webhooks and fans out update signals to VMs via RabbitMQ. Each repo has its own queue. VMs subscribe to the repos they care about.

## Architecture

```
GitHub → Webhook → Coordinator Server → RabbitMQ → All subscribed VMs
                                    ↓
                              Web Dashboard (status, queue health, repo management)
```

## Components

### 1. Coordinator Server (`coordinator/`)

**Flask webhook receiver** — receives GitHub push events, validates signatures, publishes to RabbitMQ.

**RabbitMQ** — message broker. One topic exchange (`deployments`), queues per repo.

**Web Dashboard** — PyQt5 or Flask+HTML for managing repos, viewing queue health, VM status.

### 2. Client Service (`client/`)

**RabbitMQ subscriber** — runs as a Windows service, subscribes to one or more repo queues.

**Git pull executor** — on message received, runs `git pull` on configured repo path.

**Health reporter** — periodically sends heartbeat to coordinator dashboard.

### 3. Shared (`common/`)

**Message schema** — JSON messages between coordinator and clients.

## Message Schema

```json
{
  "repo": "Keyboardvisualiser",
  "branch": "main",
  "commit_sha": "abc123",
  "commit_message": "Add new feature",
  "author": "Aiden",
  "timestamp": "2025-04-24T12:00:00Z",
  "tag": null
}
```

## RabbitMQ Design

- **Exchange**: `deployments` (type: topic)
- **Queues**: 
  - `keyboardvisualiser.queue` — binds to `keyboardvisualiser.push`
  - `swift2sen.queue` — binds to `swift2sen.push`
  - `bulkdepartment.queue` — binds to `bulkdepartment.push`
  - `*` routing key pattern for flexible routing
- **Per-VM subscriptions**: Each VM has a unique consumer tag, subscribes to queues for repos it runs

## Client Configuration (`client_config.json`)

```json
{
  "coordinator_url": "http://coordinator:5000",
  "rabbitmq_host": "coordinator.local",
  "rabbitmq_port": 5672,
  "vm_id": "vm-001",
  "vm_name": "VM 1 - POS Server",
  "repos": [
    {
      "name": "keyboardvisualiser",
      "path": "C:\\KeyboardVisualiser",
      "enabled": true
    }
  ],
  "health_interval_seconds": 30
}
```

## Coordinator Configuration (`coordinator_config.json`)

```json
{
  "coordinator_host": "0.0.0.0",
  "coordinator_port": 5000,
  "rabbitmq_host": "localhost",
  "rabbitmq_port": 5672,
  "rabbitmq_user": "guest",
  "rabbitmq_password": "guest",
  "github_secret": "your-github-webhook-secret",
  "repos": [
    {
      "name": "keyboardvisualiser",
      "display_name": "Keyboard Visualiser",
      "repo_full_name": "poscomaiden/Keyboardvisualiser",
      "webhook_path": "/webhook/keyboardvisualiser"
    },
    {
      "name": "swift2sen",
      "display_name": "Swift2SEN",
      "repo_full_name": "poscomaiden/Swift2SEN",
      "webhook_path": "/webhook/swift2sen"
    },
    {
      "name": "bulkdepartment",
      "display_name": "Bulk Department",
      "repo_full_name": "poscomaiden/BulkDepartment",
      "webhook_path": "/webhook/bulkdepartment"
    }
  ]
}
```

## Dashboard Features

- **Home** — Overview of all repos, total VMs, queue depths
- **Repos** — Add/remove repos, see last push time per repo
- **VMs** — List of registered VMs, last heartbeat, subscribed repos
- **Queues** — RabbitMQ queue depths, message rates
- **Activity** — Recent webhook events and deployments

## Windows Service

Client runs as `VMAlignmentClient` Windows service:
- Installs via `python service.py install`
- Starts via `python service.py start`
- Uninstalls via `python service.py remove`
- Logs to `vm_alignment_client.log`

## Version

__version__ = "1.0.0"
