"""飞书云空间同步：用假的飞书 CLI（代替 subprocess.run）在临时目录里验证三方比较不覆盖、不删除、不传错地方。"""
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import feishu_sync

TARGET = 'fld_target'


class FakeFeishu:
    """假的飞书 CLI：有状态的云空间。输出格式照真实 CLI 的 JSON（{"ok": true, "data": {...}}）；
    列目录每页只给两条，顺便走分页。收到删除、改权限之类没模拟的命令就让测试失败。"""

    def __init__(self, test):
        self.test, self.calls, self.count, self.clock = test, [], 0, 1_800_000_000
        self.items = {TARGET: {'name': '资料库', 'type': 'folder', 'parent': None}}
        self.account = {'appId': 'app-test', 'onBehalfOf': {'openId': 'user-a', 'userName': '测试账号'}}
        self.hidden = set()          # 列目录时故意漏掉的 token（模拟大目录列不全）
        self.broken = None           # 'paging'：has_more 却不给游标；'error'：列目录报错
        self.page_size = 2

    def tick(self):
        self.clock += 10
        return str(self.clock)

    def put(self, name, kind, parent, blob=b''):
        self.count += 1
        token = f'tok{self.count}'
        stamp = self.tick()
        self.items[token] = {'name': name, 'type': kind, 'parent': parent, 'blob': blob,
                             'created_time': stamp, 'modified_time': stamp}
        return token

    def folder(self, path):
        token = TARGET
        for part in [p for p in path.split('/') if p]:
            found = [t for t, i in self.items.items() if i['parent'] == token and i['name'] == part and i['type'] == 'folder']
            token = found[0] if found else self.put(part, 'folder', token)
        return token

    def add(self, rel, blob=b'', kind='file'):
        parent, _, name = rel.rpartition('/')
        return self.put(name, kind, self.folder(parent), blob)

    def tree(self):
        found = {}
        for token, item in self.items.items():
            parts, here = [], token
            while here not in (None, TARGET):
                parts.append(self.items[here]['name'])
                here = self.items[here]['parent']
            if here == TARGET and parts:
                found['/'.join(reversed(parts))] = token
        return found

    def blob(self, rel):
        return self.items[self.tree()[rel]]['blob']

    def edit(self, rel, blob):
        item = self.items[self.tree()[rel]]
        item['blob'], item['modified_time'] = blob, self.tick()

    def ran(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]

    def __call__(self, command, **kwargs):
        cwd = Path(kwargs['cwd'])
        args = list(command[1:])
        if args[:1] == ['--profile']:
            args = args[2:]
        self.test.assertEqual(args[-2:], ['--as', 'user'])          # 一律以用户身份，不用应用身份
        args = args[:-2]
        self.calls.append(args)
        answer = self.answer(cwd, args)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(answer, ensure_ascii=False).encode('utf-8'), stderr=b'')

    def answer(self, cwd, args):
        def opt(name):
            value = args[args.index(name) + 1]
            if name in ('--file', '--output', '--params', '--data'):
                self.test.assertFalse(Path(value.lstrip('@')).is_absolute(), value)   # CLI 只收工作区内的相对路径
            return value
        if args == ['whoami']:
            return self.account
        if args[:3] == ['drive', 'files', 'list']:
            if self.broken == 'error':
                return {'ok': False, 'error': {'message': 'permission denied'}}
            query = json.loads((cwd / opt('--params')[1:]).read_text(encoding='utf-8'))
            kids = sorted(t for t, i in self.items.items() if i['parent'] == query['folder_token'] and t not in self.hidden)
            start = int(query.get('page_token') or 0)
            more = start + self.page_size < len(kids)
            data = {'files': [{'token': t, 'name': self.items[t]['name'], 'type': self.items[t]['type'],
                               'parent_token': self.items[t]['parent'], 'created_time': self.items[t].get('created_time'),
                               'modified_time': self.items[t].get('modified_time'), 'owner_id': 'user-a',
                               'url': f'https://example.com/{t}'} for t in kids[start:start + self.page_size]],
                    'has_more': more}
            if more and self.broken != 'paging':
                data['next_page_token'] = str(start + self.page_size)
            return {'ok': True, 'data': data}
        if args[:2] == ['drive', '+upload']:
            source = cwd / opt('--file')
            if '--file-token' in args:
                token = opt('--file-token')
                self.items[token].update(blob=source.read_bytes(), modified_time=self.tick())
            else:
                token = self.put(source.name, 'file', opt('--folder-token'), source.read_bytes())
            return {'ok': True, 'data': {'file_token': token}}
        if args[:2] == ['drive', '+download']:
            out = cwd / opt('--output')
            out.write_bytes(self.items[opt('--file-token')]['blob'])
            return {'ok': True, 'data': {'saved_path': opt('--output')}}
        if args[:2] == ['drive', '+create-folder']:
            return {'ok': True, 'data': {'token': self.put(opt('--name'), 'folder', opt('--folder-token'))}}
        if args[:3] == ['api', 'POST', '/open-apis/drive/v1/metas/batch_query']:
            body = json.loads((cwd / opt('--data')[1:]).read_text(encoding='utf-8'))
            return {'ok': True, 'data': {'metas': [{'doc_token': d['doc_token'], 'doc_type': d['doc_type']}
                                                   for d in body['request_docs'] if d['doc_token'] in self.items]}}
        self.test.fail(f'不该收到这条命令：{args}')


class FeishuSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='feishu-sync-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '工作 区'
        (self.root / '.personal/files').mkdir(parents=True)
        self.cloud = FakeFeishu(self)
        self.configure()

    def configure(self, root=None, local_only=None, **feishu):
        conf = dict({'enabled': True, 'folder_token': TARGET, 'auto_sync': False}, **feishu)
        data = {'feishu': conf}
        if local_only is not None:
            data['local_only'] = local_only
        path = (root or self.root) / '.personal/agent.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding='utf-8')

    def write(self, rel, text, root=None):
        path = (root or self.root) / '.personal/files' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def read(self, rel, root=None):
        return ((root or self.root) / '.personal/files' / rel).read_text(encoding='utf-8')

    def run_cli(self, *args, root=None, which='/fake/bin/lark-cli'):
        out = io.StringIO()
        with mock.patch.object(feishu_sync.shutil, 'which', return_value=which), \
                mock.patch.object(feishu_sync.subprocess, 'run', side_effect=self.cloud), \
                mock.patch.object(feishu_sync, 'RETRY_DELAYS', (0, 0)), contextlib.redirect_stdout(out):
            code = feishu_sync.main([*args, '--root', str(root or self.root)])
        return code, out.getvalue()

    def plan(self, root=None):
        code, out = self.run_cli('plan', root=root)
        found = re.search(r'计划编号：([0-9a-f]{12})', out)
        return code, out, found.group(1) if found else None

    def approve(self, root=None):
        """出计划再按计划执行；计划里没有可做的动作（不给编号）时，返回出计划那一步的结果。"""
        code, out, plan_id = self.plan(root)
        if not plan_id:
            return code, out
        return self.run_cli('sync', '--approve', plan_id, root=root)

    def baseline(self, **files):
        for rel, text in files.items():
            self.write(rel, text)
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.cloud.calls.clear()

    def test_local_only_change_uploads_a_new_version_in_place(self):
        """防止只有本机改过的文件不上传，或者另传一份同名文件而不是按 token 原地替换。"""
        self.baseline(**{'a.txt': 'v1', 'sub/dir/b.txt': 'b'})
        token = self.cloud.tree()['a.txt']
        self.write('a.txt', 'v2 本机改的')
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        uploads = self.cloud.ran('drive', '+upload')
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][uploads[0].index('--file-token') + 1], token)
        self.assertEqual(self.cloud.blob('a.txt'), 'v2 本机改的'.encode('utf-8'))
        self.assertEqual(self.cloud.tree()['a.txt'], token)

    def test_cloud_only_change_is_downloaded_and_the_old_local_copy_backed_up(self):
        """防止云端（网页上或另一台设备）改过的文件被本机旧版盖掉，或者取回时本机旧版没留备份。"""
        self.baseline(**{'a.txt': '本机旧版'})
        self.cloud.edit('a.txt', '云端新版'.encode('utf-8'))
        code, out = self.run_cli('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('a.txt'), '云端新版')
        backups = list((self.root / feishu_sync.BACKUPS).rglob('a.txt'))
        self.assertEqual([p.read_text(encoding='utf-8') for p in backups], ['本机旧版'])
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        code, out, _ = self.plan()
        self.assertIn('没有要同步的', out)                     # 取回后登记跟上了，不会再当成本机改过去上传

    def test_both_sides_changed_overwrites_neither_and_keeps_the_cloud_copy(self):
        """防止两边都改过的文件被任何一边悄悄覆盖；冲突时命令必须返回非 0，云端版要留一份给人比较。"""
        self.baseline(**{'a.txt': '上次同步的'})
        self.write('a.txt', '本机改的')
        self.write('另一个.txt', '新文件')                            # 有别的动作，计划才会给编号
        self.cloud.edit('a.txt', '云端改的'.encode('utf-8'))
        code, out, plan_id = self.plan()
        self.assertEqual(code, 1, out)
        self.assertIn('冲突', out)
        code, out = self.run_cli('sync', '--approve', plan_id)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.read('a.txt'), '本机改的')
        self.assertEqual(self.cloud.blob('a.txt'), '云端改的'.encode('utf-8'))
        uploaded = [Path(c[c.index('--file') + 1]).name for c in self.cloud.ran('drive', '+upload')]
        self.assertEqual(uploaded, ['另一个.txt'])                    # 没冲突的照常做，冲突的那个不传
        copy = self.root / feishu_sync.CONFLICTS / 'a.txt'
        self.assertEqual(copy.read_text(encoding='utf-8'), '云端改的')

    def test_resolve_keeps_the_local_version_after_a_conflict(self):
        """防止冲突永远卡住：用户看过云端版、决定以本机为准后，下次同步要能把本机这份传上去。"""
        self.baseline(**{'a.txt': '上次同步的'})
        self.write('a.txt', '本机改的')
        self.cloud.edit('a.txt', '云端改的'.encode('utf-8'))
        self.plan()
        code, out = self.run_cli('resolve', 'a.txt')
        self.assertEqual(code, 0, out)
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cloud.blob('a.txt'), '本机改的'.encode('utf-8'))

    def test_file_missing_from_an_incomplete_listing_is_not_uploaded_again(self):
        """防止大目录列不全时，把列表里漏掉的文件当成云端删了，重新上传出一份重复的。"""
        self.baseline(**{'a.txt': 'v1'})
        self.cloud.hidden.add(self.cloud.tree()['a.txt'])
        self.write('a.txt', 'v2')
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.assertIn('按 token 查还在', out)
        self.assertTrue(self.cloud.ran('api', 'POST', '/open-apis/drive/v1/metas/batch_query'))
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])

    def test_file_really_deleted_in_the_cloud_is_listed_and_kept_locally(self):
        """防止云端删掉的文件又被传回去，或者因为云端删了就把本机这份也删掉。"""
        self.baseline(**{'a.txt': 'v1'})
        del self.cloud.items[self.cloud.tree()['a.txt']]
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.assertIn('云端已经删了', out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        self.assertEqual(self.read('a.txt'), 'v1')

    def test_local_delete_never_deletes_the_cloud_file(self):
        """防止本机删了（或误删、换了设备）就把云端那份也删掉。"""
        self.baseline(**{'a.txt': 'v1', 'b.txt': 'b'})
        (self.root / '.personal/files/a.txt').unlink()
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.assertIn('本机删了、云端还在', out)
        self.assertEqual(self.cloud.blob('a.txt'), b'v1')
        self.assertFalse(self.cloud.ran('drive', '+download'))        # 也不会悄悄从云端取回来
        self.assertFalse((self.root / '.personal/files/a.txt').exists())

    def test_online_documents_are_only_listed(self):
        """防止云端的在线文档被当成要下载的文件（下载不了会整批报错），或者被改动。"""
        self.cloud.add('说明', kind='docx')
        self.cloud.add('表/客户', kind='sheet')
        self.write('a.txt', 'a')
        code, out = self.approve()
        self.assertEqual(code, 0, out)
        self.assertIn('在线文档', out)
        self.assertEqual(self.cloud.ran('drive', '+download'), [])
        self.assertFalse((self.root / '.personal/files/说明').exists())

    def test_private_and_local_only_paths_are_never_uploaded_or_pulled_into(self):
        """防止 .personal/private/、local_only 里的东西和登记表本身被传到云空间，或者云端内容被写进只留本机的目录。"""
        self.configure(local_path='.personal',
                       local_only=['.personal/private/', '.device/', '.local/', '.personal/files/keep-local/'])
        (self.root / '.personal/private').mkdir(parents=True)
        (self.root / '.personal/private/secret.txt').write_text('密', encoding='utf-8')
        self.write('keep-local/x.txt', '只留本机')
        self.write('a.txt', 'a')
        self.cloud.add('private/from-cloud.txt', b'cloud')
        self.assertEqual(self.approve()[0], 0)
        self.assertEqual(self.approve()[0], 0)                       # 第二次同步时登记表已经存在，也不能被传上去
        uploaded = {Path(c[c.index('--file') + 1]).name for c in self.cloud.ran('drive', '+upload')}
        self.assertIn('a.txt', uploaded)
        self.assertFalse(uploaded & {'secret.txt', 'x.txt', 'feishu-map.json'}, uploaded)
        self.assertFalse((self.root / '.personal/private/from-cloud.txt').exists())

    def test_unregistered_same_name_cloud_file_is_compared_not_overwritten(self):
        """防止第一次同步到一个已有内容的文件夹时，不看内容就用本机版本盖掉云端的同名文件。"""
        self.cloud.add('same.txt', '一样'.encode('utf-8'))
        self.cloud.add('diff.txt', '云端版'.encode('utf-8'))
        self.write('same.txt', '一样')
        self.write('diff.txt', '本机版')
        code, out = self.approve()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        self.assertEqual(self.cloud.blob('diff.txt'), '云端版'.encode('utf-8'))
        registry = json.loads((self.root / feishu_sync.MAP).read_text(encoding='utf-8'))
        self.assertEqual([e['path'] for e in registry['files']], ['same.txt'])

    def test_wrong_unknown_or_expired_plan_does_nothing(self):
        """防止没看过计划、编号乱填、计划过期或重复使用时照样往云空间写东西。"""
        self.write('a.txt', 'a')
        _, _, plan_id = self.plan()
        for bad in ('ffffffffffff', '../../x', ''):
            code, out = self.run_cli('sync', '--approve', bad)
            self.assertEqual(code, 2, out)
        code, out = self.run_cli('sync')
        self.assertEqual(code, 2, out)
        path = self.root / feishu_sync.WORK / f'plan-{plan_id}.json'
        plan = json.loads(path.read_text(encoding='utf-8'))
        plan['created_at'] -= feishu_sync.PLAN_VALID_SECONDS + 60
        path.write_text(json.dumps(plan), encoding='utf-8')
        code, out = self.run_cli('sync', '--approve', plan_id)
        self.assertEqual(code, 2, out)
        self.assertIn('过期', out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        _, _, fresh = self.plan()
        self.assertEqual(self.run_cli('sync', '--approve', fresh)[0], 0)
        self.assertEqual(len(self.cloud.ran('drive', '+upload')), 1)
        self.assertEqual(self.run_cli('sync', '--approve', fresh)[0], 2)          # 同一个编号不能再用

    def test_changes_after_the_plan_are_skipped_and_listed(self):
        """防止用户看完计划以后本机又改了的文件，被当成已同意的内容一起传上去。"""
        self.write('a.txt', 'a')
        self.write('b.txt', 'b 看计划时的')
        _, _, plan_id = self.plan()
        self.write('b.txt', 'b 看完计划又改的')
        code, out = self.run_cli('sync', '--approve', plan_id)
        self.assertEqual(code, 1, out)
        self.assertIn('出计划后本机或云端又变了', out)
        uploaded = [Path(c[c.index('--file') + 1]).name for c in self.cloud.ran('drive', '+upload')]
        self.assertEqual(uploaded, ['a.txt'])

    def test_plan_is_bound_to_the_account_it_was_made_with(self):
        """防止出计划以后 CLI 换成了别的飞书账号，资料被存进那个账号。"""
        self.write('a.txt', 'a')
        _, out, plan_id = self.plan()
        self.assertIn('测试账号', out)                                       # 给人核对的是 CLI 当场报的名字
        self.cloud.account = {'appId': 'app-test', 'onBehalfOf': {'openId': 'user-b', 'userName': '另一个账号'}}
        code, out = self.run_cli('sync', '--approve', plan_id)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])

    def test_scheduled_sync_skips_with_zero_when_not_switched_on(self):
        """防止没打开 enabled 或 auto_sync 时定时任务自己去同步，或者因为没打开而报错让定时任务显示失败。"""
        self.write('a.txt', 'a')
        for conf in ({'enabled': False, 'auto_sync': True}, {'enabled': True, 'auto_sync': False}):
            self.configure(**conf)
            code, out = self.run_cli('sync', '--scheduled')
            self.assertEqual((code, out), (0, ''))
        self.assertEqual(self.cloud.calls, [])
        self.assertIn('未开启，跳过', (self.root / feishu_sync.LOG).read_text(encoding='utf-8'))

    def test_scheduled_sync_needs_a_confirmed_account_first(self):
        """防止定时任务在用户从没确认过账号和文件夹时（或 CLI 换了账号以后）自动往云空间写东西。"""
        self.configure(auto_sync=True)
        self.write('a.txt', 'a')
        self.assertEqual(self.run_cli('sync', '--scheduled')[0], 0)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        self.assertIn('还没有确认过', (self.root / feishu_sync.LOG).read_text(encoding='utf-8'))
        self.assertEqual(self.approve()[0], 0)
        self.write('b.txt', 'b')
        self.assertEqual(self.run_cli('sync', '--scheduled')[0], 0)
        self.assertEqual(self.cloud.blob('b.txt'), b'b')
        self.cloud.account = {'appId': 'app-test', 'onBehalfOf': {'openId': 'user-b', 'userName': '另一个账号'}}
        self.write('c.txt', 'c')
        self.cloud.calls.clear()
        self.assertEqual(self.run_cli('sync', '--scheduled')[0], 0)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])

    def test_second_device_downloads_newer_versions_instead_of_pushing_its_old_copy(self):
        """防止另一台设备拿着旧版本，把别的设备刚传上去的新版本当成“本机改了”覆盖回旧版。"""
        other = Path(self.temp.name) / '设备二'
        self.configure(root=other)
        self.baseline(**{'a.txt': 'v1'})
        shutil.copy2(self.root / feishu_sync.MAP, other / feishu_sync.MAP)     # 相当于另一台设备 git pull 拿到登记表
        self.assertEqual(self.run_cli('pull', root=other)[0], 0)
        self.assertEqual(self.read('a.txt', root=other), 'v1')
        self.write('a.txt', 'v2')
        self.assertEqual(self.approve()[0], 0)
        shutil.copy2(self.root / feishu_sync.MAP, other / feishu_sync.MAP)
        self.cloud.calls.clear()
        code, out = self.approve(root=other)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])
        self.assertEqual(self.read('a.txt', root=other), 'v2')
        self.assertEqual(self.cloud.blob('a.txt'), b'v2')

    def test_cloud_rename_is_followed_locally_instead_of_reuploaded(self):
        """防止云端改了名，本机按旧名再传一份，或者把旧名当成云端删了。"""
        self.baseline(**{'旧名.txt': '内容'})
        token = self.cloud.tree()['旧名.txt']
        self.cloud.items[token]['name'] = '新名.txt'
        code, out = self.run_cli('pull')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('新名.txt'), '内容')
        self.assertFalse((self.root / '.personal/files/旧名.txt').exists())
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])

    def test_listing_problems_stop_instead_of_looking_like_an_empty_folder(self):
        """防止列目录出错或分页不全时，把云端当成空的，整批重新上传或误判删除。"""
        self.baseline(**{'a.txt': 'a', 'b.txt': 'b', 'c.txt': 'c'})
        self.write('d.txt', 'd')
        for broken in ('error', 'paging'):
            self.cloud.broken = broken
            code, out = self.run_cli('plan')
            self.assertEqual(code, 2, out)
            self.assertNotIn('计划编号', out)
        self.assertEqual(self.cloud.ran('drive', '+upload'), [])

    def test_missing_setup_gives_next_steps_without_touching_the_cloud(self):
        """防止没装 CLI、没填文件夹、没登录时报一串看不懂的错，或者在没认出账号时就开始列目录、上传。"""
        code, out = self.run_cli('plan', which=None)
        self.assertEqual(code, 2)
        self.assertIn('npm install -g @larksuite/cli', out)
        self.assertIn('auth login', out)
        self.configure(folder_token='')
        code, out = self.run_cli('plan')
        self.assertEqual(code, 2)
        self.assertIn('folder_token', out)
        self.configure()
        self.cloud.account = {'appId': 'app-test'}                  # 只有应用身份、没有用户身份
        code, out = self.run_cli('plan')
        self.assertEqual(code, 2)
        self.assertIn('auth login', out)
        self.assertEqual([c for c in self.cloud.calls if c[0] != 'whoami'], [])


if __name__ == '__main__':
    unittest.main()
