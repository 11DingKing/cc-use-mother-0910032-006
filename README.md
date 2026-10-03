# 交易订单担保风控

本项目维护交易订单担保风控的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并明确担保批次余额、订单敞口计算、交割释放分录、风险拒绝解释等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
