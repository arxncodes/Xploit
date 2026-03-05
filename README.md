# Distributed Node Orchestration Framework

A modular Python framework for centralized command-and-control orchestration of remote tasking agents. Built for **DevOps automation** and **security research** portfolios.

---

## Architecture

```
┌──────────────────────┐         ┌──────────────────────┐
│   Generator          │         │   Controller         │
│   (Provisioner)      │         │   (Server)           │
│                      │         │                      │
│  Reads template ───► │         │  asyncio TCP server  │
│  Injects IP:PORT     │         │  Interactive CLI      │
│  Outputs agent .py   │         │  Multi-agent mgmt    │
└──────────────────────┘         └──────────┬───────────┘
                                            │ TCP (Base64)
                        ┌───────────────────┼───────────────────┐
                        │                   │                   │
               ┌────────▼──────┐   ┌────────▼──────┐   ┌───────▼───────┐
               │  Agent 1      │   │  Agent 2      │   │  Agent N      │
               │  recv → exec  │   │  recv → exec  │   │  recv → exec  │
               │  → send       │   │  → send       │   │  → send       │
               └───────────────┘   └───────────────┘   └───────────────┘
```

## Directory Structure

```
├── server/
│   └── controller.py          # Centralized control server
├── agent_templates/
│   └── agent_template.py      # Tasking agent template (with placeholders)
├── generator/
│   └── generate.py            # Provisioner that bakes in connection details
└── README.md
```

---

## Quick Start

### 1. Generate a Tasking Agent

```bash
cd generator
python generate.py --host <CONTROLLER_IP> --port <PORT>
# Example:
python generate.py --host 192.168.1.10 --port 4444
```

This reads `agent_templates/agent_template.py`, replaces the `<<HOST>>` and `<<PORT>>` placeholders, and writes `generated_agent.py`.

### 2. Start the Controller

```bash
cd server
python controller.py
# Enter bind address (default 0.0.0.0) and port (default 4444)
```

### 3. Deploy & Run the Agent

Copy `generated_agent.py` to the target node and execute it:

```bash
python generated_agent.py
```

The agent will connect back to the controller automatically. If the connection drops, it retries every 5 seconds.

---

## Controller Commands

| Command           | Description                              |
|--------------------|------------------------------------------|
| `list`            | Show all connected agents with IDs       |
| `interact <id>`   | Open an interactive shell to an agent    |
| `kill <id>`       | Disconnect a specific agent              |
| `exit`            | Shut down the controller and all agents  |

Inside an `interact` session, type any shell command. The agent executes it and returns the output. Type `background` to return to the main menu.

---

## How It Works

1. **All data is Base64-encoded** before transmission and decoded on receipt — this ensures network transparency and prevents formatting issues with binary or multi-line output.
2. **Newline (`\n`) is the message delimiter** — each message is a single Base64 string terminated by `\n`.
3. **The Controller uses `asyncio`** for concurrent agent handling and runs the interactive CLI in a background thread.
4. **The Tasking Agent uses `subprocess.run(shell=True)`** for Windows compatibility and captures both `stdout` and `stderr`.

---

## Requirements

- **Python 3.10+** (uses `match` type hints syntax)
- No third-party dependencies — stdlib only (`asyncio`, `socket`, `subprocess`, `base64`, `argparse`)

---

## Disclaimer

> **This framework is intended for authorized DevOps automation, security research, and educational purposes only.** Unauthorized access to computer systems is illegal. Always obtain proper written authorization before deploying agents on systems you do not own. The authors assume no liability for misuse.
