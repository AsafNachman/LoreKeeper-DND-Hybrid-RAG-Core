"""PDF ingestion pipeline: load, split, embed into ChromaDB, archive originals.

This module is the write path for the Lore Keeper knowledge base. The Streamlit
sidebar (`app`) and the CLI entrypoint below call `LoreIngestor` to move user PDFs
from `data/` into vectors under `db/` (bind-mounted in Docker). The read path is
`main`, which opens the same persisted Chroma directory.

Async: `LoreIngestor.arun` schedules blocking LangChain and filesystem calls on
`asyncio.to_thread` so the event loop can progress (useful when the UI or middleware
runs async code). `LoreIngestor.run` is a thin `asyncio.run` wrapper for synchronous
callers.

Embeddings: OpenAI `text-embedding-3-small`, the same model id as `main` so query
vectors live in the same geometry as document vectors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyPDFLoader
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _edition_from_filename(filename: str) -> str:
    """Infer D&D ruleset edition from a PDF filename.

    Args:
        filename: Raw PDF filename (with or without path).

    Returns:
        ``"2024"`` when the name contains ``2024`` or ``newest`` (case-insensitive),
        otherwise ``"2014"``.
    """
    lowered = Path(filename).name.lower()
    if "2024" in lowered or "newest" in lowered:
        return "2024"
    return "2014"


class LoreIngestor:
    """Ingest PDF files from a data directory into a persisted Chroma vector store.

    Each successful run moves processed PDFs into `{data_path}/ingested_lore/` so
    they are not re-embedded on the next invocation (idempotent queue drain).

    Attributes:
        data_path: Root folder scanned for `*.pdf` (typically `data/`).
        db_path: Chroma persistence root (typically `db/`, shared with `main.LoreKeeper`).
        ingested_lore_path: Archive subdirectory for processed originals.
        embeddings: Shared LangChain `OpenAIEmbeddings` client.
    """

    # Chunks per `add_documents` batch — balances OpenAI round-trips vs UI progress granularity.
    EMBED_BATCH = 50

    def __init__(self, data_path: str, db_path: str) -> None:
        """Validate API key and ensure on-disk folders exist.

        Args:
            data_path: Directory containing incoming `*.pdf` files.
            db_path: Chroma persistence directory path.

        Raises:
            ValueError: If `OPENAI_API_KEY` is not set.
        """
        load_dotenv()
        self.data_path = Path(data_path)
        self.db_path = db_path
        self.ingested_lore_path = self.data_path / "ingested_lore"
        self.api_key = os.getenv("OPENAI_API_KEY")

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is missing from .env file.")

        self.data_path.mkdir(exist_ok=True)
        self.ingested_lore_path.mkdir(exist_ok=True)

        self.embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        self.warnings: list[str] = []

    @staticmethod
    def _pdf_has_extractable_text(docs: list) -> bool:
        """Return True if the loaded PDF pages contain extractable text.

        Args:
            docs: LangChain `Document` pages returned by `PyPDFLoader.load()`.

        Returns:
            True if any page has a non-empty text payload after stripping; False for
            scanned/image PDFs (OCR required) or encrypted/empty PDFs.
        """
        for d in docs or []:
            text = getattr(d, "page_content", "") or ""
            if str(text).strip():
                return True
        return False

    def _move_to_ingested(self, file_path: Path) -> None:
        """Move a fully embedded PDF into `ingested_lore`, avoiding name collisions.

        Args:
            file_path: Absolute or relative path to the PDF under `data_path`.

        Raises:
            OSError: If `shutil.move` fails (permissions, cross-device issues).
        """
        dest = self.ingested_lore_path / file_path.name
        if dest.exists():
            dest = self.ingested_lore_path / (
                f"{file_path.stem}_{int(os.path.getmtime(file_path))}{file_path.suffix}"
            )
        shutil.move(str(file_path), str(dest))
        logger.info("Moved %s → %s", file_path.name, self.ingested_lore_path)

    async def ingest_pdf_to_vector_store_async(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Load PDFs, split, embed in batches, and archive sources asynchronously.

        Intent: PDF parsing and Chroma writes release the GIL intermittently but are
        still dominated by blocking I/O; `asyncio.to_thread` keeps the pattern ready for
        cooperative scheduling without claiming true parallel CPU speedup.

        Args:
            progress_callback: Optional `(done_chunks, total_chunks, filename)` hook
                for Streamlit progress bars.

        Returns:
            Number of PDF files processed, or 0 if none were found or all splits failed.

        Raises:
            None intentionally; per-file errors are logged; embedding errors surface
            from `to_thread`.
        """
        vectordb = Chroma(
            persist_directory=self.db_path,
            embedding_function=self.embeddings,
        )
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

        pdf_files = list(self.data_path.glob("*.pdf"))
        if not pdf_files:
            logger.info("No new PDF documents found to process.")
            return 0

        logger.info("Found %s file(s). Loading & splitting…", len(pdf_files))

        file_chunks: list[tuple[Path, list]] = []
        for pdf_file in pdf_files:
            try:
                loader = PyPDFLoader(str(pdf_file))
                docs = await asyncio.to_thread(loader.load)
                if not self._pdf_has_extractable_text(docs):
                    msg = (
                        f"🚫 This PDF appears to be an image. OCR is required for indexing: `{pdf_file.name}`"
                    )
                    self.warnings.append(msg)
                    logger.warning("Skipping image/scanned PDF (no text): %s", pdf_file.name)
                    continue
                chunks = await asyncio.to_thread(splitter.split_documents, docs)
                edition = _edition_from_filename(pdf_file.name)
                for chunk in chunks:
                    # Keep existing metadata and append edition for Chroma filtering.
                    chunk.metadata = dict(chunk.metadata or {})
                    chunk.metadata["edition"] = edition
                file_chunks.append((pdf_file, chunks))
                logger.info(
                    "Split %s → %s chunks (edition=%s)",
                    pdf_file.name,
                    len(chunks),
                    edition,
                )
            except Exception as exc:
                logger.error("FAILED to load %s: %s", pdf_file.name, exc)

        total_chunks = sum(len(chunks) for _, chunks in file_chunks)
        if total_chunks == 0:
            return 0

        logger.info(
            "Embedding %s chunks across %s file(s)…",
            total_chunks,
            len(file_chunks),
        )

        done_chunks = 0
        for pdf_file, chunks in file_chunks:
            for batch_start in range(0, len(chunks), self.EMBED_BATCH):
                batch = chunks[batch_start : batch_start + self.EMBED_BATCH]
                await asyncio.to_thread(vectordb.add_documents, batch)
                done_chunks += len(batch)
                if progress_callback:
                    progress_callback(done_chunks, total_chunks, pdf_file.name)

            try:
                await asyncio.to_thread(self._move_to_ingested, pdf_file)
            except Exception as exc:
                logger.error("Could not move %s: %s", pdf_file.name, exc)

        logger.info("✅ All new documents ingested.")
        return len(file_chunks)

    def ingest_pdf_to_vector_store(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Synchronous façade over async ingestion for loop-free callers.

        Args:
            progress_callback: Same semantics as `ingest_pdf_to_vector_store_async`.

        Returns:
            Number of PDF files processed.

        Raises:
            RuntimeError: If `asyncio.run` cannot start a new event loop (rare).
        """
        return asyncio.run(self.ingest_pdf_to_vector_store_async(progress_callback))

    # Backward-compatible alias kept for existing imports/callers.
    async def arun(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Backward-compatible alias for `ingest_pdf_to_vector_store_async`."""
        return await self.ingest_pdf_to_vector_store_async(progress_callback=progress_callback)

    # Backward-compatible alias kept for existing imports/callers.
    def run(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Backward-compatible alias for `ingest_pdf_to_vector_store`."""
        return self.ingest_pdf_to_vector_store(progress_callback=progress_callback)


if __name__ == "__main__":
    _ingestor = LoreIngestor(data_path="data", db_path="db")
    _ingestor.ingest_pdf_to_vector_store()
"""PDF ingestion pipeline: load, split, embed into ChromaDB, archive originals.

This module is the write path for the Lore Keeper knowledge base. The Streamlit
sidebar (`app`) and the CLI entrypoint below call `LoreIngestor` to move user PDFs
from `data/` into vectors under `db/` (bind-mounted in Docker). The read path is
`main`, which opens the same persisted Chroma directory.

Async: `LoreIngestor.arun` schedules blocking LangChain and filesystem calls on
`asyncio.to_thread` so the event loop can progress (useful when the UI or middleware
runs async code). `LoreIngestor.run` is a thin `asyncio.run` wrapper for synchronous
callers.

Embeddings: OpenAI `text-embedding-3-small`, the same model id as `main` so query
vectors live in the same geometry as document vectors.
"""

# This module keeps the historical misspelled filename for backward compatibility.

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.document_loaders import PyPDFLoader
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _edition_from_filename(filename: str) -> str:
    """Infer D&D ruleset edition from a PDF filename.

    Args:
        filename: Raw PDF filename (with or without path).

    Returns:
        ``"2024"`` when the name contains ``2024`` or ``newest`` (case-insensitive),
        otherwise ``"2014"``.
    """
    lowered = Path(filename).name.lower()
    if "2024" in lowered or "newest" in lowered:
        return "2024"
    return "2014"


class LoreIngestor:
    """Ingest PDF files from a data directory into a persisted Chroma vector store.

    Each successful run moves processed PDFs into `{data_path}/ingested_lore/` so
    they are not re-embedded on the next invocation (idempotent queue drain).

    Attributes:
        data_path: Root folder scanned for `*.pdf` (typically `data/`).
        db_path: Chroma persistence root (typically `db/`, shared with `main.LoreKeeper`).
        ingested_lore_path: Archive subdirectory for processed originals.
        embeddings: Shared LangChain `OpenAIEmbeddings` client.
    """

    # Chunks per `add_documents` batch — balances OpenAI round-trips vs UI progress granularity.
    EMBED_BATCH = 50

    def __init__(self, data_path: str, db_path: str) -> None:
        """Validate API key and ensure on-disk folders exist.

        Args:
            data_path: Directory containing incoming `*.pdf` files.
            db_path: Chroma persistence directory path.

        Raises:
            ValueError: If `OPENAI_API_KEY` is not set.
        """
        load_dotenv()
        self.data_path = Path(data_path)
        self.db_path = db_path
        self.ingested_lore_path = self.data_path / "ingested_lore"
        self.api_key = os.getenv("OPENAI_API_KEY")

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is missing from .env file.")

        self.data_path.mkdir(exist_ok=True)
        self.ingested_lore_path.mkdir(exist_ok=True)

        self.embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
        self.warnings: list[str] = []

    @staticmethod
    def _pdf_has_extractable_text(docs: list) -> bool:
        """Return True if the loaded PDF pages contain extractable text.

        Args:
            docs: LangChain `Document` pages returned by `PyPDFLoader.load()`.

        Returns:
            True if any page has a non-empty text payload after stripping; False for
            scanned/image PDFs (OCR required) or encrypted/empty PDFs.
        """
        for d in docs or []:
            text = getattr(d, "page_content", "") or ""
            if str(text).strip():
                return True
        return False

    def _move_to_ingested(self, file_path: Path) -> None:
        """Move a fully embedded PDF into `ingested_lore`, avoiding name collisions.

        Args:
            file_path: Absolute or relative path to the PDF under `data_path`.

        Raises:
            OSError: If `shutil.move` fails (permissions, cross-device issues).
        """
        dest = self.ingested_lore_path / file_path.name
        if dest.exists():
            dest = self.ingested_lore_path / (
                f"{file_path.stem}_{int(os.path.getmtime(file_path))}{file_path.suffix}"
            )
        shutil.move(str(file_path), str(dest))
        logger.info("Moved %s → %s", file_path.name, self.ingested_lore_path)

    async def ingest_pdf_to_vector_store_async(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Load PDFs, split, embed in batches, and archive sources asynchronously.

        Intent: PDF parsing and Chroma writes release the GIL intermittently but are
        still dominated by blocking I/O; `asyncio.to_thread` keeps the pattern ready for
        cooperative scheduling without claiming true parallel CPU speedup.

        Args:
            progress_callback: Optional `(done_chunks, total_chunks, filename)` hook
                for Streamlit progress bars.

        Returns:
            Number of PDF files processed, or 0 if none were found or all splits failed.

        Raises:
            None intentionally; per-file errors are logged; embedding errors surface
            from `to_thread`.
        """
        vectordb = Chroma(
            persist_directory=self.db_path,
            embedding_function=self.embeddings,
        )
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

        pdf_files = list(self.data_path.glob("*.pdf"))
        if not pdf_files:
            logger.info("No new PDF documents found to process.")
            return 0

        logger.info("Found %s file(s). Loading & splitting…", len(pdf_files))

        file_chunks: list[tuple[Path, list]] = []
        for pdf_file in pdf_files:
            try:
                loader = PyPDFLoader(str(pdf_file))
                docs = await asyncio.to_thread(loader.load)
                if not self._pdf_has_extractable_text(docs):
                    msg = (
                        f"🚫 This PDF appears to be an image. OCR is required for indexing: `{pdf_file.name}`"
                    )
                    self.warnings.append(msg)
                    logger.warning("Skipping image/scanned PDF (no text): %s", pdf_file.name)
                    continue
                chunks = await asyncio.to_thread(splitter.split_documents, docs)
                edition = _edition_from_filename(pdf_file.name)
                for chunk in chunks:
                    # Keep existing metadata and append edition for Chroma filtering.
                    chunk.metadata = dict(chunk.metadata or {})
                    chunk.metadata["edition"] = edition
                file_chunks.append((pdf_file, chunks))
                logger.info(
                    "Split %s → %s chunks (edition=%s)",
                    pdf_file.name,
                    len(chunks),
                    edition,
                )
            except Exception as exc:
                logger.error("FAILED to load %s: %s", pdf_file.name, exc)

        total_chunks = sum(len(chunks) for _, chunks in file_chunks)
        if total_chunks == 0:
            return 0

        logger.info(
            "Embedding %s chunks across %s file(s)…",
            total_chunks,
            len(file_chunks),
        )

        done_chunks = 0
        for pdf_file, chunks in file_chunks:
            for batch_start in range(0, len(chunks), self.EMBED_BATCH):
                batch = chunks[batch_start : batch_start + self.EMBED_BATCH]
                await asyncio.to_thread(vectordb.add_documents, batch)
                done_chunks += len(batch)
                if progress_callback:
                    progress_callback(done_chunks, total_chunks, pdf_file.name)

            try:
                await asyncio.to_thread(self._move_to_ingested, pdf_file)
            except Exception as exc:
                logger.error("Could not move %s: %s", pdf_file.name, exc)

        logger.info("✅ All new documents ingested.")
        return len(file_chunks)

    def ingest_pdf_to_vector_store(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Synchronous façade over async ingestion for loop-free callers.

        Args:
            progress_callback: Same semantics as `ingest_pdf_to_vector_store_async`.

        Returns:
            Number of PDF files processed.

        Raises:
            RuntimeError: If `asyncio.run` cannot start a new event loop (rare).
        """
        return asyncio.run(self.ingest_pdf_to_vector_store_async(progress_callback))

    # Backward-compatible alias kept for existing imports/callers.
    async def arun(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Backward-compatible alias for `ingest_pdf_to_vector_store_async`."""
        return await self.ingest_pdf_to_vector_store_async(progress_callback=progress_callback)

    # Backward-compatible alias kept for existing imports/callers.
    def run(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Backward-compatible alias for `ingest_pdf_to_vector_store`."""
        return self.ingest_pdf_to_vector_store(progress_callback=progress_callback)


if __name__ == "__main__":
    _ingestor = LoreIngestor(data_path="data", db_path="db")
    _ingestor.ingest_pdf_to_vector_store()
