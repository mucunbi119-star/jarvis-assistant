"""Start the local server and open the browser only after a readiness response."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


def main() -> int:
    parser = argparse.ArgumentParser(description="日本語アシスタントを起動します")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開きません")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("ポートは 1024〜65535 で指定してください")
    root = Path(__file__).resolve().parents[1]
    base_url = f"http://127.0.0.1:{args.port}"
    # Do not open an unrelated service when this port is already occupied.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            print(f"ポート {args.port} は使用中です。すでに起動したアシスタントを終了してください。", file=sys.stderr)
            return 1
    command = [sys.executable, "-m", "uvicorn", "assistant.main:app", "--host", "127.0.0.1", "--port", str(args.port)]
    options = {"cwd": str(root)}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **options)
    opener = build_opener(ProxyHandler({}))  # localhost must remain on this PC.
    try:
        deadline = time.monotonic() + 35
        ready = False
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with opener.open(base_url + "/api/health", timeout=1) as response:
                    payload = json.load(response)
                    if response.status == 200 and payload.get("status") == "ok":
                        ready = True
                        break
            except (URLError, OSError, ValueError):
                pass
            time.sleep(0.2)
        if not ready:
            print("起動の確認ができませんでした。上に表示されたエラーを確認してください。", file=sys.stderr)
            return process.returncode or 1
        print("起動しました。終了するには、この画面で Ctrl+C を押してください。", flush=True)
        if not args.no_browser:
            webbrowser.open(base_url)
        return process.wait()
    except KeyboardInterrupt:
        print("\n終了しています…", flush=True)
        return 0
    finally:
        if process.poll() is None:
            if sys.platform == "win32":
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    process.terminate()
            else:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
