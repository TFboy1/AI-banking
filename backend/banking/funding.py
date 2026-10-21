"""付款账户必须有用户本次选择的依据，不能取模型默认值。"""

import re


def _aliases(account):
    name = account["name"]
    aliases = {account["id"], name, name.removesuffix("账户")}
    bank, separator, tail = name.partition(" · 尾号")
    if separator and re.fullmatch(r"\d{4}", tail):
        aliases.add(bank)
        aliases.add(bank.removeprefix("中国"))
        short_names = {"中国农业银行": "农行", "中国工商银行": "工行", "中国建设银行": "建行",
                       "中国银行": "中行", "交通银行": "交行", "招商银行": "招行",
                       "中国邮政储蓄银行": "邮储银行"}
        if bank in short_names:
            aliases.add(short_names[bank])
        aliases.add("尾号" + tail)
    return aliases


def chosen_source(text, accounts, followup=False):
    choices = []
    for account in accounts:
        aliases = _aliases(account)
        for alias in aliases:
            escaped = re.escape(alias)
            direct = re.search(rf"(?:从|用|由)\s*(?:我的)?{escaped}|(?:付款|转出|出款)账户(?:选择|选|是|用|：|:)?\s*{escaped}|{escaped}\s*(?:付款|转出|扣款|出款)", text)
            short = followup and re.fullmatch(rf"(?:选|选择|用)?\s*{escaped}(?:转|转出|吧)?[。！!]?", text.strip())
            # A bank name identifies a funding card only in a source phrase.
            # Restrict an explicitly supplied tail within that phrase before
            # accepting a bank alias, so two cards at one bank stay ambiguous.
            source = re.search(rf"(?:从|用|由)\s*(?:我的)?{escaped}(?P<details>.*?)(?=往|向|给|到|转|支付|付款|扣款|提现|充值|[，。；]|$)", text)
            if source:
                tail = re.search(r"尾号\s*(\d{4})(?!\d)", source.group(0))
                if tail and not account["name"].endswith("尾号" + tail.group(1)):
                    continue
            if direct or short or source:
                choices.append(account["id"])
                break
    return choices[0] if len(set(choices)) == 1 else None
