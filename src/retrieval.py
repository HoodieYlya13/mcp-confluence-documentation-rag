import logging
from typing import List, Tuple

from src.parser import ParsedDocument
from src.settings import Settings
from src.vector_store import DocumentChunk, LocalVectorIndex

logger = logging.getLogger("retrieval")

ROLE_FLAG_PREFIX = "role_"


def role_flag(role: str) -> str:
    return f"{ROLE_FLAG_PREFIX}{role.lower().replace('-', '_')}"


class StructureAwareChunker:

    def __init__(self, max_words: int = 220) -> None:
        if max_words <= 0:
            raise ValueError(f"max_words must be positive, got {max_words}.")
        self.max_words = max_words

    def chunk_document(self, doc: ParsedDocument) -> List[DocumentChunk]:
        blocks = self._split_blocks(doc.clean_content)
        if not blocks:
            return []

        chunks: List[DocumentChunk] = []
        current_parts: List[str] = []
        current_words = 0
        active_heading = ""

        def emit() -> None:
            nonlocal current_parts, current_words
            if not current_parts:
                return
            text = "\n\n".join(current_parts).strip()
            if text:
                chunks.append(
                    DocumentChunk(
                        doc_id=doc.doc_id,
                        space=doc.space,
                        allowed_roles=list(doc.allowed_roles),
                        last_modified=doc.last_modified,
                        text=text,
                        chunk_index=len(chunks),
                        source_url=doc.source_url,
                    )
                )
            current_parts = []
            current_words = 0

        for block_type, block_text in blocks:
            if block_type == "heading":
                emit()
                active_heading = block_text
                continue

            block_words = len(block_text.split())
            if current_parts and current_words + block_words > self.max_words:
                emit()

            if not current_parts and active_heading:
                current_parts.append(active_heading)
                current_words = len(active_heading.split())

            current_parts.append(block_text)
            current_words += block_words

            if current_words > self.max_words:
                emit()

        emit()
        return chunks

    @staticmethod
    def _split_blocks(markdown_text: str) -> List[Tuple[str, str]]:
        blocks: List[Tuple[str, str]] = []
        paragraph: List[str] = []
        table: List[str] = []

        def flush_paragraph() -> None:
            if paragraph:
                blocks.append(("paragraph", "\n".join(paragraph)))
                paragraph.clear()

        def flush_table() -> None:
            if table:
                blocks.append(("table", "\n".join(table)))
                table.clear()

        for line in markdown_text.splitlines():
            stripped = line.strip()
            is_table_row = stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1

            if is_table_row:
                flush_paragraph()
                table.append(stripped)
                continue
            flush_table()

            if stripped.startswith("#"):
                flush_paragraph()
                blocks.append(("heading", stripped))
            elif not stripped:
                flush_paragraph()
            else:
                paragraph.append(stripped)

        flush_paragraph()
        flush_table()
        return blocks


