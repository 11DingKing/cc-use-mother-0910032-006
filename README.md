# 交易订单担保风控

积分交易开放账期后，卖方可能在多个订单中重复承诺尚未交割的额度，买方保证金不足时又会把风险传递到清算。本服务用**不可变分录（append-only ledger）**登记信用额度、保证金批次、订单敞口与风控规则，受理订单时计算并冻结可用担保，成交后按交割进度释放；追加担保、部分违约、监管降额、并发下单全部以分录为准，接口能逐笔解释每笔订单占用了哪些额度，重试绝不扩大敞口。

## 设计要点

### 1. 只追加账本与哈希链（`store.py`）

- SQLite `entries` 表只允许 `INSERT`，触发器在任何连接上拒绝 `UPDATE`/`DELETE`；
- 每条分录取前一条的哈希构成 SHA-256 哈希链，`GET /v1/ledger` 可验链，脱库篡改会被 `verify_chain()` 定位到具体分录；
- 所有命令在一个 `BEGIN IMMEDIATE` 事务内"读状态—评估—落账"，提交成功后才更新内存状态，回滚不留痕；
- 内存状态（`state.py`）完全由分录回放得到，重启重建，不存在状态表与分录表漂移。

### 2. 担保模型（`models.py` / `rules.py`）

| 担保方 | 登记对象 | 受理时 | 交割释放 | 违约罚没 |
|---|---|---|---|---|
| 卖方 | 总额度 + 额度行（默认行 `L-DEFAULT` + 专项行，各行之和恒等于总额度） | 按名义金额冻结；指定行优先、其余按行 FIFO | 冻结回池可再用 | 永久消耗授信（限额与冻结同步核减，可用额不回弹） |
| 买方 | 独立保证金批次（先入先出，追加担保=新增批次） | 按 `名义金额 × 保证金率` 冻结批次可用余额 | 批次内冻结转可用 | 批次金额永久核减，赔付清算，不重新可用 |

### 3. 风控规则（每条都在响应里逐规则解释）

1. 交易双方已登记；
2. 卖方信用账户存在且未被监管冻结；
3. 可用额度覆盖订单名义金额（**同一未交割额度不得在多单重复承诺**）；
4. 不超过单笔订单名义金额上限；
5. 同一 `client_token` 无在途重复单；
6. 买方保证金批次可用余额满足所需保证金（**风险不向清算传递**）；
7. 各额度行能构造完整冻结方案。

评估为纯函数（`rules.evaluate`），`POST /v1/orders/preview` 不落账即可看到每条规则的 required/available 与拟占用来源。

### 4. 幂等与重试不扩敞口（`engine.py`）

- 订单以 `client_token` 为幂等键，其余写接口要求 `Idempotency-Key`；
- 幂等命中且请求指纹一致时，命令逻辑完全不执行、不写任何分录，原样返回首次结果；
- 同一键换请求体 → `IDEMPOTENCY_MISMATCH` 409；
- 拒绝也会落 `order_rejected` 分录，因此被拒订单的同 token 重试仍返回那张拒绝单，不会二次评估。

### 5. 交割、部分违约与监管动作

- `settle` 每次事件以**池内剩余未结金额**和**订单剩余数量**为基准按比例分摊（整数分最大余数法，违约先分、释放吸收尾差），多段部分交割的舍入误差不累积，结案时无一分钱残留；全部数量处理完时同一事务出 `order_closed`；
- 撤单：剩余冻结全额退回，占用标记 `refunded`；
- 监管降额是一条 `credit_adjusted` 带符号分录；若历史冻结超过新限额，账户标记 `distressed` 供监管视图跟进，但**绝不回改历史分录**，新订单按新限额评估；
- 监管冻结/解冻为 `credit_blocked` 分录。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/collateral_service/`：担保风控服务端
  - `models.py`：枚举、不可变分录、账户/批次/订单/占用模型（Decimal 金额）
  - `store.py`：SQLite 只追加账本、哈希链、`BEGIN IMMEDIATE` 事务
  - `state.py`：分录回放重建全部账户状态
  - `rules.py`：风控规则评估与担保分配/摊分算法（纯函数）
  - `engine.py`：幂等命令引擎（登记、受理、交割、撤单、降额、冻结）
  - `server.py` / `__main__.py`：线程化 HTTP JSON 接口
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域规则、随机化摊分属性测试、HTTP 端到端与持久化重建。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/parties` | 登记主体（卖方/买方） |
| POST | `/v1/sellers/{id}/credit` | 授予/追加授信（注入默认额度行） |
| POST | `/v1/sellers/{id}/lines` | 登记专项额度行 |
| GET | `/v1/sellers/{id}/credit` | 信用账户（限额/冻结/可用/distressed） |
| POST | `/v1/sellers/{id}/block` | 监管冻结/解冻 |
| POST | `/v1/sellers/{id}/reduce` | 监管降额（可带 `line_reductions`） |
| POST | `/v1/buyers/{id}/margin` | 存入保证金批次（追加担保） |
| GET | `/v1/buyers/{id}/margin` | 批次余额与累计罚没 |
| POST | `/v1/orders/preview` | 不落账预检：逐规则 + 拟占用 + 拒绝原因 |
| POST | `/v1/orders` | 受理订单（评估+冻结原子，`client_token` 幂等） |
| GET | `/v1/orders/{id}` | 订单敞口与逐笔占用解释 |
| POST | `/v1/orders/{id}/settle` | 交割/部分违约，按进度释放 |
| POST | `/v1/orders/{id}/cancel` | 撤单，剩余冻结退回 |
| GET | `/v1/ledger` | 分录与哈希链审计（支持 `?order_id=`） |
| GET | `/healthz` | 健康检查 |

