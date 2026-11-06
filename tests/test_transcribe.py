import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import transcribe


class FakeModel:
    """冒充 WhisperModel：记下转了哪些文件；文件名带 broken 的转到一半抛错。"""
    def __init__(self):
        self.seen = []

    def transcribe(self, path, **options):
        self.seen.append((Path(path).name, options.get('language')))
        info = SimpleNamespace(language='zh', language_probability=0.98, duration=3661.5)

        def segments():
            yield SimpleNamespace(start=0.0, end=1.29, text=' 第一句 ')
            if 'broken' in path:
                raise RuntimeError('解码失败')
            yield SimpleNamespace(start=3661.5, end=3662.0, text='第二句')
        return segments(), info


class TranscribeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='transcribe-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def put(self, rel, data=b'x'):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_main(self, argv, loader):
        with redirect_stdout(io.StringIO()) as out:
            code = transcribe.main(argv, loader)
        return code, out.getvalue()

    def test_missing_faster_whisper_gives_install_hint(self):
        """防的是：没装可选依赖时甩出一串堆栈，用户不知道该装什么。"""
        self.put('a.mp3')
        with mock.patch.dict(sys.modules, {'faster_whisper': None}):
            code, out = self.run_main([str(self.root)], transcribe.load_model)
        self.assertEqual(code, 1)
        self.assertIn('requirements-media.txt', out)
        self.assertFalse((self.root / 'a.txt').exists())

    def test_already_transcribed_files_are_skipped_without_loading_model(self):
        """防的是：重跑时把几小时的录音重新转一遍，或者盖掉人工改过的文字稿。"""
        self.put('录音/周会.mp3')
        edited = self.put('录音/周会.txt', '人工改过的稿子'.encode('utf-8'))
        loader = mock.Mock()
        code, out = self.run_main([str(self.root)], loader)
        self.assertEqual(code, 0)
        loader.assert_not_called()
        self.assertIn('[跳过] 周会.mp3', out)
        self.assertEqual(edited.read_text(encoding='utf-8'), '人工改过的稿子')

    def test_directory_is_recursive_and_writes_txt_and_srt(self):
        """防的是：子目录里的音视频漏转、代码文件被当成视频，或者字幕时间戳格式错了播放器读不了。"""
        self.put('深/一层/访谈.M4A')
        self.put('深/main.ts', b'export {}')
        model = FakeModel()
        code, _ = self.run_main([str(self.root), '--lang', 'zh'], lambda name: model)
        self.assertEqual(code, 0)
        self.assertEqual(model.seen, [('访谈.M4A', 'zh')])
        folder = self.root / '深/一层'
        self.assertEqual((folder / '访谈.txt').read_text(encoding='utf-8'), '第一句\n第二句\n')
        self.assertEqual((folder / '访谈.srt').read_text(encoding='utf-8'),
                         '1\n00:00:00,000 --> 00:00:01,290\n第一句\n\n'
                         '2\n01:01:01,500 --> 01:01:02,000\n第二句\n')

    def test_failure_mid_file_leaves_nothing_so_it_is_retried(self):
        """防的是：转到一半出错留下半份 .txt，下次被当成已转写永远跳过；或者一个坏文件让整批停掉。"""
        self.put('broken.mp3')
        self.put('good.wav')
        code, out = self.run_main([str(self.root)], lambda name: FakeModel())
        self.assertEqual(code, 1)
        self.assertIn('失败 1：broken.mp3', out)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ['broken.mp3', 'good.srt', 'good.txt', 'good.wav'])


if __name__ == '__main__':
    unittest.main()
