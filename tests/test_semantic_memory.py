import sqlite3
import sys
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import memory_index
import memory_recall


def fake_vector(text):
    lowered = text.lower()
    return [float(lowered.count('alpha')), float(lowered.count('beta'))]


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='semantic-memory-test-')
        self.root = Path(self.temp.name)
        self.private = self.root / '.personal' / 'knowledge'
        self.private.mkdir(parents=True)
        self.database = self.root / '.local' / 'memory_index.db'

    def tearDown(self):
        self.temp.cleanup()

    def test_index_recall_and_deleted_source_cleanup(self):
        alpha = self.private / 'alpha.md'
        beta = self.private / 'beta.md'
        alpha.write_text('alpha ' * 30, encoding='utf-8')
        beta.write_text('beta ' * 30, encoding='utf-8')

        result = memory_index.build_index(
            self.root, self.database, embed=lambda texts: [fake_vector(t) for t in texts]
        )
        self.assertEqual(result['total_files'], 2)
        recalled = memory_recall.recall(
            'alpha', self.database, embed_query=fake_vector, min_score=0.1
        )
        self.assertEqual(recalled[0]['source'], '.personal/knowledge/alpha.md')

        alpha.unlink()
        result = memory_index.build_index(
            self.root, self.database, embed=lambda texts: [fake_vector(t) for t in texts]
        )
        self.assertEqual(result['removed_files'], 1)
        conn = sqlite3.connect(self.database)
        sources = [row[0] for row in conn.execute('SELECT source FROM files')]
        conn.close()
        self.assertEqual(sources, ['.personal/knowledge/beta.md'])

    def test_split_text_rejects_invalid_overlap(self):
        with self.assertRaises(ValueError):
            memory_index.split_text('useful text ' * 10, chunk_size=20, overlap=20)

    def test_recall_scores_are_json_serializable_with_numpy_embeddings(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('NumPy is installed with the optional semantic-memory dependency.')
        note = self.private / 'alpha.md'
        note.write_text('alpha ' * 30, encoding='utf-8')
        memory_index.build_index(
            self.root,
            self.database,
            embed=lambda texts: [np.array(fake_vector(t), dtype=np.float32) for t in texts],
        )

        recalled = memory_recall.recall(
            'alpha',
            self.database,
            embed_query=lambda text: np.array(fake_vector(text), dtype=np.float32),
            min_score=0.1,
        )

        json.dumps(recalled)
        self.assertIs(type(recalled[0]['score']), float)


if __name__ == '__main__':
    unittest.main()
