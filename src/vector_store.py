import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from src.parser import ParsedDocument


@dataclass
class DocumentChunk:
    doc_id: str
    space: str
    allowed_roles: List[str]
    last_modified: str
    text: str
    chunk_index: int


class LocalVectorIndex:

    def __init__(self, chunk_size: int = 100, chunk_overlap: int = 20) -> None:
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}.")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError(
                f"chunk_overlap must satisfy 0 <= overlap < chunk_size "
                f"(got overlap={chunk_overlap}, chunk_size={chunk_size})."
            )
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunks: List[DocumentChunk] = []
        self.vocab: List[str] = []
        self.vocab_idx: Dict[str, int] = {}
        self.idf: np.ndarray = np.array([])
        self.chunk_vectors: np.ndarray = np.array([])
        self.logger = logging.getLogger(self.__class__.__name__)

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"\b[a-zA-Z0-9_]{2,}\b", text.lower())

    def chunk_document(self, doc: ParsedDocument) -> List[DocumentChunk]:
        words = doc.clean_content.split()
        if not words:
            self.logger.warning(f"Document {doc.doc_id} contains no text to chunk.")
            return []

        doc_chunks: List[DocumentChunk] = []
        i = 0
        chunk_idx = 0

        while i < len(words):
            chunk_words = words[i : i + self.chunk_size]
            chunk_text = " ".join(chunk_words)

            doc_chunks.append(
                DocumentChunk(
                    doc_id=doc.doc_id,
                    space=doc.space,
                    allowed_roles=list(doc.allowed_roles),
                    last_modified=doc.last_modified,
                    text=chunk_text,
                    chunk_index=chunk_idx
                )
            )

            chunk_idx += 1
            i += (self.chunk_size - self.chunk_overlap)

        self.logger.info(
            "Document chunking complete.",
            extra={"doc_id": doc.doc_id, "chunks_created": len(doc_chunks)}
        )
        return doc_chunks

    def add_documents(self, docs: List[ParsedDocument]) -> None:
        self.logger.info(f"Indexing {len(docs)} documents into local vector space.")

        all_chunks: List[DocumentChunk] = []
        for doc in docs:
            all_chunks.extend(self.chunk_document(doc))
        self.chunks = all_chunks

        if not self.chunks:
            self.logger.warning("No document chunks available to vectorize.")
            return

        unique_tokens = set()
        for chunk in self.chunks:
            unique_tokens.update(self._tokenize(chunk.text))

        self.vocab = sorted(list(unique_tokens))
        self.vocab_idx = {token: idx for idx, token in enumerate(self.vocab)}

        self.logger.info(
            "Vocabulary compiled.",
            extra={"vocab_size": len(self.vocab), "total_chunks": len(self.chunks)}
        )

        if not self.vocab:
            self.logger.warning("Empty vocabulary. Vector indexing skipped.")
            return

        N = len(self.chunks)
        df = np.zeros(len(self.vocab))
        for chunk in self.chunks:
            chunk_tokens = set(self._tokenize(chunk.text))
            for token in chunk_tokens:
                if token in self.vocab_idx:
                    df[self.vocab_idx[token]] += 1

        self.idf = np.log((1 + N) / (1 + df)) + 1.0

        self.chunk_vectors = np.zeros((N, len(self.vocab)))
        for idx, chunk in enumerate(self.chunks):
            tokens = self._tokenize(chunk.text)
            if not tokens:
                continue

            tf = np.zeros(len(self.vocab))
            for token in tokens:
                if token in self.vocab_idx:
                    tf[self.vocab_idx[token]] += 1
            tf = tf / len(tokens)

            self.chunk_vectors[idx] = tf * self.idf

        self.logger.info("TF-IDF Vector Space Indexing completed successfully.")

    def similarity_search(
        self, query: str, top_k: int, user_role: str
    ) -> List[Tuple[DocumentChunk, float]]:
        self.logger.info(
            "Executing similarity search.",
            extra={"query": query, "user_role": user_role, "top_k": top_k}
        )

        if not self.chunks or len(self.vocab) == 0:
            self.logger.warning("Search executed on empty index.")
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            self.logger.warning("Query contains no valid vocabulary tokens.")
            return []

        query_tf = np.zeros(len(self.vocab))
        for token in query_tokens:
            if token in self.vocab_idx:
                query_tf[self.vocab_idx[token]] += 1

        query_tf = query_tf / len(query_tokens)
        query_vector = query_tf * self.idf

        dot_products = np.dot(self.chunk_vectors, query_vector)
        chunk_norms = np.linalg.norm(self.chunk_vectors, axis=1)
        query_norm = np.linalg.norm(query_vector)

        similarities = np.zeros(len(self.chunks))
        if query_norm > 0:
            valid_norms = chunk_norms > 0
            similarities[valid_norms] = dot_products[valid_norms] / (
                chunk_norms[valid_norms] * query_norm
            )

        sorted_indices = np.argsort(similarities)[::-1]

        results: List[Tuple[DocumentChunk, float]] = []
        rbac_violations_intercepted = 0

        for idx in sorted_indices:
            score = float(similarities[idx])
            chunk = self.chunks[idx]

            if score <= 0.0:
                break

            if user_role in chunk.allowed_roles:
                results.append((chunk, score))
            else:
                rbac_violations_intercepted += 1

            if len(results) >= top_k:
                break

        if rbac_violations_intercepted > 0:
            self.logger.info(
                "RBAC filtered unauthorized chunks during retrieval.",
                extra={
                    "user_role": user_role,
                    "blocked_count": rbac_violations_intercepted,
                    "query": query
                }
            )

        return results
