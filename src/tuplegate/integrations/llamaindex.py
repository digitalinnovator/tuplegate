"""LlamaIndex retriever bound to one subject (``pip install tuplegate[llamaindex]``).

Build one per request; never share a retriever across users.

    retriever = AuthzLlamaRetriever(authorized, embed_model, subject=user_email, k=4)
    nodes = retriever.retrieve("how do we rotate deploy keys?")

``embed`` is a LlamaIndex ``BaseEmbedding`` or any callable from text to a vector.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

from tuplegate.authz.base import validate_subject
from tuplegate.retrieval import AuthorizedRetriever, Strategy


class AuthzLlamaRetriever(BaseRetriever):
    def __init__(
        self,
        engine: AuthorizedRetriever,
        embed: BaseEmbedding | Callable[[str], Sequence[float]],
        *,
        subject: str,
        k: int = 4,
        strategy: Strategy | None = None,
        **kwargs: Any,
    ) -> None:
        # Fail at construction: an unbound retriever must never fall back to some identity.
        validate_subject(subject)
        super().__init__(**kwargs)
        self._engine = engine
        self._embed = embed
        self._subject = subject
        self._k = k
        self._strategy = strategy

    def _vector(self, query: QueryBundle) -> Sequence[float]:
        if query.embedding is not None:
            return query.embedding
        if isinstance(self._embed, BaseEmbedding):
            return self._embed.get_query_embedding(query.query_str)
        return self._embed(query.query_str)

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        result = self._engine.retrieve(
            self._subject, self._vector(query_bundle), self._k, strategy=self._strategy
        )
        return [
            NodeWithScore(
                node=TextNode(
                    id_=h.chunk_id,
                    text=h.text,
                    metadata={
                        **dict(h.metadata),
                        "doc_id": h.doc_id,
                        "request_id": result.request_id,
                    },
                ),
                score=h.score,
            )
            for h in result.hits
        ]
