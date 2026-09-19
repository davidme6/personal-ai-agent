#!/usr/bin/env python3
"""Build an optional local semantic index from the Git-ignored private workspace."""
import argparse
import sqlite3
from array import array
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = 'BAAI/bge-small-zh-v1.5'
TEXT_EXTENSIONS = {'.md', '.txt', '.csv'}
SKIP_DIRECTORIES = {'__pycache__', 'node_modules', 'secrets'}
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
MIN_CHUNK = 20


def split_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = text.strip()
    if not text:
        return []
    step = chunk_size - overlap
    if step <= 0:
        raise ValueError('Chunk overlap must be smaller than chunk size.')
    return [
        text[start:start + chunk_size].strip()
        for start in range(0, len(text), step)
        if len(text[start:start + chunk_size].strip()) >= MIN_CHUNK
    ]


def discover_files(root):
    private_root = root / '.personal'
    if not private_root.is_dir():
        raise ValueError('Private workspace not found. Run python tools/init_workspace.py first.')
    found = {}
    for path in private_root.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(private_root).parts):
            continue
        stat = path.stat()
        found[path.relative_to(root).as_posix()] = (path, stat.st_mtime_ns, stat.st_size)
    return found


def connect_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            source TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            source TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            embedding BLOB NOT NULL,
            PRIMARY KEY (source, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
    ''')
    return conn


def default_embed(texts):
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError(
            'Semantic-memory dependencies are missing. Run '
            'python -m pip install -r requirements-memory.txt'
        ) from exc
    model = TextEmbedding(model_name=MODEL_NAME)
    return list(model.embed(texts))


def encode_vector(vector):
    return array('f', vector).tobytes()


def build_index(root=ROOT, database=None, embed=None, rebuild=False):
    root = Path(root).resolve()
    database = Path(database) if database else root / '.local' / 'memory_index.db'
    current = discover_files(root)
    conn = connect_database(database)
    indexed = {
        row[0]: (row[1], row[2])
        for row in conn.execute('SELECT source, mtime_ns, size FROM files')
    }
    stored_model = conn.execute(
        "SELECT value FROM metadata WHERE key='model'"
    ).fetchone()
    if rebuild or (stored_model and stored_model[0] != MODEL_NAME):
        changed = sorted(current)
        removed = sorted(set(indexed) - set(current))
    else:
        changed = sorted(
            source for source, (_, mtime_ns, size) in current.items()
            if indexed.get(source) != (mtime_ns, size)
        )
        removed = sorted(set(indexed) - set(current))

    pending = []
    file_chunks = {}
    for source in changed:
        text = current[source][0].read_text(encoding='utf-8', errors='replace')
        chunks = split_text(text)
        file_chunks[source] = chunks
        pending.extend((source, index, content) for index, content in enumerate(chunks))

    vectors = []
    if pending:
        vectors = (embed or default_embed)([item[2] for item in pending])
        if len(vectors) != len(pending):
            conn.close()
            raise ValueError('Embedding provider returned an unexpected number of vectors.')

    with conn:
        if rebuild:
            conn.execute('DELETE FROM chunks')
            conn.execute('DELETE FROM files')
        for source in set(changed) | set(removed):
            conn.execute('DELETE FROM chunks WHERE source=?', (source,))
        for source in removed:
            conn.execute('DELETE FROM files WHERE source=?', (source,))
        for source in changed:
            _, mtime_ns, size = current[source]
            conn.execute(
                'INSERT OR REPLACE INTO files(source, mtime_ns, size) VALUES (?, ?, ?)',
                (source, mtime_ns, size),
            )
        conn.executemany(
            'INSERT INTO chunks(source, chunk_index, content, embedding) VALUES (?, ?, ?, ?)',
            [
                (source, index, content, encode_vector(vector))
                for (source, index, content), vector in zip(pending, vectors)
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('model', ?)",
            (MODEL_NAME,),
        )
    total_files = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
    total_chunks = conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    conn.close()
    return {
        'changed_files': len(changed),
        'removed_files': len(removed),
        'total_files': total_files,
        'total_chunks': total_chunks,
        'database': str(database),
    }


def database_stats(database):
    database = Path(database)
    if not database.is_file():
        return {'total_files': 0, 'total_chunks': 0, 'database': str(database)}
    conn = connect_database(database)
    result = {
        'total_files': conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],
        'total_chunks': conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],
        'database': str(database),
    }
    conn.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--stats', action='store_true')
    args = parser.parse_args()
    database = args.database or args.root.resolve() / '.local' / 'memory_index.db'
    try:
        result = database_stats(database) if args.stats else build_index(
            args.root, database, rebuild=args.rebuild
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Memory indexing failed: {exc}\n')
    print(
        f"Local memory index ready: {result['total_files']} file(s), "
        f"{result['total_chunks']} chunk(s), {result['database']}"
    )


if __name__ == '__main__':
    main()
