"""A read-only report of the three stores, for `llm-harness --storage-status`.

It never creates a file or a directory and never writes or migrates. The one
side effect is the local vector lock probe: it briefly opens each collection that
already exists (which the app does on first use anyway) and closes it again. A
remote `qdrant` vector store is never contacted. A `postgres` relational store and a
`pgvector` vector store are contacted through a read-only session and only ever shown
by their exception types: never the user, the password, the url or the server's text.
"""

import asyncio
import os
import sqlite3
from pathlib import Path

from llm_harness.config import ConfigError, HarnessConfig
from llm_harness.storage.files import FileStoreError, LocalFileStore
from llm_harness.storage.pgvector import PgVectorStatus, PgVectorStore
from llm_harness.storage.postgres import PostgresConnectError, PostgresRelationalStore
from llm_harness.storage.qdrant_edge import (
    QdrantEdgeVectorStore,
    existing_collection_names,
    read_collection_meta,
)
from llm_harness.storage.relational import ReadOnlySchema, read_schema_versions
from llm_harness.storage.vectors import VectorStoreError, VectorStoreLockedError
from llm_harness.store import expected_schema_versions, optional_schema_versions

_NEWER = "newer than this version of llm-harness"


async def storage_status(
    config: HarnessConfig, *, relational_path: Path, data_dir: Path
) -> list[str]:
    """The report lines for the stores `config` names.

    `relational_path` is the SQLite ledger's; a `postgres` ledger has none.
    """
    storage = config.storage
    if storage.relational.backend == "postgres":
        if storage.relational.url is None:
            # Config validation guarantees the url; this guards a hand-built config.
            raise ConfigError(
                'backend "postgres" needs [storage.relational] url in config.toml'
            )
        store = PostgresRelationalStore(
            storage.relational.url, config.postgres_password
        )
        lines = [f"relational: postgres  {store.label}"]
        lines += await _postgres_lines(store)
    else:
        lines = [f"relational: {storage.relational.backend}  {relational_path}"]
        lines += _relational_lines(relational_path)
    if storage.vector.backend == "qdrant":
        # Remote: no local path to look at, no server contact, and the api key is
        # neither used nor printed here (config loading has already read it).
        lines.append(
            f"vector: qdrant  {storage.vector.url}  "
            "(remote server, not contacted by --storage-status)"
        )
    elif storage.vector.backend == "none":
        lines.append('vector: none  (disabled: [storage.vector] backend = "none")')
    elif storage.vector.backend == "pgvector":
        if storage.vector.url is None:
            # Config validation guarantees the url; this guards a hand-built config.
            raise ConfigError(
                'backend "pgvector" needs [storage.vector] url in config.toml'
            )
        vector_store = PgVectorStore(storage.vector.url, config.postgres_password)
        lines.append(f"vector: pgvector  {vector_store.label}")
        lines += await _pgvector_lines(vector_store)
    else:
        vector_root = storage.vector.resolved_path(data_dir)
        lines.append(f"vector: {storage.vector.backend}  {vector_root}")
        lines += await _vector_lines(vector_root)
    files_root = storage.files.resolved_root(data_dir)
    lines.append(f"files: {storage.files.backend}  {files_root}")
    lines += await _files_lines(files_root)
    return lines


