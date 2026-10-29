"""LangChain retriever bound to one subject (``pip install tuplegate[langchain]``).

Build one per request with ``for_subject``; never share a retriever across users.

    retriever = AuthzRetriever(engine=authorized, embeddings=emb).for_subject(user_email)
    docs = retriever.invoke("how do we rotate deploy keys?")
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from tuplegate.authz.base import validate_subject
from tuplegate.retrieval import AuthorizedRetriever, Strategy


class AuthzRetriever(BaseRetriever):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    engine: AuthorizedRetriever
    embeddings: Embeddings
    subject: str = ""
    k: int = 4
    strategy: Strategy | None = None

    def for_subject(self, subject: str) -> AuthzRetriever:
        validate_subject(subject)
        return self.model_copy(update={"subject": subject})

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # An unbound retriever must fail, not fall back to some default identity.
        validate_subject(self.subject)
        vector = self.embeddings.embed_query(query)
        result = self.engine.retrieve(self.subject, vector, self.k, strategy=self.strategy)
        docs = []
        for h in result.hits:
            meta: dict[str, Any] = dict(h.metadata)
            meta.update(
                doc_id=h.doc_id, chunk_id=h.chunk_id, score=h.score, request_id=result.request_id
            )
            docs.append(Document(page_content=h.text, metadata=meta))
        return docs
