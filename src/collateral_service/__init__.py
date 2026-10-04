"""交易订单担保风控服务端。

组件：

- models：领域模型、不可变分录类型、精确十进制序列化
- store：SQLite 只追加账本（触发器禁止 UPDATE/DELETE，SHA-256 哈希链防篡改）
- state：分录回放，重建额度账户、保证金批次与订单敞口
- rules：风控规则评估（额度、保证金、监管状态、单笔上限）
- engine：担保冻结/释放/罚没的分配算法与幂等命令
- server：线程化 HTTP JSON 接口
"""
from __future__ import annotations

from .engine import CollateralEngine, RiskConfig
from .models import DomainError

__all__ = ["CollateralEngine", "RiskConfig", "DomainError"]
__version__ = "0.2.0"
