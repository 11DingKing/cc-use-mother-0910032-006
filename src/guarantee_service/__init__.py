"""积分交易担保风控服务端。

以不可变分录（append-only ledger）登记信用额度、保证金批次、订单敞口与风控规则；
受理订单时计算并冻结可用担保，成交后按交割进度释放；追加担保、部分违约、
监管降额与并发下单都通过分录处理，接口可解释每笔订单的额度占用与拒绝原因，
幂等重试不会扩大敞口。
"""
from .ledger import Ledger
from .service import GuaranteeService, ServiceError

__all__ = ["Ledger", "GuaranteeService", "ServiceError"]
__version__ = "1.0.0"
