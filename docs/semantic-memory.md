# Local semantic memory / 本地语义记忆

The optional semantic-memory layer helps an agent find relevant history without loading every private file into a conversation. The Markdown and text files under `.personal/` remain the source of truth; the SQLite database is only a rebuildable search cache.

可选的语义记忆层让 Agent 无需把所有私人文件塞进当前对话，就能找到可能相关的历史。`.personal/` 下的 Markdown 和文本仍是权威来源，SQLite 数据库只是可重建的搜索缓存。

## Install and use / 安装与使用

```bash
python -m pip install -r requirements-memory.txt
python tools/init_workspace.py
python tools/memory_index.py
python tools/memory_recall.py "What did we decide about the launch?"
python tools/memory_recall.py "之前对发布做了什么决定？" --json
```

The first indexing or recall run may download `BAAI/bge-small-zh-v1.5`. After the model is available locally, these scripts create embeddings and search them on the device; they do not upload private text. Re-run the indexer after changing memory files. Unchanged files are skipped, and deleted source files are removed from the index.

第一次索引或召回可能下载 `BAAI/bge-small-zh-v1.5`。模型保存在本机后，脚本在设备上生成向量并检索，不上传私人文本。私人记忆文件变化后重新运行索引；未变化文件会跳过，已经删除的来源会从索引移除。

## Trust boundary / 信任边界

- Recall results are leads, not verified conclusions. Open the cited source and check whether it is current.
- `.local/memory_index.db` stores plaintext excerpts and vectors. Keep it private, out of Git, and out of unencrypted sync.
- Each device can rebuild its own index, so computer-specific absolute paths never need to be shared.
- The scripts scan supported text files under `.personal/` and skip directories named `secrets`.

- 召回结果只是线索，不是已经核验的结论；必须打开引用来源确认是否仍然有效。
- `.local/memory_index.db` 同时保存原文片段和向量，必须保持私有，不进入 Git，也不进入未加密同步。
- 每台设备都可以重建自己的索引，因此无需共享电脑特有的绝对路径。
- 脚本扫描 `.personal/` 下支持的文本文件，并跳过名为 `secrets` 的目录。
