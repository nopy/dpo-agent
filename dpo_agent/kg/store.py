"""GraphStore — Layer 4 of the 8-layer GraphRAG pipeline.

Persists the resolved contracts as a **property graph**.
This is a port of `wiki-contracts/kgpipeline/store.py`.

Default: SQLite (no external dependencies, portable).
The schema is designed to mirror Neo4j exactly; `to_cypher()` exports
a Cypher script that creates the same nodes/edges in Neo4j.

The graph schema (matches the Neo4j 2025 reference schema):

  Nodes:
    - (:Contract {contract_id, contract_type, title, summary, ...})
    - (:Party {name, role, aliases, ...})
    - (:Location {address, city, state, country})
    - (:Clause {clause_id, clause_type, summary, ...})
    - (:Obligation {obligor, obligee, action, deadline, condition, ...})

  Edges:
    - (Contract)-[:HAS_GOVERNING_LAW]->(Location)
    - (Contract)-[:HAS_CLAUSE]->(Clause)
    - (Contract)-[:HAS_OBLIGATION]->(Obligation)
    - (Contract)<-[:PARTY_TO {role}]-(Party)
    - (Party)-[:HAS_LOCATION]->(Location)

The dpo-agent integration:
- The `kg_build` task uses this to persist the final Contract
- The `kg_verify` task checks contracts are in the store
- The `kg_update` task classifies new facts vs existing
- The `kg_agent` task queries the store via `Retriever`
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .ontology import (
    Clause, Contract, Location, Obligation, Party, SCHEMA_VERSION,
)


# ─── SQLite schema ─────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes_contract (
    contract_id TEXT PRIMARY KEY,
    contract_type TEXT NOT NULL,
    title TEXT,
    summary TEXT NOT NULL,
    effective_date TEXT,
    end_date TEXT,
    duration TEXT,
    total_amount REAL,
    currency TEXT,
    governing_law_country TEXT,
    governing_law_state TEXT,
    governing_law_city TEXT,
    source_path TEXT,
    extraction_model TEXT,
    schema_version TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS nodes_party (
    party_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    aliases TEXT,  -- JSON list
    confidence_score REAL DEFAULT 1.0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(name, role)
);

CREATE TABLE IF NOT EXISTS nodes_location (
    location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT,
    city TEXT,
    state TEXT,
    country TEXT
);

CREATE TABLE IF NOT EXISTS nodes_clause (
    clause_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL,
    clause_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence_score REAL DEFAULT 1.0,
    source_chunk_id TEXT,
    char_start INTEGER,
    char_end INTEGER,
    quote TEXT,
    FOREIGN KEY (contract_id) REFERENCES nodes_contract(contract_id)
);

CREATE TABLE IF NOT EXISTS nodes_obligation (
    obligation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL,
    obligor TEXT NOT NULL,
    obligee TEXT NOT NULL,
    action TEXT NOT NULL,
    deadline TEXT,
    condition TEXT,
    confidence_score REAL DEFAULT 1.0,
    source_chunk_id TEXT,
    FOREIGN KEY (contract_id) REFERENCES nodes_contract(contract_id)
);

-- Edges (relationship tables)
CREATE TABLE IF NOT EXISTS edges_party_to (
    contract_id TEXT NOT NULL,
    party_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY (contract_id, party_id),
    FOREIGN KEY (contract_id) REFERENCES nodes_contract(contract_id),
    FOREIGN KEY (party_id) REFERENCES nodes_party(party_id)
);

CREATE TABLE IF NOT EXISTS edges_party_location (
    party_id INTEGER NOT NULL,
    location_id INTEGER NOT NULL,
    PRIMARY KEY (party_id, location_id),
    FOREIGN KEY (party_id) REFERENCES nodes_party(party_id),
    FOREIGN KEY (location_id) REFERENCES nodes_location(location_id)
);

CREATE TABLE IF NOT EXISTS edges_governing_law (
    contract_id TEXT NOT NULL,
    location_id INTEGER NOT NULL,
    PRIMARY KEY (contract_id, location_id),
    FOREIGN KEY (contract_id) REFERENCES nodes_contract(contract_id),
    FOREIGN KEY (location_id) REFERENCES nodes_location(location_id)
);

CREATE INDEX IF NOT EXISTS idx_clause_contract ON nodes_clause(contract_id);
CREATE INDEX IF NOT EXISTS idx_obligation_contract ON nodes_obligation(contract_id);
CREATE INDEX IF NOT EXISTS idx_party_to_contract ON edges_party_to(contract_id);
"""