def _relational_lines(path: Path) -> list[str]:
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return [
            "  file: not created yet (it is created on first use)",
            "  schema: nothing to read yet",
        ]
    except OSError as exc:
        return [f"  file: not readable: {exc.strerror or type(exc).__name__}"]
    lines = [f"  file: exists ({size} bytes)"]
    try:
        schema = read_schema_versions(path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        return [*lines, f"  schema: not readable: {exc}"]
    return [*lines, *_schema_lines(schema)]


async def _postgres_lines(store: PostgresRelationalStore) -> list[str]:
    try:
        versions = await asyncio.to_thread(store.read_schema_versions_sync)
    except PostgresConnectError as exc:
        return [f"  {exc}"]
    except Exception as exc:  # noqa: BLE001 - any read failure, shown by type
        # The driver's text can quote a row or name the user: the type only.
        return [f"  schema: not readable ({type(exc).__name__})"]
    if versions is None:
        return ["  schema: nothing to read yet"]
    return _schema_lines(ReadOnlySchema(versions=versions, user_version=0))


async def _pgvector_lines(store: PgVectorStore) -> list[str]:
    try:
        status = await asyncio.to_thread(store.read_status_sync)
    except PostgresConnectError as exc:
        return [f"  {exc}"]
    except Exception as exc:  # noqa: BLE001 - any read failure, shown by type
        # The driver's text can quote a row or name the user: the type only.
        return [f"  not readable ({type(exc).__name__})"]
    return _pgvector_status_lines(status)


def _pgvector_status_lines(status: PgVectorStatus) -> list[str]:
    if status.extension_version is None:
        if status.extension_available:
            return [
                (
                    "  extension: not created in this database "
                    "(an administrator runs: CREATE EXTENSION vector;)"
                )
            ]
        return [
            (
                "  extension: not installed on this server "
                "(install the pgvector package, then run: CREATE EXTENSION vector;)"
            )
        ]
    lines = [f"  extension: vector {status.extension_version}"]
    if status.collections is None:
        return [*lines, "  collections: not set up yet (it is set up on first use)"]
    if not status.collections:
        return [*lines, "  no collections yet"]
    return lines + [
        f"  collection {name}: {dim} dimensions, {distance}"
        for name, dim, distance in status.collections
    ]


def _schema_lines(schema: ReadOnlySchema) -> list[str]:
    recorded = schema.versions or {}
    lines = []
    for namespace, expected in expected_schema_versions().items():
        version = recorded.get(namespace)
        if version is None:
            lines.append(_unrecorded_line(namespace, expected, schema.user_version))
        else:
            lines.append(_version_line(namespace, version, expected))
    optional = optional_schema_versions()
    for namespace in sorted(set(recorded) - set(expected_schema_versions())):
        if namespace in optional:
            lines.append(
                _version_line(namespace, recorded[namespace], optional[namespace])
            )
            continue
        lines.append(
            f"  schema: {namespace} version {recorded[namespace]} "
            "(this version of llm-harness does not know this namespace)"
        )
    return lines


def _version_line(namespace: str, version: int, expected: int) -> str:
    if version == expected:
        return f"  schema: {namespace} version {version} (the code expects {expected})"
    if version > expected:
        return (
            f"  schema: {namespace} version {version}, {_NEWER} "
            f"(the code expects {expected})"
        )
    return (
        f"  schema: {namespace} version {version} (the code expects {expected}), "
        "it will be migrated on the next start"
    )


def _unrecorded_line(namespace: str, expected: int, user_version: int) -> str:
    # The migration adopts `PRAGMA user_version` when no version is recorded.
    if user_version == 0:
        return f"  schema: {namespace} not set up yet (it is set up on first use)"
    if user_version > expected:
        return f"  schema: legacy file (user_version {user_version}), {_NEWER}"
    return (
        f"  schema: legacy file (user_version {user_version}), "
        "it will be migrated on the next start"
    )


async def _vector_lines(root: Path) -> list[str]:
    if not root.exists():
        return ["  not created yet"]
    try:
        names = existing_collection_names(root)
    except OSError as exc:
        return [f"  unreadable: {exc.strerror or type(exc).__name__}"]
    if not names:
        return ["  no collections yet"]
    return [await _collection_line(root, name) for name in names]


async def _collection_line(root: Path, name: str) -> str:
    try:
        dim, distance = read_collection_meta(root, name)
    except VectorStoreError as exc:
        return f"  collection {name}: unreadable: {exc}"
    # A fresh adapter per collection, so each shard is closed again at once.
    probe = QdrantEdgeVectorStore(root)
    try:
        await probe.ensure_collection(name, dim, distance)
    except VectorStoreLockedError:
        state = "locked (in use by another llm-harness process)"
    except VectorStoreError as exc:
        return f"  collection {name}: unreadable: {exc}"
    else:
        state = "free"
    try:
        await probe.close()
    except VectorStoreError as exc:
        return f"  collection {name}: unreadable: {exc}"
    if state == "free":
        return f"  collection {name}: {dim} dimensions, {distance}, free"
    return f"  collection {name}: {state}"


async def _files_lines(root: Path) -> list[str]:
    if not root.exists():
        return ["  not created yet"]
    try:
        found = await LocalFileStore(root).list("")
    except (FileStoreError, OSError) as exc:
        return [f"  unreadable: {exc}"]
    return [f"  {len(found)} file" + ("" if len(found) == 1 else "s")]
