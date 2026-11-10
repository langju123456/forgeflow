"""Start the desk service: ``python -m forgeflow.desk_service all`` (or one part: retrieval | worker | gateway).

Needs Redis and RabbitMQ on loopback (``docker compose -f compose.desk-service.yaml up -d``, or Homebrew's
``redis`` and ``rabbitmq``), the pdf extra, and for answers a local Ollama with the answer model
(scripts/mac_real_reports_eval.sh creates ``forgeflow-qwen3-8b-ctx16k``). Then open
http://127.0.0.1:8790/ — the 年报问答 page, answering through the service.

``all`` starts one retrieval server, ``--workers`` worker processes and the gateway as child processes with
one shared configuration, and stops them together on Ctrl-C.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import DeskConfig

ROOT = Path(__file__).resolve().parents[3]


def _config(args) -> DeskConfig:
    return DeskConfig.from_env(reports_dir=getattr(args, "reports", None), model=getattr(args, "model", None),
                               prefetch=getattr(args, "prefetch", None))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("part", choices=["all", "retrieval", "worker", "gateway"])
    parser.add_argument("--reports", help="folder with the annual-report PDFs (default ~/code/finreports)")
    parser.add_argument("--model", help="answer model tag in the local Ollama")
    parser.add_argument("--workers", type=int, default=2, help="worker processes for `all`")
    parser.add_argument("--prefetch", type=int, help="jobs one worker runs at once")
    args = parser.parse_args(argv)
    config = _config(args)

    if args.part == "retrieval":
        from .retrieval_server import serve

        serve(config)
    elif args.part == "worker":
        from .worker import run_worker

        asyncio.run(run_worker(config))
    elif args.part == "gateway":
        import uvicorn

        from .gateway import create_app

        uvicorn.run(create_app(config, static_dir=ROOT / "web" / "dist"), host="127.0.0.1",
                    port=config.gateway_port, log_level="warning")
    else:
        env = {**os.environ, **config.to_env()}
        command = [sys.executable, "-m", "forgeflow.desk_service"]
        children = [subprocess.Popen([*command, "retrieval"], env=env)]
        children += [subprocess.Popen([*command, "worker"], env=env) for _ in range(args.workers)]
        children.append(subprocess.Popen([*command, "gateway"], env=env))
        print(f"desk service: http://127.0.0.1:{config.gateway_port}/  ({args.workers} workers, "
              f"prefetch {config.prefetch}, model {config.model})", flush=True)

        def stop(*_):
            for child in children:
                child.send_signal(signal.SIGTERM)

        signal.signal(signal.SIGTERM, stop)
        try:
            while all(child.poll() is None for child in children):
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        stop()
        for child in children:
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
