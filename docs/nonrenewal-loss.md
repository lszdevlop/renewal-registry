# 不续费损失（替代应退金额）

## 业务规则

- 仅功能启用之后的单个成员删除入账，不回填过去删除事件。
- Asia/Shanghai 删除日期；月成本按售价 >=800 使用 $127，否则 $27，汇率 6.8。
- 用域名目录中的 Team billing_day，非成员个人续费日。账单日短月压至月末。
- 损失 = 月成本 × [删除当日, 下个 Team 账单日) 剩余天数 / 本 Team 周期实际天数；人民币分四舍五入。
- 在账单日删除视为新周期已预付，计完整月成本。
- 删除时固定快照，永久累计不清零。现有三个盈利指标不变，不扣损失。
- 表格“预计删除损失”仅为预估，不提前入账。缺价格/Team 账单日拒绝删除。

## 实现与恢复

`nonrenewal_loss.py` 是唯一 HTTP/CLI 共用计算及记账模块。
`data/nonrenewal_loss.json` 保存启用时间和全站永久账本。
成员删除与 `loss_events` outbox 在同一个成员 JSON 原子写入，再合并根账本。
根账本写入失败而成员文件提交成功时返回警告，不误报删除失败；刷新明细/整点重算会幂等恢复。
域名改名/删除在清空前对账，失败拒绝清空。8766 使用共享 domain_catalog 模块，更新后也需重启 hub 以加载生命周期保护。

启用：`python3 nonrenewal_loss.py enable`（幂等；不删除成员）
只读查看：`python3 nonrenewal_loss.py summary`
API：`GET /api/nonrenewal-loss` 返回启用时间、count、total_cny 与 events。
缓存：`GET /api/finance-summary` 返回 `totals.nonrenewal_loss`、顶层 `nonrenewal_count` 和 `nonrenewal_enabled_at`。
缓存正常读用文件 stat 判定版本；仅变化后对账，整点和写操作仍使用原共享刷新线程。

## 测试与部署

所有 `tests/test_*.py` 直接逐个执行；24 个脚本的隔离验收报告位于工作区 `renewal-loss-sandbox/evidence/final-all-tests-green.json`。
`test_nonrenewal_*` 覆盖 HTTP/CLI、日历、并发重试、写入失败恢复、域生命周期、前端真实 JS、缓存与审查回归。
不要在生产运行旧 `tests/run_functional_tests.py` / `tests/run_dynamic_domain_http_sandbox.py`。
部署前先备份，再提交（ExecStartPre 禁止脏工作树），推送后重启。不要将 sandbox 的 data/ 或任何测试成员复制到生产。
保留启动时生成的真实启用时间，不硬编码历史日期。
