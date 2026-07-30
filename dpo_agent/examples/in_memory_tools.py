"""In-memory DocumentTools for local testing.

Stores documents in a dict, with a simple chunk-by-character-count
chunking strategy. No external dependencies. Use this for unit
tests, local development, and as a reference implementation for
production DocumentTools (database, S3, CLM).

Imported as `dpo_agent.examples.in_memory_tools` so it ships with
the package but stays in an `examples` submodule. Production
users will write their own DocumentTools and import only from
`dpo_agent`.

Usage:
    from dpo_agent.examples.in_memory_tools import InMemoryDocStore
    from dpo_agent import DPOAgent, DocumentTools

    store = InMemoryDocStore(chunk_size=4000)
    store.add("contract-001", contract_text)
    tools = DocumentTools(
        get_document_size=store.size,
        retrieve_whole_document_content=store.get,
        get_number_of_chunks=store.chunk_count,
        get_document_chunk_by_index=store.get_chunk,
    )
    agent = DPOAgent(tools=tools)
    result = agent.review(document_id="contract-001")
"""

from __future__ import annotations


class InMemoryDocStore:
    """An in-memory document store with character-based chunking.

    Attributes:
        chunk_size: target characters per chunk. Chunks are
            produced by splitting the text at exactly `chunk_size`
            boundaries. The store does not respect section
            boundaries — a section may span multiple chunks.
            Production document stores should chunk at section
            boundaries instead.
    """

    def __init__(self, chunk_size: int = 4000):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.chunk_size = chunk_size
        self._docs: dict[str, str] = {}

    def add(self, document_id: str, text: str) -> None:
        """Add a document to the store."""
        self._docs[document_id] = text

    def size(self, document_id: str) -> int:
        """Return the character count of a document.

        Raises KeyError if the document is not in the store.
        """
        return len(self._docs[document_id])

    def get(self, document_id: str) -> str:
        """Return the full text of a document.

        Raises KeyError if the document is not in the store.
        """
        return self._docs[document_id]

    def chunk_count(self, document_id: str) -> int:
        """Return the number of chunks for a document."""
        text = self._docs[document_id]
        n = len(text) // self.chunk_size
        if len(text) % self.chunk_size != 0:
            n += 1
        return n

    def get_chunk(self, document_id: str, index: int) -> str:
        """Return a specific chunk (0-indexed).

        Negative indexes raise IndexError. Out-of-range positive
        indexes return '' (Python slice semantics). Bounds
        enforcement is the dispatcher's job — see dpo_agent.tools.
        """
        if index < 0:
            raise IndexError(f"chunk index {index} < 0")
        text = self._docs[document_id]
        start = index * self.chunk_size
        end = start + self.chunk_size
        return text[start:end]

    def as_document_tools(self) -> "DocumentTools":
        """Return a dpo_agent.DocumentTools instance bound to this store."""
        from dpo_agent import DocumentTools
        return DocumentTools(
            get_document_size=self.size,
            retrieve_whole_document_content=self.get,
            get_number_of_chunks=self.chunk_count,
            get_document_chunk_by_index=self.get_chunk,
        )


# A small example contract for smoke-testing. Not a real DPO
# exercise — just enough to exercise the tool loop.
EXAMPLE_CONTRACT = """
DATA PROCESSING ADDENDUM

This Data Processing Addendum ("DPA") forms part of the Master
Services Agreement between Acme Corp ("Provider") and Widget Inc
("Customer") dated 2024-01-15 (the "Agreement").

1. DEFINITIONS

"Personal Data" means any information relating to an identified
or identifiable natural person, as defined in Article 4(1) of
Regulation (EU) 2016/679 ("GDPR").

"Processing" has the meaning given in Article 4(2) GDPR.

2. ROLES OF THE PARTIES

The parties acknowledge that Provider acts as a Processor when
Processing Personal Data on behalf of Customer, and Customer acts
as the Controller.

3. PROCESSING PURPOSES

Provider shall Process Personal Data only for the following
purposes: (a) providing the Services as described in the
Agreement; (b) Customer support; (c) security and fraud prevention.

4. LAWFUL BASIS

Customer represents that it has established a lawful basis under
Article 6(1) GDPR for each Processing purpose.

5. SUB-PROCESSORS

Provider shall not engage any sub-processor without prior
specific written authorization from Customer. Provider shall
maintain a list of approved sub-processors at a URL provided
to Customer.

6. SECURITY MEASURES

Provider shall implement appropriate technical and
organizational measures to ensure a level of security
appropriate to the risk, including encryption of Personal
Data in transit and at rest.

7. PERSONAL DATA BREACH

Provider shall notify Customer without undue delay, and in any
case within 48 hours, after becoming aware of a Personal Data
Breach. The notification shall describe the nature of the
breach, the categories and approximate number of data
subjects affected, and the measures taken to address it.

8. INTERNATIONAL TRANSFERS

Provider may transfer Personal Data outside the European
Economic Area only with Customer's prior written consent and
subject to appropriate safeguards, including the Standard
Contractual Clauses adopted by the European Commission
(Decision 2021/914).

9. AUDIT

Customer may audit Provider's compliance with this DPA once
per calendar year, on at least 30 days' prior written notice.

10. TERM AND TERMINATION

This DPA shall remain in effect for the term of the Agreement.
Upon termination, Provider shall, at Customer's option, return
or delete all Personal Data, and certify such deletion in
writing.
"""
