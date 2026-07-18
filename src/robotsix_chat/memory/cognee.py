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
import base64
import logging
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import MemorySettings

logger = logging.getLogger(__name__)

# Cap recalled context so a large graph can't blow up the prompt.
_MAX_RECALL_CHARS = 4000

# Patterns for kuzu/ladybug database errors that can be healed by
# removing the database files and letting cognee recreate them.
_SHADOW_MISSING_RE = re.compile(r"Cannot open file.*\.shadow.*No such file")
_DB_ID_MISMATCH_RE = re.compile(r"Database ID.*does not match")

# First 16 bytes of every SQLite database file.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_kuzu_db_entity(entry: Path) -> bool:
    """Return True if *entry* is (or could be) a kuzu graph database.

    The self-heal below deletes any database entity that lacks a companion
    ``.shadow`` file, on the theory that a shadow-less kuzu database is
    inconsistent.  But cognee's ``databases`` directory also holds two stores
    that are **not** kuzu and legitimately never have a ``.shadow``:

    * ``cognee_db`` — the SQLite relational store (default user, dataset
      registry, node/edge metadata), and
    * ``cognee.lancedb`` — the LanceDB vector store.

    Treating those as inconsistent kuzu databases wiped them on *every*
    startup, destroying the default user/dataset registry that
    ``cognee.search`` requires — so recall failed permanently while ingestion
    silently recreated a fresh, empty store.  Exclude them here so only real
    kuzu graph databases are ever healed.
    """
    name = entry.name
    # LanceDB vector store (a directory, e.g. ``cognee.lancedb``).
    if name.endswith(".lancedb"):
        return False
    # SQLite sidecar files (``-wal`` / ``-shm`` / ``-journal`` — note these
    # use a hyphen, unlike kuzu's ``.wal``).
    if name.endswith(("-wal", "-shm", "-journal")):
        return False
    # SQLite relational database — identify by magic header, robust to naming.
    if entry.is_file():
        try:
            with entry.open("rb") as fh:
                if fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC:
                    return False
        except OSError:
            pass
    return True