class SemanticVectorIndex:

    COLLECTION_NAME = "ats_ops_chunks"

    def __init__(self, settings: Settings, persistent: bool = True) -> None:
        import chromadb
        from llama_index.core import VectorStoreIndex
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding
        from llama_index.vector_stores.chroma import ChromaVectorStore

        self.settings = settings
        self.logger = logging.getLogger(self.__class__.__name__)
        self.chunker = StructureAwareChunker(max_words=settings.chunk_max_words)
        self.chunks: List[DocumentChunk] = []

        self.logger.info(
            "Loading local embedding model.", extra={"model": settings.embedding_model}
        )
        self._embed_model = HuggingFaceEmbedding(model_name=settings.embedding_model)

        if persistent:
            client = chromadb.PersistentClient(path=settings.chroma_dir)
        else:
            client = chromadb.EphemeralClient()
        self._collection = client.get_or_create_collection(
            self.COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        self._vector_store = ChromaVectorStore(chroma_collection=self._collection)
        self._index = VectorStoreIndex.from_vector_store(
            self._vector_store, embed_model=self._embed_model
        )

    def add_documents(self, docs: List[ParsedDocument]) -> None:
        from llama_index.core.schema import TextNode

        self.logger.info(f"Indexing {len(docs)} documents into semantic vector space.")

        all_chunks: List[DocumentChunk] = []
        nodes: List[TextNode] = []
        current_doc_ids = {doc.doc_id for doc in docs}

        for doc in docs:
            doc_chunks = self.chunker.chunk_document(doc)
            all_chunks.extend(doc_chunks)
            title = str(doc.metadata.get("title", doc.doc_id))

            for chunk in doc_chunks:
                metadata = {
                    "doc_id": chunk.doc_id,
                    "space": chunk.space,
                    "title": title,
                    "source_url": chunk.source_url,
                    "last_modified": chunk.last_modified,
                    "chunk_index": chunk.chunk_index,
                    "allowed_roles": ",".join(chunk.allowed_roles),
                }
                for role in chunk.allowed_roles:
                    metadata[role_flag(role)] = 1

                node = TextNode(
                    text=chunk.text,
                    id_=f"{chunk.doc_id}:{chunk.chunk_index}",
                    metadata=metadata,
                )
                node.excluded_embed_metadata_keys = [
                    key for key in metadata if key != "title"
                ]
                node.excluded_llm_metadata_keys = list(metadata.keys())
                nodes.append(node)

        stale_doc_ids = self._existing_doc_ids() - current_doc_ids
        for doc_id in current_doc_ids | stale_doc_ids:
            self._collection.delete(where={"doc_id": {"$eq": doc_id}})

        if nodes:
            self._index.insert_nodes(nodes)

        self.chunks = all_chunks
        self.logger.info(
            "Semantic indexing completed.",
            extra={
                "indexed_documents": len(docs),
                "total_chunks": len(all_chunks),
                "stale_documents_removed": len(stale_doc_ids),
            },
        )

    def similarity_search(
        self, query: str, top_k: int, user_role: str
    ) -> List[Tuple[DocumentChunk, float]]:
        from llama_index.core.vector_stores import (
            FilterOperator,
            MetadataFilter,
            MetadataFilters,
        )

        self.logger.info(
            "Executing semantic similarity search.",
            extra={"query": query, "user_role": user_role, "top_k": top_k},
        )

        filters = MetadataFilters(
            filters=[
                MetadataFilter(key=role_flag(user_role), value=1, operator=FilterOperator.EQ)
            ]
        )
        retriever = self._index.as_retriever(similarity_top_k=top_k, filters=filters)
        retrieved = retriever.retrieve(query)

        results: List[Tuple[DocumentChunk, float]] = []
        for node_with_score in retrieved:
            metadata = node_with_score.node.metadata
            allowed_roles = str(metadata.get("allowed_roles", "")).split(",")

            if user_role not in allowed_roles:
                self.logger.critical(
                    "ACL filter pushdown failed: unauthorized chunk escaped the vector "
                    "store filter. Dropping chunk.",
                    extra={
                        "doc_id": metadata.get("doc_id"),
                        "user_role": user_role,
                        "security_violation": True,
                    },
                )
                continue

            chunk = DocumentChunk(
                doc_id=str(metadata.get("doc_id", "")),
                space=str(metadata.get("space", "")),
                allowed_roles=allowed_roles,
                last_modified=str(metadata.get("last_modified", "")),
                text=node_with_score.node.get_content(),
                chunk_index=int(metadata.get("chunk_index", 0)),
                source_url=str(metadata.get("source_url", "")),
            )
            score = float(node_with_score.score or 0.0)
            results.append((chunk, score))

        return results

    def _existing_doc_ids(self) -> set:
        records = self._collection.get(include=["metadatas"])
        return {
            str(metadata.get("doc_id"))
            for metadata in records.get("metadatas") or []
            if metadata and metadata.get("doc_id")
        }


def build_index(settings: Settings) -> LocalVectorIndex | SemanticVectorIndex:
    if settings.retriever_backend == "semantic":
        logger.info("Retriever backend: semantic (LlamaIndex + Chroma + local embeddings).")
        return SemanticVectorIndex(settings)
    logger.info("Retriever backend: tfidf (NumPy, zero-dependency).")
    return LocalVectorIndex(chunk_size=100, chunk_overlap=25)
