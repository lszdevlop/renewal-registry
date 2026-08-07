# Claude Team 续费登记系统

用于登记 Team 成员的**用户名 / 邮箱 / 每月续费日 / 续费价格**，支持：

1. 手动维护续费日与价格  
2. 定期从 Claude 导出（`users.json` 或整个导出目录）**同步补充成员**  
3. 不定期标记**当月已续费**  
4. 看板按续费日 / 价格 / 当月缴费排序，以及盈利 / 应退金额汇总  

## 盈利统计口径

- **不封号·单日**：所有可计算用户的 `(售价 − 席位成本) / 30` 之和。
- **不封号·累计**：按每位用户固定的 `billing_day` 分别计算。起点为最近一次续费日，续费日当天计第 1 天；累计公式为 `(售价−成本)/30 × min(已用天数,30)`，到下一个续费日该用户重新从第 1 天计算。31 号在没有 31 号的月份压到月末。
- **总累计盈利**：跨周期长期毛利估算。从用户录入时间（优先 `created_at`，否则最早缴费记录）回推到所在续费周期起点，再累计至今天。由于系统没有历史价格流水，历史部分统一使用当前售价和当前成本规则回溯。
- **应退金额合计**：封号时对客户退款的参考值，按最近实缴金额（没有则售价）× 当前周期剩余天数 / 30。

顶部不再计算或显示“封号·单日 / 封号·累计”。盈利周期最多计 30 天；即使两个续费日之间实际经过 31 天，盈利也只计 30 天。续费周期同时用于应退金额和总累计盈利的历史起点对齐。

## 目录

```
renewal-registry/
  data/domains/<domain>/members.json  # 多域主数据
  data/members.json                   # 默认域镜像
  renewal_cli.py                      # 命令行工具
  server.py                           # 看板 HTTP + 写 API
  index.html                          # 网页看板
  README.md
```

## 快速开始

```bash
cd /root/workspace/renewal-registry

# 看板（终端）
python3 renewal_cli.py board

# 列表
python3 renewal_cli.py list

# 给成员填续费日与价格
python3 renewal_cli.py set CHUJUN --day 8 --price 100
python3 renewal_cli.py set moshaobo --day 10 --price 80

# 批量默认值并填充空字段
python3 renewal_cli.py defaults --day 1 --price 100 --apply

# 标记 2026-07 已续费
python3 renewal_cli.py paid 2026-07 CHUJUN moshaobo Shirley --note 微信

# 查看未缴
python3 renewal_cli.py unpaid 2026-07

# 从 Claude 导出同步新成员（zip / 目录 / users.json 均可）
python3 renewal_cli.py sync /path/to/data-...-batch-0000.zip
python3 renewal_cli.py sync /path/to/data-...-batch-0000
python3 renewal_cli.py sync /path/to/users.json --mark-missing

# 删除成员（永久）
python3 renewal_cli.py remove ash yuyu

# 导出 CSV
python3 renewal_cli.py export-csv
```

## 网页看板

```bash
cd /root/workspace/renewal-registry
python3 server.py
# http://127.0.0.1:8765/
```

看板顶部 **「导入导出包」** 可直接选 Claude 下载的 `.zip`（或单独 `users.json`），会调用 `POST /api/sync-export` 自动合并成员，**保留**已有续费日/价格/缴费记录。请通过 `server.py` / systemd 访问以支持写入。

智能分域规则：明确命中实时 Team 域的邮箱按自身后缀分域；Gmail、Outlook 等公共/共享邮箱继承整个导出包的所属 Team 域。包所属域优先由 `admin@<实时域>` 判断，其次使用文件名中的域名，再其次使用包内实时域邮箱的多数票。这样同一 Team 中使用公共邮箱的成员不会错误落入默认域。

表头可点 **续费日 / 价格 / 当月缴费** 排序（互斥）。

## 和你的协作方式（对应需求）

| 你做什么 | 我 / CLI / 看板做什么 |
|---|---|
| 上传新的 Claude 导出 zip | 看板「导入导出包」或 `sync /path/to.zip` 补全新增成员，保留原有 day/price/缴费记录 |
| 告诉我「本月谁已续费」 | `paid YYYY-MM 用户1 用户2...` |
| 告诉我每人几号、多少钱 | `set 用户 --day N --price X` |

## 字段说明

| 字段 | 含义 |
|---|---|
| `username` / `email` | 成员名与邮箱 |
| `billing_day` | 每月续费日（1–31，手动填） |
| `price` | 续费价格（手动填） |
| `payments[]` | 按月缴费记录：`{month, paid, amount, note, recorded_at}` |

> 说明：需求里的「治疗」按上下文理解为「数据」（Claude 导出）。若你指的是别的表，再说一下格式即可扩展同步器。
