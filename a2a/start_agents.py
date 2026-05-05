#!/usr/bin/env python3
"""
start_agents.py
Launches all A2A agent servers using the single generic server.py.
Run this BEFORE tests/day7.py or adk web.

Usage:
    python start_agents.py
"""
import subprocess
import sys
import time
import urllib.request
import os

CARDS = [
    "agent_cards/document_parser_card.json",
    "agent_cards/validator_card.json",
    "agent_cards/router_card.json",
]

def health_check(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3) as r:
            data = r.read()
            return json_status(data) == "ok"
    except Exception:
        return False

def json_status(data: bytes) -> str:
    try:
        import json
        return json.loads(data).get("status", "")
    except Exception:
        return ""

def main():
    root = os.path.dirname(os.path.abspath(__file__))  ## F:\Multi-Line Submission\
    processes = [] ## empty list that will store each launched server process

    print("=" * 55)
    print("  Insurance Intake — A2A Agent Server Launcher")
    print("=" * 55)

    for card_path in CARDS:
        full_path = os.path.join(root, card_path)
        print(f"\n[->] Starting server from {card_path}...")
        ## Launch server.py as a background process with the card file as input, and remember the handle so we can check on it and stop it later.
        proc = subprocess.Popen(
            [sys.executable, os.path.join(root, "server.py"), full_path],
            cwd=root
        )
        processes.append((card_path, proc))

    print("\n[->] Waiting for servers to boot...")
    time.sleep(5)

    # Read ports from cards
    import json
    all_up = True
    for card_path, proc in processes:
        full_path = os.path.join(root, card_path)
        with open(full_path) as f:
            card = json.load(f)
        port = card["port"]
        name = card["name"]
        if health_check(port):
            print(f"  [OK] {name} is UP -> http://localhost:{port}")
        else:
            print(f"  [X] {name} on port {port} did NOT respond")
            all_up = False

    if all_up:
        print("\n[OK] All agents ready! Run in another terminal:")
        print("      adk web")
        print("      python tests/day7.py (for batch demo)")
    else:
        print("\n[!] Some agents failed. Check the output above.")

    print("\nPress Ctrl+C to stop all agents.\n")
    try:
        for _, proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        print("\n[->] Shutting down...")
        for _, proc in processes:
            proc.terminate()
        print("[OK] All agents stopped.")

if __name__ == "__main__":
    main()
