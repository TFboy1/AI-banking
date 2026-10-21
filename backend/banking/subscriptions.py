"""订阅报价领域：只接收模型解析后的参数，查询事实并进行整月金额计算。"""

from .capabilities import business_result
from .grounding import grounded_entities


def quote_subscriptions(db, args):
    from .bank import table, yuan

    all_items = [dict(row) for row in db.execute("SELECT id,name,amount_minor,status FROM subscriptions")]
    conversation_id = args.pop("_conversation_id", None)
    grounded = None
    if conversation_id:
        history = [dict(row) for row in db.execute(
            "SELECT text FROM (SELECT rowid AS ord,text FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid DESC LIMIT 30) ORDER BY ord", (conversation_id,)
        )]
        entities = [{**item, "aliases": [item["name"].removesuffix("会员")]} for item in all_items]
        grounded = grounded_entities(entities, history)
    selected, ambiguous = [], []
    for reference in args["services"]:
        exact = [item for item in all_items if reference in (item["id"], item["name"])]
        matches = exact or [item for item in all_items if reference in item["name"]]
        if len(matches) != 1:
            ambiguous.append(reference)
        elif matches[0] not in selected:
            if grounded is not None and matches[0]["id"] not in grounded:
                ambiguous.append(reference)
            else:
                selected.append(matches[0])
    limitations = ["当前仅有模拟月费，没有套餐、年付或优惠报价", "沙箱暂不支持重新开通订阅"]
    if not selected or ambiguous:
        answer = "请明确要计算的订阅服务名称；对象缺失、未找到或有多个匹配时不能猜测。"
        return business_result(status="needs_clarification", facts={"unresolved_services": ambiguous},
            references=[], calculation=None, answer=answer,
            display=table("订阅费用待补充", ["需要补充"], [[answer]]), limitations=limitations)
    months = args["months"]
    quotes = [{"subscription_id": item["id"], "name": item["name"], "status": item["status"],
               "monthly_minor": item["amount_minor"], "months": months,
               "estimated_minor": item["amount_minor"] * months} for item in selected]
    total = sum(item["estimated_minor"] for item in quotes)
    answer = "".join(f"{item['name']}当前演示月费 {yuan(item['monthly_minor'])}，{months} 个月按月费估算共 {yuan(item['estimated_minor'])}。" for item in quotes)
    if len(quotes) > 1:
        answer += f"合计 {yuan(total)}。"
    if args["inquire_packages"]:
        answer += "当前模拟数据没有套餐、年付或优惠报价，无法确认是否有套餐。"
    answer += "这里只查询费用，沙箱暂不支持重新开通订阅。"
    return business_result(status="verified",
        facts={"quotes": quotes, "total_minor": total, "package_information": "unavailable"},
        references=[{"table": "subscriptions", "id": item["subscription_id"]} for item in quotes],
        calculation={"formula": "monthly_minor * months", "currency": "CNY", "unit": "minor", "months": months},
        answer=answer, limitations=limitations,
        display=table("订阅费用估算", ["服务", "当前月费", "期限", "估算费用"],
            [[item["name"], yuan(item["monthly_minor"]), f"{months} 个月", yuan(item["estimated_minor"])] for item in quotes],
            note="按当前模拟月费估算，不含未提供的折扣或未来价格变化。",
            metrics=[{"label": "按月费估算合计", "value": yuan(total)}]))



from datetime import date, timedelta


def subscription_record(row):
    result = dict(row)
    # The sandbox models monthly renewal; it has no merchant entitlement API.
    # Keep the paid-period end when future automatic renewals are cancelled.
    try:
        result['expires_on'] = (date.fromisoformat(result['next_charge_date']) - timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        result['expires_on'] = None
    result['expiry_estimated'] = True
    return result
