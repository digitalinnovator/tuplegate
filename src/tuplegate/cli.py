"""Command-line entry point: ``tuplegate``."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from tuplegate import schema

app = typer.Typer(no_args_is_help=True, add_completion=False)


class SchemaFormat(StrEnum):
    OPENFGA_JSON = "openfga-json"
    OPENFGA_DSL = "openfga-dsl"
    SPICEDB = "spicedb"


def render_schema(fmt: SchemaFormat) -> str:
    match fmt:
        case SchemaFormat.OPENFGA_JSON:
            return json.dumps(schema.openfga_model(), indent=2) + "\n"
        case SchemaFormat.OPENFGA_DSL:
            return schema.openfga_dsl()
        case SchemaFormat.SPICEDB:
            return schema.spicedb_schema()


SCHEMA_FILES = {
    SchemaFormat.OPENFGA_JSON: "openfga.json",
    SchemaFormat.OPENFGA_DSL: "openfga.fga",
    SchemaFormat.SPICEDB: "spicedb.zed",
}


@app.command("schema")
def schema_cmd(
    fmt: Annotated[SchemaFormat, typer.Option("--format", "-f")] = SchemaFormat.OPENFGA_DSL,
    write_dir: Annotated[
        Path | None, typer.Option("--write-dir", help="Write all formats into this directory.")
    ] = None,
) -> None:
    """Print the authorization schema, or write every rendering to a directory."""
    if write_dir is not None:
        write_dir.mkdir(parents=True, exist_ok=True)
        for f, name in SCHEMA_FILES.items():
            (write_dir / name).write_text(render_schema(f))
        typer.echo(f"wrote {len(SCHEMA_FILES)} files to {write_dir}")
        return
    typer.echo(render_schema(fmt), nl=False)


@app.command("version")
def version_cmd() -> None:
    """Print the package version."""
    from importlib.metadata import version

    typer.echo(version("tuplegate"))


@app.command("hash-token")
def hash_token_cmd(
    name: Annotated[str, typer.Argument(help="Caller name for the audit log.")],
) -> None:
    """Generate a caller token. Prints the token once, and the TUPLEGATE_CALLERS entry."""
    import hashlib
    import secrets

    token = secrets.token_urlsafe(32)
    typer.echo(f"token (give to the caller, shown once): {token}")
    typer.echo(f"TUPLEGATE_CALLERS entry: {name}:{hashlib.sha256(token.encode()).hexdigest()}")


@app.command("serve")
def serve_cmd(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8089,
) -> None:
    """Run the sidecar (needs the `server` extra). Settings come from TUPLEGATE_* variables."""
    import uvicorn

    uvicorn.run("tuplegate.server:app_from_env", factory=True, host=host, port=port)


class Source(StrEnum):
    GITHUB = "github"
    GDRIVE = "gdrive"
    CONFLUENCE = "confluence"


@app.command("sync")
def sync_cmd(
    source: Annotated[Source, typer.Argument()],
    embedder: Annotated[
        str | None,
        typer.Option(
            help="module:callable taking a list of texts and returning vectors. "
            "Without it the sync is ACL-only and your application ingests content."
        ),
    ] = None,
    sweep_expired: Annotated[bool, typer.Option(help="Also run the expiry sweep.")] = True,
) -> None:
    """Reconcile one source into the engine (and the index). Settings: TUPLEGATE_* variables."""
    import importlib

    from tuplegate.config import Settings, build_engine, build_index
    from tuplegate.identity import IdentityPassthrough, IdentityResolver, MappingResolver
    from tuplegate.sync import SyncEngine, SyncState

    s = Settings.from_env()
    identity: IdentityResolver | None = (
        MappingResolver.from_file(Path(s.identity_map)) if s.identity_map else None
    )
    embed = None
    if embedder:
        mod, _, attr = embedder.partition(":")
        embed = getattr(importlib.import_module(mod), attr)
    conn: Any
    match source:
        case Source.GITHUB:
            from tuplegate.connectors.github import GitHubConnector

            if not s.github_org:
                raise typer.BadParameter("set TUPLEGATE_GITHUB_ORG")
            conn = GitHubConnector(
                s.github_org,
                identity or MappingResolver({}),
                token=s.github_token,
                include_content=embed is not None,
            )
        case Source.GDRIVE:
            from tuplegate.connectors.gdrive import GoogleDriveConnector

            token = s.gdrive_token
            conn = GoogleDriveConnector(
                [d for d in s.gdrive_drive_ids.split(",") if d],
                (lambda: token) if token else None,
                identity=identity or IdentityPassthrough(),
                include_content=embed is not None,
            )
        case Source.CONFLUENCE:
            from tuplegate.connectors.confluence import ConfluenceConnector

            if not s.confluence_url:
                raise typer.BadParameter("set TUPLEGATE_CONFLUENCE_URL")
            classes = dict(
                p.split(":", 1) for p in s.confluence_access_class_groups.split(",") if ":" in p
            )
            conn = ConfluenceConnector(
                s.confluence_url,
                email=s.confluence_email,
                api_token=s.confluence_api_token,
                identity=identity,
                access_class_groups=classes,
                include_content=embed is not None,
            )
    engine = build_engine(s)
    index = build_index(s) if embed is not None else _NoIndex()
    sync = SyncEngine(engine, index, SyncState(s.sync_state), embedder=embed)
    rep = sync.reconcile(source.value, conn.snapshot())
    out: dict[str, Any] = {
        "source": source.value,
        "upserts": rep.upserts,
        "deletes": rep.deletes,
        "swept": rep.swept,
        "sweep_skipped": rep.sweep_skipped,
        "tuples_written": rep.tuples_written,
        "tuples_deleted": rep.tuples_deleted,
        "chunks_written": rep.chunks_written,
        "errors": rep.errors[:20],
        "stats": vars(conn.stats),
    }
    if sweep_expired:
        out["expired"] = sync.sweep_expired(source.value).expired
    typer.echo(json.dumps(out, indent=2, default=str))
    if rep.errors:
        raise typer.Exit(1)


class _NoIndex:
    """ACL-only sync: chunk deletions are the application's job when it owns ingestion."""

    name = "none"

    def ensure_schema(self) -> None:
        return None

    def upsert(self, chunks: Any) -> None:
        return None

    def replace_doc(self, doc_id: str, chunks: Any) -> None:
        return None

    def delete_docs(self, doc_ids: Any) -> int:
        return 0

    def search(self, vector: Any, k: int, *, acl_filter: Any = None, offset: int = 0) -> list[Any]:
        return []

    def count(self) -> int:
        return 0

    def close(self) -> None:
        return None
