#!/usr/bin/env python3
"""Recall relevant private-memory excerpts from the optional local semantic index."""
import argparse
import json
import math
import sqlite3
from array import array
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODEL_NAME = 'BAAI/bge-small-zh-v1.5'


def default_embed_query(text):
    try:
        from fastembed import TextEmbedding
    except ImportError as exc:
        raise RuntimeError(
            'Semantic-memory dependencies are missing. Run '
            'python -m pip install -r requirements-memory.txt'
        ) from exc
    model = TextEmbedding(model_name=MODEL_NAME)
    return list(model.embed([text]))[0]


def decode_vector(blob):
    vector = array('f')
    vector.frombytes(blob)
    return vector


def cosine_similarity(left, right):
    if len(left) != len(right):
        raise ValueError('Embedding dimensions do not match. Rebuild the local index.')
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return float(dot / (left_norm * right_norm)) if left_norm and right_norm else 0.0


def recall(query, database, top_k=6, min_score=0.25, embed_query=None):
    database = Path(database)
    if not database.is_file():
        raise ValueError('Local index not found. Run python tools/memory_index.py first.')
    query_vector = (embed_query or default_embed_query)(query)
    conn = sqlite3.connect(database)
    rows = conn.execute(
        'SELECT source, chunk_index, content, embedding FROM chunks'
    ).fetchall()
    conn.close()
    ranked = []
    for source, chunk_index, content, blob in rows:
        score = cosine_similarity(query_vector, decode_vector(blob))
        if score >= min_score:
            ranked.append({
                'score': round(score, 4),
                'source': source,
                'chunk': chunk_index,
                'content': content,
            })
    ranked.sort(key=lambda item: item['score'], reverse=True)
    return ranked[:top_k]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query')
    parser.add_argument('-k', '--top', type=int, default=6)
    parser.add_argument('--min-score', type=float, default=0.25)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--database', type=Path)
    args = parser.parse_args()
    database = args.database or args.root.resolve() / '.local' / 'memory_index.db'
    try:
        results = recall(
            args.query, database, args.top, args.min_score
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Memory recall failed: {exc}\n')
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f'Recall results for: {args.query}')
    if not results:
        print('No excerpts met the similarity threshold.')
    for position, item in enumerate(results, 1):
        print(
            f"\n#{position} | score {item['score']:.4f} | "
            f"{item['source']} (chunk {item['chunk']})\n{item['content']}"
        )


if __name__ == '__main__':
    main()
