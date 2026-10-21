"""会话对象依据校验，可由订阅、联系人等业务提供自己的名称和别名。"""


def grounded_entities(entities, user_messages):
    """最近一次明确提及优先；助手展示的整张数据表不构成用户选择。"""
    for offset, message in enumerate(reversed(user_messages)):
        text = message["text"]
        mentioned = [entity["id"] for entity in entities if any(
            alias and alias in text for alias in [entity["id"], entity["name"], *entity.get("aliases", [])]
        )]
        if mentioned:
            return set(mentioned) if offset == 0 or len(mentioned) == 1 else set()
    return set()
