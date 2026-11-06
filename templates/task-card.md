# 任务标题（一句话写清要做什么）

<!-- 复制到 .personal/logs/task-cards/YYYY-MM-DD_主题.md 后填写。
     agent.json 里 require_task_card 为 true 时，提交前 tools/workflow_gate.py 会逐项核对下面这些行，格式不要改。 -->

- 实际写入时间：YYYY-MM-DD HH:mm（时区）
- 记录执行者：哪个 AI / 哪个人实际写的
- 来源：谁提出的需求，原话是什么
- 任务标记：TASK-主题-YYYYMMDD
- 工作日志：.personal/logs/work-logs/YYYY-MM-DD_主题.md
- 范围：改哪些文件、不改哪些
- 备份：改之前的版本放在哪（Git 可回退的写“Git”）
- 验证结果：通过
- [ ] G0 接手：读 AGENTS.md、current-state、相关项目入口，确认当前状态
- [ ] G1 探查：读现有代码和资料，弄清楚要改的地方
- [ ] G2 方案与备份：说清方案和影响，需要时先备份
- [ ] G3 执行：按方案改
- [ ] G4 验证：实际跑过的测试和结果；没验证的写明
- [ ] G5 记录：工作日志、current-state、未闭环事项
- [ ] G6 提交：`python tools/sync.py push --task-card 本文件 --message "说明" 文件…`