金额一律用字符串（Decimal，两位小数）；写接口需带 `Idempotency-Key`（订单用 body 里的 `client_token`）。

## 快速试用

启动（两种方式任选）：

```bash
pip install -e .          # 可选：安装后可直接使用 python -m collateral_service
python3 -m collateral_service --host 0.0.0.0 --port 8080 --db ledger.db

# 不安装时从仓库根目录运行：
PYTHONPATH=src python3 -m collateral_service --host 0.0.0.0 --port 8080 --db ledger.db
```

端到端示例：

```bash
BASE=http://127.0.0.1:8080
curl -s -XPOST $BASE/v1/parties -H 'Idempotency-Key: p1' -H 'Content-Type: application/json' \
  -d '{"party_id":"S1","name":"卖方","role":"seller"}'
curl -s -XPOST $BASE/v1/parties -H 'Idempotency-Key: p2' -H 'Content-Type: application/json' \
  -d '{"party_id":"B1","name":"买方","role":"buyer"}'
curl -s -XPOST $BASE/v1/sellers/S1/credit -H 'Idempotency-Key: g1' \
  -H 'Content-Type: application/json' -d '{"amount":"100000"}'
curl -s -XPOST $BASE/v1/buyers/B1/margin -H 'Idempotency-Key: m1' \
  -H 'Content-Type: application/json' -d '{"amount":"30000","batch_id":"MB-1"}'

# 预检：逐规则解释这笔单要占用哪些额度/批次
curl -s -XPOST $BASE/v1/orders/preview -H 'Content-Type: application/json' -d '{
  "seller_id":"S1","buyer_id":"B1","quantity":"1000",
  "unit_price":"100","margin_rate":"0.2"}'

# 受理：名义 100000，冻结卖方额度 100000、买方保证金 20000
curl -s -XPOST $BASE/v1/orders -H 'Content-Type: application/json' -d '{
  "seller_id":"S1","buyer_id":"B1","quantity":"1000",
  "unit_price":"100","margin_rate":"0.2","client_token":"ORD-001"}'

# 交割 700 + 部分违约 100：释放对应担保、罚没违约部分
curl -s -XPOST $BASE/v1/orders/ORD-001/settle -H 'Idempotency-Key: s1' \
  -H 'Content-Type: application/json' -d '{"delivered_qty":"700","defaulted_qty":"100"}'

# 同一幂等键重试：返回与首次完全一致，不新增分录、不扩大敞口
curl -s -XPOST $BASE/v1/orders/ORD-001/settle -H 'Idempotency-Key: s1' \
  -H 'Content-Type: application/json' -d '{"delivered_qty":"700","defaulted_qty":"100"}'
```

## 验证

```bash
# 全部测试（契约 + 领域规则 + 随机化摊分属性测试 + HTTP 端到端 + 持久化重建）
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```

## 关键不变量

- 卖方 `frozen ≤ total_limit`，且 `total_limit == Σ 额度行限额`；
- 保证金批次 `frozen ≤ amount`，受理只从 `amount - frozen` 中冻结；
- 每笔订单的每条占用都可追溯到具体额度行/保证金批次，释放与罚没逐笔入账；
- 释放 + 罚没不超过该笔占用冻结金额；罚没永久消耗额度/保证金，不重新可用；
- 任意重试不新增分录，并发下单在写锁下串行，总额度绝不超卖；
- 哈希链完整，历史分录不可变。
