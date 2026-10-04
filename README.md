# 交易订单担保风控

本项目维护交易订单担保风控的领域约定、角色边界与样例数据，并提供完整的担保风控服务端：登记信用额度、保证金批次、订单敞口和风控规则，受理订单时计算并冻结可用担保，成交后按交割进度释放；追加担保、部分违约、监管降额和并发下单全部通过不可变分录处理，接口可解释每笔订单占用了哪些额度以及拒绝原因，幂等重试不会扩大敞口。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/guarantee_service/`：担保风控服务端（分录账簿 + 应用服务 + HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动服务端。
- `tests/`：契约完整性、领域行为与 HTTP 端到端回归测试。

## 设计

### 不可变分录账簿

所有状态变更（登记、冻结、释放、罚没、降额……）都先追加一条不可变分录（`Ledger.append`），再应用到内存投影；分录同步写入 JSONL 文件，重启后按序重放即可恢复完全一致的状态与幂等索引。分录类型：

| 分录 | 含义 |
| --- | --- |
| `credit_line_registered` / `credit_line_adjusted` | 登记信用额度；追加担保（`topup`）、监管降额（`regulatory_cut`）、更正 |
| `margin_batch_registered` / `margin_batch_adjusted` | 登记保证金批次；追加（`topup`）、提取可用部分（`withdrawal`） |
| `risk_rule_registered` | 登记风控规则新版本（按 `rule_id` 递增版本号） |
| `order_accepted` / `order_rejected` | 订单受理并冻结担保；或记录结构化拒绝原因（同样不可变、可审计） |
| `delivery_recorded` | 按交割进度释放担保 |
| `default_recorded` | 部分违约，按比例罚没冻结担保（保证金本金划转清算） |
| `order_cancelled` | 撤销未履约部分，释放全部剩余冻结 |

### 关键不变量

- **担保批次余额**：批次余额 = 本金 − 罚没；可用 = 余额 − 冻结，提取不得超过可用部分。
- **订单敞口计算**：名义金额 = 数量 × 价格；卖方敞口 = 名义金额 × 敞口系数（冻结卖方信用额度），买方保证金 = 名义金额 × 保证金率（冻结买方批次），冻结要求一律向上取整。
- **交割释放分录**：累计释放 = 冻结 × 累计交割 / 订单总量，收官时释放全部剩余，任意交割/违约序列下每笔冻结 = 已释放 + 已罚没，无尾差。
- **风险拒绝解释**：拒绝原因结构化（`code` + `message` + `details`），并落成 `order_rejected` 分录。

### 幂等与并发

- 每个写接口接受 `request_id`；订单接口强制要求。同一 `request_id` 的重试重放首次分录的结果（响应由分录纯函数生成，逐字节一致），不会重复冻结、不会扩大敞口；同一 `request_id` 携带不同报文则返回 `409 REQUEST_ID_CONFLICT`。
- “检查可用担保 + 追加冻结分录”在账簿锁内完成；多线程 HTTP 服务下并发下单不会透支同一笔额度（测试：16 线程抢 10 笔额度，恰好成交 10 笔）。
- 监管降额不改写历史分录：降额后若冻结超过新限额，额度进入 `breached` 状态并阻断新订单，追加担保或交割释放后自动解除。

## 接口

启动：`python3 tools/run_server.py --host 127.0.0.1 --port 8080 --ledger data/ledger.jsonl`（`--ledger :memory:` 仅内存运行）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/credit-lines` | 登记信用额度 `{line_id, owner_id, limit}` |
| GET | `/credit-lines/{id}` | 额度视图：限额/冻结/可用/击穿/被哪些订单占用 |
| POST | `/credit-lines/{id}/adjustments` | 追加担保或监管降额 `{delta, reason, request_id?}` |
| POST | `/margin-batches` | 登记保证金批次 `{batch_id, owner_id, amount}` |
| GET | `/margin-batches/{id}` | 批次视图：余额/冻结/可用/罚没累计 |
| POST | `/margin-batches/{id}/adjustments` | 追加或提取保证金 `{delta, reason, request_id?}` |
| POST | `/risk-rules` | 登记风控规则新版本 `{rule_id, margin_rate, exposure_rate, max_order_notional, max_seller_utilization}` |
| GET | `/risk-rules` | 当前生效规则列表 |
| POST | `/orders` | 受理订单 `{request_id, order_id?, seller_id, buyer_id, quantity, price, rule_id?}`，返回冻结明细或拒绝原因 |
| GET | `/orders` | 订单列表（`?status=&owner_id=`） |
| GET | `/orders/{id}` | 订单解释：额度占用、释放/罚没、拒绝原因、关联分录 |
| POST | `/orders/{id}/deliveries` | 交割 `{quantity, request_id?}`，按比例释放 |
| POST | `/orders/{id}/defaults` | 部分违约 `{quantity, reason?, request_id?}`，按比例罚没 |
| POST | `/orders/{id}/cancellation` | 撤销订单，释放剩余冻结 |
| GET | `/accounts/{owner_id}/summary` | 主体汇总：额度、批次、在途订单、合计敞口 |
| GET | `/ledger` | 审计分录流（`?after_seq=&limit=`） |
| GET | `/health` | 健康检查 |

示例：

```bash
curl -s localhost:8080/credit-lines -d '{"line_id":"CL-S1","owner_id":"seller-1","limit":"1000.00"}'
curl -s localhost:8080/margin-batches -d '{"batch_id":"MB-B1","owner_id":"buyer-1","amount":"500.00"}'
curl -s localhost:8080/orders -d '{"request_id":"r1","seller_id":"seller-1","buyer_id":"buyer-1","quantity":10,"price":"10.00"}'
curl -s localhost:8080/orders/<order_id>/deliveries -d '{"quantity":4}'
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