def _is_healable_kuzu_error(exc: BaseException) -> bool:
    """Return True if *exc* matches a kuzu error healable by rebuilding the DB."""
    msg = str(exc)
    return bool(_SHADOW_MISSING_RE.search(msg) or _DB_ID_MISMATCH_RE.search(msg))


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

        # Self-heal stale kuzu shadow directories/files — if the process
        # crashed or was killed while kuzu had a WAL/shadow directory open,
        # it is left behind with a database ID that does not match the
        # current database.  The next db open then hard-crashes with
        # "RuntimeError: Database ID ... does not match the current
        # database".  We remove any left-over shadow entries before cognee
        # ever opens the database so the crash is preempted.
        self._remove_stale_kuzu_shadows(system_root)

        cognee.config.data_root_directory(str(data_root))
        cognee.config.system_root_directory(str(system_root))

        # Extraction LLM — OpenRouter via litellm's `custom` provider.
        cognee.config.set_llm_provider(s.llm.provider)
        cognee.config.set_llm_model(s.llm.model)
        cognee.config.set_llm_endpoint(s.llm.endpoint)
        cognee.config.set_llm_api_key(s.llm.api_key.get_secret_value())

        # Embeddings — remote OpenAI-compatible server (Ollama / bge-m3).
        cognee.config.set_embedding_config(
            {
                "embedding_provider": s.embedding.provider,
                "embedding_model": s.embedding.model,
                "embedding_endpoint": s.embedding.endpoint,
                "embedding_dimensions": s.embedding.dimensions,
                "embedding_api_key": s.embedding.api_key.get_secret_value(),
                "huggingface_tokenizer": s.embedding.huggingface_tokenizer,
            }
        )

        logger.info(
            "cognee memory configured (data_dir=%s, embed=%s@%s, llm=%s)",
            data_dir,
            s.embedding.model,
            s.embedding.endpoint,
            s.llm.model,
        )

        self._register_litellm_langfuse_callback()

    @staticmethod
    def _remove_stale_kuzu_shadows(system_root: Path) -> None:
        """Heal stale or inconsistent kuzu database state from an unclean shutdown.

        Two conditions trigger a heal:

        1. **Orphan artifacts** — ``.shadow`` / ``.wal`` files left behind
           when the process was killed while kuzu had them open.
        2. **Missing shadow** — a database entity (file or directory) whose
           companion ``.shadow`` file is absent.  Opening such a database
           immediately fails with "IO exception: Cannot open file …
           .shadow: No such file or directory".

        In either case the entire dataset for that database (the database
        entity itself plus any ``.shadow`` / ``.wal`` siblings) is removed.
        The graph is a rebuildable cache of conversation memory, so starting
        from a clean slate is always safe.

        Only genuine kuzu graph databases are healed — cognee's SQLite
        relational store and LanceDB vector store have no ``.shadow`` by
        design (see :func:`_is_kuzu_db_entity`); deleting them would wipe the
        default user and dataset registry that ``search`` needs, silently
        breaking all recall.
        """
        databases_dir = system_root / "databases"
        if not databases_dir.exists():
            return

        # Collect all stale artifacts: both .shadow and .wal.
        stale_entries: list[Path] = []
        for pattern in ("*.shadow", "*.wal"):
            stale_entries.extend(databases_dir.glob(pattern))

        for entry in stale_entries:
            try:
                if entry.is_dir():
                    logger.warning("Removing stale kuzu artifact directory: %s", entry)
                    shutil.rmtree(entry)
                elif entry.is_file():
                    logger.warning("Removing stale kuzu artifact file: %s", entry)
                    entry.unlink()
            except OSError:
                logger.exception("Failed to remove stale kuzu artifact: %s", entry)

        # Build the set of database names that need a clean slate.
        db_names: set[str] = set()

        # From orphan artifacts: the DB they belong to must be recreated.
        for entry in stale_entries:
            for suffix in (".shadow", ".wal"):
                if entry.name.endswith(suffix):
                    db_names.add(entry.name[: -len(suffix)])
                    break

        # From DB entities missing their companion .shadow: the DB is
        # inconsistent and will fail on open. Only kuzu graph databases are
        # subject to this heal — the SQLite relational store and LanceDB
        # vector store have no .shadow by design and must never be deleted.
        for entry in databases_dir.iterdir():
            if entry.name.endswith((".shadow", ".wal")):
                continue
            if not _is_kuzu_db_entity(entry):
                continue
            shadow = databases_dir / (entry.name + ".shadow")
            if not shadow.exists():
                logger.warning(
                    "Kuzu database missing companion shadow file; "
                    "treating as inconsistent: %s",
                    entry,
                )
                db_names.add(entry.name)

        # Remove inconsistent database entities — handle both file and
        # directory forms (ladybug/kuzu can use either).
        for db_name in sorted(db_names):
            db_entity = databases_dir / db_name
            if db_entity.exists() and _is_kuzu_db_entity(db_entity):
                try:
                    if db_entity.is_dir():
                        logger.warning(
                            "Removing inconsistent kuzu database directory: %s",
                            db_entity,
                        )
                        shutil.rmtree(db_entity)
                    else:
                        logger.warning(
                            "Removing inconsistent kuzu database file: %s",
                            db_entity,
                        )
                        db_entity.unlink()
                except OSError:
                    logger.exception(
                        "Failed to remove inconsistent kuzu database: %s",
                        db_entity,
                    )

    def _register_litellm_langfuse_callback(self) -> None:
        """Wire litellm Langfuse OTLP tracing with dedicated cognee creds.

        Registers an explicitly-configured ``LangfuseOtelLogger`` *instance*
        (OTLP over HTTP) rather than the ``"langfuse_otel"`` string callback:
        the string form makes litellm build its config from the
        ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/``LANGFUSE_HOST``
        environment **lazily on the first LLM call** — which would pick up the
        main chat project's credentials (and, with ``LANGFUSE_HOST`` unset,
        default the exporter to Langfuse US cloud). An instance carries its
        own endpoint + Basic-auth header, so neither the process env nor
        llmio's already-initialized tracing is involved at all.

        Cognee's internal LLM traffic lands in the separate
        ``robotsix-chat-cognee`` Langfuse project (per-standards: one
        Langfuse project per repo/function).  Graceful no-op when dedicated
        creds are absent.
        """
        s = self._settings
        lf_public = s.langfuse.public_key.get_secret_value()
        lf_secret = s.langfuse.secret_key.get_secret_value()
        if not lf_public or not lf_secret:
            logger.debug(
                "cognee Langfuse creds not set; skipping litellm Langfuse callback"
            )
            return

        # The cognee project lives on the same Langfuse instance as the main
        # project, so reuse the deployment's base URL (llmio's env name, with
        # litellm's LANGFUSE_HOST honored as a fallback).
        lf_host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get(
            "LANGFUSE_HOST", ""
        )
        if not lf_host:
            logger.warning(
                "cognee Langfuse creds set but no LANGFUSE_BASE_URL/LANGFUSE_HOST; "
                "skipping litellm Langfuse callback (exporter would default to "
                "Langfuse US cloud)"
            )
            return

        try:
            import litellm  # type: ignore[import-not-found]
            from litellm.integrations.langfuse.langfuse_otel import (  # type: ignore[import-not-found]
                LangfuseOtelLogger,
            )
            from litellm.integrations.opentelemetry import (  # type: ignore[import-not-found]
                OpenTelemetryConfig,
            )

            # opentelemetry-exporter-otlp-proto-http ships with the ``tracing``
            # extra; import-check so we fail fast with a warning, not at call-time.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]  # noqa: F401
                OTLPSpanExporter,
            )
        except ImportError as exc:
            logger.warning(
                "litellm Langfuse OTEL tracing unavailable (%s); "
                "install the 'tracing' extra alongside 'memory' to enable it",
                exc,
            )
            return

        # Idempotent: setup() can run more than once per process.
        if any(isinstance(cb, LangfuseOtelLogger) for cb in litellm.callbacks):
            return

        if not lf_host.startswith("http"):
            lf_host = "https://" + lf_host
        # Langfuse's OTLP route needs the full signal path — the bare
        # /api/public/otel prefix 404s (verified against the live instance).
        endpoint = lf_host.rstrip("/") + "/api/public/otel/v1/traces"
        auth = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
        otel_logger = LangfuseOtelLogger(
            config=OpenTelemetryConfig(
                exporter="otlp_http",
                endpoint=endpoint,
                headers=f"Authorization=Basic {auth}",
                # llmio registers the GLOBAL tracer provider (main-project
                # exporter) at server startup; without this flag litellm
                # attaches to it and cognee spans land in the MAIN Langfuse
                # project. Forces a private, isolated provider instead.
                skip_set_global=True,
            )
        )
        litellm.callbacks.append(otel_logger)

        # Stamp all cognee-issued litellm calls with a component tag so they
        # are distinguishable at a glance in the Langfuse project.
        tag = "component:cognee"
        if litellm.langfuse_default_tags is None:
            litellm.langfuse_default_tags = [tag]
        elif tag not in litellm.langfuse_default_tags:
            litellm.langfuse_default_tags = [*list(litellm.langfuse_default_tags), tag]

        logger.info(
            "litellm Langfuse OTLP tracing configured for cognee traffic (%s)",
            endpoint,
        )

    # -- read -------------------------------------------------------------

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        """Return memory relevant to *query* (``""`` on any failure).

        *session_id* scopes the recall to one conversation, isolating
        session-level guidance across concurrent windows.

        Wrapped in :func:`asyncio.timeout` so a hang in the cognee stack
        (e.g. orphaned LanceDB adapter lock) degrades to "no memory"
        instead of freezing the caller forever.
        """
        if not query.strip():
            return ""
        try:
            async with asyncio.timeout(self._settings.recall_timeout_seconds):
                return await self._recall_core(query, session_id=session_id)
        except TimeoutError:
            logger.warning(
                "memory recall timed out after %.0fs; continuing without memory",
                self._settings.recall_timeout_seconds,
            )
            return ""
        except Exception as exc:
            # Best-effort: a recall failure (incl. the expected "empty store"
            # case on the first-ever message) must never break the reply, so
            # log it concisely — no ERROR-level traceback — and continue.
            logger.warning("memory recall failed (%s); continuing without memory", exc)
            return ""

    async def _recall_core(self, query: str, *, session_id: str | None = None) -> str:
        """Inner recall logic — separated so the timeout wrapper is clean."""
        await self.setup()
        import cognee
        from cognee import SearchType

        search_type = getattr(
            SearchType,
            self._settings.recall_search_type,
            SearchType.GRAPH_COMPLETION,
        )
        for attempt in range(2):
            try:
                results = await cognee.search(
                    query_type=search_type,
                    query_text=query,
                    session_id=session_id,
                )
                return _format_results(results)
            except Exception as exc:
                if attempt == 0 and _is_healable_kuzu_error(exc):
                    logger.warning(
                        "Kuzu graph open failed; rebuilding database: %s",
                        exc,
                    )
                    data_dir = Path(self._settings.data_dir).expanduser().resolve()
                    self._remove_stale_kuzu_shadows(data_dir / "system")
                    continue
                raise
        # Unreachable — the retry loop always either returns or raises.
        # Required to satisfy mypy's exhaustive check.
        return ""

    # -- write ------------------------------------------------------------

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Persist one exchange into long-term memory (consolidates the graph).

        *session_id* scopes the write to one conversation, isolating
        session-level guidance across concurrent windows.

        Wrapped in :func:`asyncio.timeout` so a hang in cognee's
        consolidation pipeline (e.g. orphaned LanceDB adapter lock)
        skips the write instead of leaking a stuck background task.
        """
        try:
            async with asyncio.timeout(self._settings.remember_timeout_seconds):
                await self._remember_core(
                    user_message, assistant_message, session_id=session_id
                )
        except TimeoutError:
            logger.warning(
                "memory write timed out after %.0fs; exchange not persisted",
                self._settings.remember_timeout_seconds,
            )
        except Exception:
            logger.exception("memory write failed; exchange not persisted")

    async def _remember_core(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Inner remember logic — separated so the timeout wrapper is clean."""
        await self.setup()
        import cognee

        text = f"User: {user_message}\nAssistant: {assistant_message}"
        for attempt in range(2):
            try:
                async with self._write_lock:
                    await cognee.add(text, session_id=session_id)
                    await cognee.cognify(session_id=session_id)
                return
            except Exception as exc:
                if attempt == 0 and _is_healable_kuzu_error(exc):
                    logger.warning(
                        "Kuzu graph open failed; rebuilding database: %s",
                        exc,
                    )
                    data_dir = Path(self._settings.data_dir).expanduser().resolve()
                    self._remove_stale_kuzu_shadows(data_dir / "system")
                    continue
                raise


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