# ─── GraphStore: the main API ────────────────────────────────────────────

class GraphStore:
    """SQLite-backed property graph store.

    Usage:
        store = GraphStore("contracts.db")
        store.upsert(contract)
        store.export_cypher("contracts.cypher")
        store.close()
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._connect()
        self._init_schema()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def _init_schema(self) -> None:
        assert self._conn is not None
        with self._conn:
            self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        assert self._conn is not None
        try:
            with self._conn:
                yield self._conn
        except Exception:
            self._conn.rollback()
            raise

    # ─── Upsert ─────────────────────────────────────────────────────────

    def upsert(self, contract: Contract) -> None:
        """Insert or update a Contract and all its relations."""
        now = datetime.utcnow().isoformat() + "Z"
        with self._tx() as conn:
            conn.execute("""
                INSERT INTO nodes_contract
                    (contract_id, contract_type, title, summary,
                     effective_date, end_date, duration,
                     total_amount, currency,
                     governing_law_country, governing_law_state, governing_law_city,
                     source_path, extraction_model, schema_version,
                     ingested_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(contract_id) DO UPDATE SET
                    contract_type = excluded.contract_type,
                    title = excluded.title,
                    summary = excluded.summary,
                    effective_date = excluded.effective_date,
                    end_date = excluded.end_date,
                    duration = excluded.duration,
                    total_amount = excluded.total_amount,
                    currency = excluded.currency,
                    governing_law_country = excluded.governing_law_country,
                    governing_law_state = excluded.governing_law_state,
                    governing_law_city = excluded.governing_law_city,
                    source_path = excluded.source_path,
                    extraction_model = excluded.extraction_model,
                    schema_version = excluded.schema_version,
                    ingested_at = excluded.ingested_at,
                    version = version + 1
            """, (
                contract.contract_id,
                contract.contract_type.value if hasattr(contract.contract_type, "value") else str(contract.contract_type),
                contract.title,
                contract.summary,
                contract.effective_date,
                contract.end_date,
                contract.duration,
                contract.total_amount.amount if contract.total_amount else None,
                contract.total_amount.currency if contract.total_amount else None,
                contract.governing_law.country if contract.governing_law else None,
                contract.governing_law.state if contract.governing_law else None,
                contract.governing_law.city if contract.governing_law else None,
                contract.source_path,
                contract.extraction_model,
                contract.schema_version,
                now,
            ))
            # Wipe and re-insert edges for this contract (simpler than diffing)
            conn.execute("DELETE FROM nodes_clause WHERE contract_id = ?", (contract.contract_id,))
            conn.execute("DELETE FROM nodes_obligation WHERE contract_id = ?", (contract.contract_id,))
            conn.execute("DELETE FROM edges_party_to WHERE contract_id = ?", (contract.contract_id,))
            conn.execute("DELETE FROM edges_governing_law WHERE contract_id = ?", (contract.contract_id,))
            # Clauses
            for clause in contract.clauses:
                ev = clause.evidence[0] if clause.evidence else None
                conn.execute("""
                    INSERT INTO nodes_clause
                        (contract_id, clause_type, summary, confidence_score,
                         source_chunk_id, char_start, char_end, quote)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    contract.contract_id,
                    clause.clause_type,
                    clause.summary,
                    clause.confidence_score,
                    ev.chunk_id if ev else None,
                    ev.char_start if ev else None,
                    ev.char_end if ev else None,
                    ev.quote if ev else None,
                ))
            # Obligations
            for ob in contract.obligations:
                ev = ob.evidence[0] if ob.evidence else None
                conn.execute("""
                    INSERT INTO nodes_obligation
                        (contract_id, obligor, obligee, action, deadline, condition,
                         confidence_score, source_chunk_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    contract.contract_id,
                    ob.obligor,
                    ob.obligee,
                    ob.action,
                    ob.deadline,
                    ob.condition,
                    ob.confidence_score,
                    ev.chunk_id if ev else None,
                ))
            # Parties (dedup by (name, role))
            for party in contract.parties:
                cur = conn.execute(
                    "SELECT party_id FROM nodes_party WHERE name = ? AND role = ?",
                    (party.name, party.role.value if hasattr(party.role, "value") else str(party.role)),
                )
                row = cur.fetchone()
                if row is None:
                    cur = conn.execute("""
                        INSERT INTO nodes_party (name, role, aliases, confidence_score, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        party.name,
                        party.role.value if hasattr(party.role, "value") else str(party.role),
                        json.dumps(party.aliases),
                        party.confidence_score,
                        now,
                        now,
                    ))
                    party_id = cur.lastrowid
                else:
                    party_id = row["party_id"]
                    conn.execute("""
                        UPDATE nodes_party SET last_seen = ?,
                            aliases = ?, confidence_score = ?
                        WHERE party_id = ?
                    """, (now, json.dumps(party.aliases), party.confidence_score, party_id))
                # Edge: party → contract
                conn.execute("""
                    INSERT OR IGNORE INTO edges_party_to (contract_id, party_id, role)
                    VALUES (?, ?, ?)
                """, (contract.contract_id, party_id, party.role.value if hasattr(party.role, "value") else str(party.role)))
                # Party location
                if party.location:
                    loc_id = self._upsert_location(conn, party.location)
                    conn.execute("""
                        INSERT OR IGNORE INTO edges_party_location (party_id, location_id)
                        VALUES (?, ?)
                    """, (party_id, loc_id))
            # Governing law location
            if contract.governing_law:
                loc_id = self._upsert_location(conn, contract.governing_law)
                conn.execute("""
                    INSERT OR IGNORE INTO edges_governing_law (contract_id, location_id)
                    VALUES (?, ?)
                """, (contract.contract_id, loc_id))

    def _upsert_location(self, conn: sqlite3.Connection, loc: Location) -> int:
        # Dedupe by (city, state, country, address)
        cur = conn.execute("""
            SELECT location_id FROM nodes_location
            WHERE IFNULL(address, '') = IFNULL(?, '')
              AND IFNULL(city, '') = IFNULL(?, '')
              AND IFNULL(state, '') = IFNULL(?, '')
              AND IFNULL(country, '') = IFNULL(?, '')
        """, (loc.address, loc.city, loc.state, loc.country))
        row = cur.fetchone()
        if row is not None:
            return int(row["location_id"])
        cur = conn.execute("""
            INSERT INTO nodes_location (address, city, state, country)
            VALUES (?, ?, ?, ?)
        """, (loc.address, loc.city, loc.state, loc.country))
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    # ─── Read API ───────────────────────────────────────────────────────

    def get_contract(self, contract_id: str) -> Optional[dict]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT * FROM nodes_contract WHERE contract_id = ?", (contract_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def all_contracts(self) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT * FROM nodes_contract ORDER BY contract_id")
        return [dict(r) for r in cur.fetchall()]

    def all_parties(self) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT * FROM nodes_party ORDER BY name")
        return [dict(r) for r in cur.fetchall()]

    def all_clauses(self) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT * FROM nodes_clause ORDER BY contract_id, clause_id")
        return [dict(r) for r in cur.fetchall()]

    def all_obligations(self) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("SELECT * FROM nodes_obligation ORDER BY contract_id, obligation_id")
        return [dict(r) for r in cur.fetchall()]

    def contracts_by_party(self, party_name: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT c.* FROM nodes_contract c
            JOIN edges_party_to e ON e.contract_id = c.contract_id
            JOIN nodes_party p ON p.party_id = e.party_id
            WHERE p.name = ?
        """, (party_name,))
        return [dict(r) for r in cur.fetchall()]

    def parties_by_contract(self, contract_id: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT p.*, e.role as edge_role FROM nodes_party p
            JOIN edges_party_to e ON e.party_id = p.party_id
            WHERE e.contract_id = ?
        """, (contract_id,))
        return [dict(r) for r in cur.fetchall()]

    def clauses_by_contract(self, contract_id: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT * FROM nodes_clause WHERE contract_id = ?
            ORDER BY clause_id
        """, (contract_id,))
        return [dict(r) for r in cur.fetchall()]

    def obligations_by_contract(self, contract_id: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT * FROM nodes_obligation WHERE contract_id = ?
            ORDER BY obligation_id
        """, (contract_id,))
        return [dict(r) for r in cur.fetchall()]

    def obligations_by_deadline(self, before_date: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT * FROM nodes_obligation
            WHERE deadline IS NOT NULL AND deadline <= ?
            ORDER BY deadline
        """, (before_date,))
        return [dict(r) for r in cur.fetchall()]

    def contracts_by_governing_law(self, country: str) -> List[dict]:
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT * FROM nodes_contract WHERE governing_law_country = ?
            ORDER BY contract_id
        """, (country,))
        return [dict(r) for r in cur.fetchall()]

    def contracts_by_year(self, year: int) -> List[dict]:
        """All contracts whose effective_date is in the given calendar year."""
        assert self._conn is not None
        cur = self._conn.execute("""
            SELECT * FROM nodes_contract
            WHERE effective_date IS NOT NULL
              AND effective_date >= ? AND effective_date < ?
            ORDER BY effective_date
        """, (f"{year:04d}-01-01", f"{year + 1:04d}-01-01"))
        return [dict(r) for r in cur.fetchall()]

    def shortest_path(self, from_node: str, to_node: str, max_depth: int = 4) -> List[str]:
        """Find a path between two parties through shared contracts."""
        assert self._conn is not None
        visited: set[str] = {from_node}
        frontier: list[list[str]] = [[from_node]]
        for _ in range(max_depth):
            next_frontier: list[list[str]] = []
            for path in frontier:
                last = path[-1]
                cur = self._conn.execute("""
                    SELECT DISTINCT p2.name FROM edges_party_to e1
                    JOIN edges_party_to e2 ON e1.contract_id = e2.contract_id
                    JOIN nodes_party p1 ON p1.party_id = e1.party_id
                    JOIN nodes_party p2 ON p2.party_id = e2.party_id
                    WHERE p1.name = ? AND p2.name != ?
                """, (last, last))
                for r in cur.fetchall():
                    name = r["name"]
                    if name == to_node:
                        return path + [name]
                    if name not in visited:
                        visited.add(name)
                        next_frontier.append(path + [name])
            frontier = next_frontier
            if not frontier:
                return []
        return []

    def stats(self) -> dict:
        """Summary stats: count of each node/edge type."""
        assert self._conn is not None
        return {
            "contracts": self._conn.execute("SELECT COUNT(*) FROM nodes_contract").fetchone()[0],
            "parties": self._conn.execute("SELECT COUNT(*) FROM nodes_party").fetchone()[0],
            "clauses": self._conn.execute("SELECT COUNT(*) FROM nodes_clause").fetchone()[0],
            "obligations": self._conn.execute("SELECT COUNT(*) FROM nodes_obligation").fetchone()[0],
            "locations": self._conn.execute("SELECT COUNT(*) FROM nodes_location").fetchone()[0],
            "party_to_edges": self._conn.execute("SELECT COUNT(*) FROM edges_party_to").fetchone()[0],
        }

    # ─── Cypher export ─────────────────────────────────────────────────

    def to_cypher(self, *, only_contract: Optional[str] = None) -> str:
        """Export the graph as a Cypher script. Re-runnable.

        If `only_contract` is set, exports just that one contract.
        """
        lines: list[str] = []
        lines.append("// Auto-generated Cypher export from dpo_agent.kg.GraphStore")
        lines.append(f"// Generated: {datetime.utcnow().isoformat()}Z")
        lines.append(f"// Schema version: {SCHEMA_VERSION}")
        lines.append("")
        lines.append("// Create constraints (idempotent)")
        lines.append("CREATE CONSTRAINT contract_id IF NOT EXISTS FOR (c:Contract) REQUIRE c.contract_id IS UNIQUE;")
        lines.append("CREATE CONSTRAINT party_id IF NOT EXISTS FOR (p:Party) REQUIRE (p.name, p.role) IS UNIQUE;")
        lines.append("CREATE CONSTRAINT location_id IF NOT EXISTS FOR (l:Location) REQUIRE l.location_id IS UNIQUE;")
        lines.append("CREATE CONSTRAINT clause_id IF NOT EXISTS FOR (cl:Clause) REQUIRE cl.clause_id IS UNIQUE;")
        lines.append("CREATE CONSTRAINT obligation_id IF NOT EXISTS FOR (o:Obligation) REQUIRE o.obligation_id IS UNIQUE;")
        lines.append("")

        for c in (self.all_contracts() if not only_contract else [self.get_contract(only_contract)]):
            if c is None:
                continue
            props = {
                "contract_id": c["contract_id"],
                "contract_type": c["contract_type"],
                "title": c["title"],
                "summary": c["summary"],
                "effective_date": c["effective_date"],
                "end_date": c["end_date"],
                "duration": c["duration"],
                "total_amount": c["total_amount"],
                "currency": c["currency"],
                "source_path": c["source_path"],
                "extraction_model": c["extraction_model"],
                "schema_version": c["schema_version"],
                "version": c["version"],
            }
            props = {k: v for k, v in props.items() if v is not None}
            props_str = ", ".join(f"{k}: {_cypher_value(v)}" for k, v in props.items())
            lines.append(f"MERGE (c:Contract {{contract_id: '{_cypher_escape(c['contract_id'])}'}})")
            lines.append(f"  SET c += {{{props_str}}};")
            # Governing law
            if c.get("governing_law_country") or c.get("governing_law_state") or c.get("governing_law_city"):
                loc_merge: dict[str, str] = {}
                if c.get("governing_law_country"):
                    loc_merge["country"] = c["governing_law_country"]
                if c.get("governing_law_state"):
                    loc_merge["state"] = c["governing_law_state"]
                if c.get("governing_law_city"):
                    loc_merge["city"] = c["governing_law_city"]
                if loc_merge:
                    loc_props = loc_merge
                    loc_str = ", ".join(f"{k}: '{_cypher_escape(v)}'" for k, v in loc_props.items())
                    merge_str = ", ".join(f"{k}: '{_cypher_escape(v)}'" for k, v in loc_merge.items())
                    lines.append(f"MERGE (gov:Location {{{merge_str}}})")
                    lines.append(f"  SET gov += {{{loc_str}}};")
                    lines.append(f"MERGE (c)-[:HAS_GOVERNING_LAW]->(gov);")
            # Clauses
            for cl in self.clauses_by_contract(c["contract_id"]):
                clause_props = {
                    "clause_id": cl["clause_id"],
                    "clause_type": cl["clause_type"],
                    "summary": cl["summary"],
                    "confidence_score": cl["confidence_score"],
                }
                if cl["source_chunk_id"]:
                    clause_props["source_chunk_id"] = cl["source_chunk_id"]
                clause_str = ", ".join(f"{k}: {_cypher_value(v)}" for k, v in clause_props.items())
                lines.append(f"MERGE (c)-[:HAS_CLAUSE]->(cl:Clause {{clause_id: {cl['clause_id']}}})")
                lines.append(f"  SET cl += {{{clause_str}}};")
            # Obligations
            for ob in self.obligations_by_contract(c["contract_id"]):
                ob_props = {
                    "obligor": ob["obligor"],
                    "obligee": ob["obligee"],
                    "action": ob["action"],
                    "deadline": ob["deadline"],
                    "condition": ob["condition"],
                    "confidence_score": ob["confidence_score"],
                }
                ob_props = {k: v for k, v in ob_props.items() if v is not None}
                ob_str = ", ".join(f"{k}: {_cypher_value(v)}" for k, v in ob_props.items())
                lines.append(f"MERGE (c)-[:HAS_OBLIGATION]->(ob:Obligation {{obligor: '{_cypher_escape(ob['obligor'])}', obligee: '{_cypher_escape(ob['obligee'])}', action: '{_cypher_escape(ob['action'][:100])}'}})")
                lines.append(f"  SET ob += {{{ob_str}}};")
            # Parties
            for p in self.parties_by_contract(c["contract_id"]):
                lines.append(f"MERGE (party:Party {{name: '{_cypher_escape(p['name'])}', role: '{_cypher_escape(p['role'])}'}})")
                if p["aliases"]:
                    try:
                        aliases = json.loads(p["aliases"])
                        if aliases:
                            aliases_str = ", ".join(f"'{_cypher_escape(a)}'" for a in aliases)
                            lines.append(f"  SET party.aliases = [{aliases_str}];")
                    except json.JSONDecodeError:
                        pass
                lines.append(f"MERGE (party)-[r:PARTY_TO {{role: '{_cypher_escape(p['edge_role'])}'}}]->(c);")
        return "\n".join(lines) + "\n"


def _cypher_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f"'{_cypher_escape(v)}'"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_cypher_value(x) for x in v) + "]"
    return f"'{_cypher_escape(str(v))}'"


def _cypher_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
