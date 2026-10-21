"""业务能力注册：语言参数、执行权限和结果依据共享同一份定义。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

Effect = Literal["read", "preview", "confirm", "auto"]


@dataclass(frozen=True)
class Capability:
    name: str
    input_model: type[BaseModel]
    description: str
    domain: str
    effect: Effect

    @property
    def requires_confirmation(self):
        return self.effect == "confirm"

    @property
    def handler(self):
        return "_" + self.name

    def schema(self):
        permission = {"auto": "用户明确要求后自动执行非资金变更", "read": "只读查询", "preview": "保存预演，不执行资金变更", "confirm": "必须由用户确认后执行"}[self.effect]
        return {"type": "function", "function": {
            "name": self.name,
            "description": f"[{self.domain}；{permission}] {self.description}",
            "parameters": self.input_model.model_json_schema(),
        }}


class CapabilityRegistry:
    def __init__(self):
        self._items = {}

    def register(self, capability):
        if capability.name in self._items:
            raise ValueError(f"Duplicate capability: {capability.name}")
        if capability.effect not in ("read", "preview", "confirm", "auto"):
            raise ValueError("Invalid capability effect")
        self._items[capability.name] = capability

    def __getitem__(self, name):
        return self._items[name]

    def __contains__(self, name):
        return name in self._items

    def schemas(self):
        return [item.schema() for item in self._items.values()]


def business_result(*, status, facts, references, calculation, answer, display, limitations=()):
    """新能力的标准结果：事实与来源可核对，clarification 不代表业务已执行。"""
    if status not in ("verified", "needs_clarification"):
        raise ValueError("Unsupported query outcome")
    return {
        "outcome": {"status": status, "facts": facts, "limitations": list(limitations)},
        "evidence": {"source": "模拟银行数据库", "references": references, "calculation": calculation},
        "answer": answer,
        "display": display,
    }
