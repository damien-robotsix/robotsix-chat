"""Cognee-backed :class:`~robotsix_chat.memory.base.ChatMemory`.

Wires the embedded `cognee` knowledge-graph memory to:

* an **OpenRouter** extraction LLM (``custom`` provider via litellm), and
* a remote **OpenAI-compatible embedding** server (self-hosted Ollama / ``bge-m3``).

Configuration is global to the cognee process, so it is applied exactly once
(guarded by :attr:`_setup_lock`). Every public method is wrapped so a
misconfigured or unreachable backend degrades to "no memory" rather than
breaking the chat reply.

cognee is imported lazily (it is a heavy optional dependency, the ``memory``
extra); this module is only imported at all when that extra is installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MemorySettings

logger = logging.getLogger(__name__)

# Cap recalled context so a large graph can't blow up the prompt.
_MAX_RECALL_CHARS = 4000


class CogneeMemory:
    """Long-term agent memory backed by cognee.

    One instance per agent; cognee's own state is process-global. ``recall`` is
    on the request's latency path (kept to a vector/graph lookup), while
    ``remember`` runs the expensive consolidation and is expected to be called
    in the background by the agent.
    """

    def __init__(self, settings: MemorySettings) -> None:
        """Store settings; actual cognee configuration is deferred to ``setup``."""
        self._settings = settings
        self._setup_done = False
        self._setup_lock = asyncio.Lock()
        # Serialise writes: concurrent cognify() runs would contend on cognee's
        # shared stores. Recalls stay parallel.
        self._write_lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------

    async def setup(self) -> None:
        """Apply cognee configuration once (idempotent, concurrency-safe)."""
        if self._setup_done:
            return
        async with self._setup_lock:
            if self._setup_done:
                return
            self._configure()
            self._setup_done = True

    def _configure(self) -> None:
        s = self._settings
        # Embedded, single-user posture (cognee defaults to multi-tenant auth).
        os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
        os.environ.setdefault("TELEMETRY_DISABLED", "1")
        os.environ.setdefault("MONITORING_TOOL", "none")

        # cognee force-selects Langfuse as its monitoring tool when LANGFUSE_*
        # creds are present in the env (a model validator, overriding
        # MONITORING_TOOL) and then `import cognee` does `from langfuse.decorators
        # import observe` — which crashes because the image ships no langfuse SDK.
        # Hide those creds for cognee's (one-time) import so it caches
        # monitoring=NONE; llmio's own Langfuse tracing was already configured at
        # server startup, so it is unaffected.
        saved_langfuse = {
            key: os.environ.pop(key)
            for key in (
                "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY",
                "LANGFUSE_HOST",
                "LANGFUSE_BASE_URL",
            )
            if key in os.environ
        }
        try:
            import cognee
        finally:
            os.environ.update(saved_langfuse)

        # cognee builds file:// URIs from these, so they MUST be absolute —
        # a relative data_dir raises "relative paths can't be expressed as file
        # URIs" deep in ingestion. Resolve against the working dir.
        data_dir = Path(s.data_dir).expanduser().resolve()
        data_root = data_dir / "data"
        system_root = data_dir / "system"
        data_root.mkdir(parents=True, exist_ok=True)
        system_root.mkdir(parents=True, exist_ok=True)
        cognee.config.data_root_directory(str(data_root))
        cognee.config.system_root_directory(str(system_root))

        # Extraction LLM — OpenRouter via litellm's `custom` provider.
        cognee.config.set_llm_provider(s.llm.provider)
        cognee.config.set_llm_model(s.llm.model)
        cognee.config.set_llm_endpoint(s.llm.endpoint)
        cognee.config.set_llm_api_key(s.llm.api_key)

        # Embeddings — remote OpenAI-compatible server (Ollama / bge-m3).
        cognee.config.set_embedding_config(
            {
                "embedding_provider": s.embedding.provider,
                "embedding_model": s.embedding.model,
                "embedding_endpoint": s.embedding.endpoint,
                "embedding_dimensions": s.embedding.dimensions,
                "embedding_api_key": s.embedding.api_key,
                "huggingface_tokenizer": s.embedding.huggingface_tokenizer,
            }
        )

        self._register_litellm_langfuse_callback()

        logger.info(
            "cognee memory configured (data_dir=%s, embed=%s@%s, llm=%s)",
            data_dir,
            s.embedding.model,
            s.embedding.endpoint,
            s.llm.model,
        )

    def _register_litellm_langfuse_callback(self) -> None:
        """Wire litellm's ``langfuse_otel`` callback with dedicated cognee creds.

        Uses the OTLP-based callback (``langfuse_otel``) rather than the SDK
        callback (``langfuse``) because the Docker image ships no langfuse v2
        SDK.  The OTLP path only needs the OpenTelemetry exporter already
        available through the ``tracing`` extra.

        Cognee's internal LLM traffic lands in the separate
        ``robotsix-chat-cognee`` Langfuse project (per-standards: one
        Langfuse project per repo/function).  Graceful no-op when dedicated
        creds are absent.
        """
        s = self._settings
        lf_public = s.langfuse_public_key.get_secret_value()
        lf_secret = s.langfuse_secret_key.get_secret_value()
        if not lf_public or not lf_secret:
            logger.debug(
                "cognee Langfuse creds not set; skipping litellm Langfuse callback"
            )
            return

        import litellm  # type: ignore[import-not-found]

        # litellm's Langfuse logger reads credentials from the environment at
        # callback-registration time. Temporarily swap in the memory-project
        # creds, initialise the callback, then restore the main-project creds
        # so llmio's already-running OTel tracing is unaffected.
        lf_base_url = os.environ.get("LANGFUSE_BASE_URL", "")
        saved: dict[str, str | None] = {}
        for var, val in (
            ("LANGFUSE_PUBLIC_KEY", lf_public),
            ("LANGFUSE_SECRET_KEY", lf_secret),
        ):
            saved[var] = os.environ.get(var)
            os.environ[var] = val
        if lf_base_url:
            saved["LANGFUSE_BASE_URL"] = os.environ.get("LANGFUSE_BASE_URL")
            os.environ["LANGFUSE_BASE_URL"] = lf_base_url

        try:
            # Belt-and-suspenders: set both programmatic attrs and env vars.
            litellm.langfuse_public_key = lf_public
            litellm.langfuse_secret_key = lf_secret
            if lf_base_url:
                litellm.langfuse_base_url = lf_base_url

            callbacks: list[str] = (
                list(litellm.success_callback) if litellm.success_callback else []
            )
            if "langfuse_otel" not in callbacks:
                callbacks.append("langfuse_otel")
            litellm.success_callback = callbacks

            logger.info("litellm langfuse_otel callback configured for cognee traffic")
        finally:
            for var, old in saved.items():
                if old is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = old

    # -- read -------------------------------------------------------------

    async def recall(self, query: str) -> str:
        """Return memory relevant to *query* (``""`` on any failure)."""
        if not query.strip():
            return ""
        try:
            await self.setup()
            import cognee
            from cognee import SearchType

            search_type = getattr(
                SearchType,
                self._settings.recall_search_type,
                SearchType.GRAPH_COMPLETION,
            )
            results = await cognee.search(query_type=search_type, query_text=query)
            return _format_results(results)
        except Exception as exc:
            # Best-effort: a recall failure (incl. the expected "empty store"
            # case on the first-ever message) must never break the reply, so
            # log it concisely — no ERROR-level traceback — and continue.
            logger.warning("memory recall failed (%s); continuing without memory", exc)
            return ""

    # -- write ------------------------------------------------------------

    async def remember(self, user_message: str, assistant_message: str) -> None:
        """Persist one exchange into long-term memory (consolidates the graph)."""
        try:
            await self.setup()
            import cognee

            text = f"User: {user_message}\nAssistant: {assistant_message}"
            async with self._write_lock:
                await cognee.add(text)
                await cognee.cognify()
        except Exception:
            logger.exception("memory write failed; exchange not persisted")


def _format_results(results: Any) -> str:
    """Flatten cognee search results into a single bounded context string."""
    if not results:
        return ""
    if isinstance(results, str):
        text = results
    elif isinstance(results, list | tuple):
        text = "\n".join(str(item) for item in results if item)
    else:
        text = str(results)
    text = text.strip()
    if len(text) > _MAX_RECALL_CHARS:
        text = text[:_MAX_RECALL_CHARS].rstrip() + "…"
    return text
