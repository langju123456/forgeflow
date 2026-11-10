"""One configuration for all three processes, read from FORGEFLOW_DESK_* environment variables.

Gateway and workers must agree on the answer model and its settings, because both compute the cache key from
them; the launcher (``python -m forgeflow.desk_service all``) passes one config to every child. A worker also
checks the model named in each job against its own and refuses a mismatch instead of caching under a wrong key.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class DeskConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    amqp_url: str = "amqp://guest:guest@127.0.0.1:5672/"
    grpc_target: str = "127.0.0.1:50061"
    gateway_port: int = 8790
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "forgeflow-qwen3-8b-ctx16k"  # the same preference order as cited_answer.PREFERRED_MODELS
    subject: str = "desk-analyst"  # DEV_ONLY: the one identity the retrieval server knows
    reports_dir: str = str(Path.home() / "code" / "finreports")
    work_dir: str = str(Path(__file__).resolve().parents[3] / ".desk-service")
    top_k: int = 5
    prefetch: int = 2  # jobs one worker runs at once; match the model server's parallel slots
    max_queue: int = 200  # RabbitMQ x-max-length; beyond it a publish is nacked and the gateway answers 429
    max_attempts: int = 3  # tries per job for transient failures (retrieval server or model unreachable)
    retry_delay_ms: int = 2000  # how long a failed job waits in desk.ask.retry before it is tried again
    grpc_deadline_s: float = 30.0  # Search includes the optional gate, which is a model call
    model_timeout_s: float = 300.0
    cache_ttl_s: int = 24 * 3600
    job_ttl_s: int = 3600
    token_flush_ms: int = 80  # coalesce streamed tokens into one Redis event per interval
    queue: str = "desk.ask"

    @classmethod
    def from_env(cls, **overrides) -> DeskConfig:
        values = {}
        for field in fields(cls):
            raw = os.environ.get(f"FORGEFLOW_DESK_{field.name.upper()}")
            if raw is not None:
                values[field.name] = type(field.default)(raw) if not isinstance(field.default, str) else raw
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def to_env(self) -> dict[str, str]:
        return {f"FORGEFLOW_DESK_{k.upper()}": str(v) for k, v in asdict(self).items()}

    @property
    def retry_queue(self) -> str:
        return f"{self.queue}.retry"

    @property
    def dead_queue(self) -> str:
        return f"{self.queue}.dead"
