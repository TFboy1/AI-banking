"""联系人目录：人员与电话、银行账户分别保存，详情按需展开。"""

from .bank import BankError
from .db import uid


def person_key(db, identifier):
    row = db.execute("SELECT person_id FROM contact_links WHERE contact_id=?", (identifier,)).fetchone()
    if not row:
        raise BankError("联系人不存在。")
    return row[0]


def contact(db, identifier, reveal=False):
    person_id = person_key(db, identifier)
    row = db.execute("SELECT c.*,d.note FROM contacts c JOIN contact_details d ON d.contact_id=c.id WHERE c.id=?", (person_id,)).fetchone()
    if not row:
        raise BankError("联系人不存在。")
    item = dict(row)
    accounts = [dict(account) for account in db.execute("SELECT c.id,c.bank,c.last4,d.card_number FROM contacts c JOIN contact_details d ON d.contact_id=c.id JOIN contact_links l ON l.contact_id=c.id WHERE l.person_id=? ORDER BY c.rowid", (person_id,))]
    phones = [row[0] for row in db.execute("SELECT phone FROM contact_phones WHERE person_id=? ORDER BY rowid", (person_id,))]
    accounts = [account for account in accounts if account["card_number"]]
    profile = db.execute("SELECT * FROM contact_profiles WHERE person_id=?", (person_id,)).fetchone()
    profile = dict(profile) if profile else {"relationship": "未分类", "birthday": None, "gender": "未设置", "special_attention": False}
    masked_name = item["name"][:1] + "*" * max(1, len(item["name"]) - 1)
    return {"id": person_id, "name": item["name"] if reveal else masked_name,
            "phone": (phones[0] if reveal else "*******" + phones[0][-4:]) if phones else "",
            "phones": phones if reveal else ["*******" + phone[-4:] for phone in phones],
            "card_number": (accounts[0]["card_number"] if reveal else "**** **** **** " + accounts[0]["last4"]) if accounts else "",
            "accounts": [{"contact_id": a["id"], "bank": a["bank"], "card_number": a["card_number"] if reveal else "**** **** **** " + a["last4"]} for a in accounts],
            "bank": item["bank"], "note": item["note"], "revealed": reveal, "simulated": True,
            "relationship": profile["relationship"], "birthday": profile["birthday"], "gender": profile["gender"],
            "special_attention": bool(profile["special_attention"])}


def update_profile(db, identifier, values):
    person_id = person_key(db, identifier)
    db.execute("INSERT OR IGNORE INTO contact_profiles(person_id) VALUES (?)", (person_id,))
    for key in ("relationship", "birthday", "gender", "special_attention"):
        if key in values:
            value = values[key]
            if key == "relationship":
                value = value or "未分类"
            elif key == "gender":
                value = value or "未设置"
            elif key == "special_attention":
                value = int(bool(value))
            db.execute(f"UPDATE contact_profiles SET {key}=? WHERE person_id=?", (value, person_id))
    if "note" in values:
        db.execute("UPDATE contact_details SET note=? WHERE contact_id IN (SELECT contact_id FROM contact_links WHERE person_id=?)", ((values["note"] or "").strip(), person_id))
    return contact(db, person_id)


def check_create(db, values):
    name = values["name"].strip()
    if not name:
        raise BankError("联系人姓名不能为空。")
    if bool(values.get("bank")) != bool(values.get("card_number")):
        raise BankError("添加银行卡时，请同时提供银行名称和卡号。")
    if values.get("bank") and not values["bank"].strip():
        raise BankError("银行名称不能为空。")
    existing = [row[0] for row in db.execute("SELECT DISTINCT l.person_id FROM contacts c JOIN contact_links l ON l.contact_id=c.id WHERE c.name=?", (name,))]
    same = values.get("same_person_id")
    if same:
        if person_key(db, same) not in existing or values.get("new_person"):
            raise BankError("选择的已有联系人与姓名不匹配。")
    elif existing and not values.get("new_person"):
        raise BankError("存在同名联系人，请查询候选并询问是同一个人还是另一个人，不能直接保存。")
    if values.get("card_number") and db.execute("SELECT 1 FROM contact_details WHERE card_number=?", (values["card_number"],)).fetchone():
        raise BankError("该银行卡已在联系人库中。")


def create_person(db, values):
    check_create(db, values)
    if values.get("same_person_id"):
        person_id = person_key(db, values["same_person_id"])
        if values.get("phone") or values.get("card_number"):
            add_details(db, person_id, values.get("phone"), values.get("bank"), values.get("card_number"))
    else:
        person_id = uid("contact_")
        db.execute("INSERT INTO contacts VALUES (?,?,?,?,?)", (person_id, values["name"].strip(), values.get("phone") or "", (values.get("bank") or "").strip(), (values.get("card_number") or "")[-4:]))
        db.execute("INSERT INTO contact_details VALUES (?,?,?)", (person_id, values.get("note") or "", values.get("card_number") or ""))
        db.execute("INSERT INTO contact_links VALUES (?,?)", (person_id, person_id))
        if values.get("phone"):
            db.execute("INSERT INTO contact_phones VALUES (?,?)", (person_id, values["phone"]))
    update_profile(db, person_id, {key: values[key] for key in ("relationship", "birthday", "gender", "special_attention") if values.get(key) is not None})
    if values.get("note") is not None:
        db.execute("UPDATE contact_details SET note=? WHERE contact_id IN (SELECT contact_id FROM contact_links WHERE person_id=?)", (values["note"], person_id))
    return contact(db, person_id)


def add_details(db, identifier, phone=None, bank=None, card_number=None):
    person_id = person_key(db, identifier)
    original = db.execute("SELECT c.*,d.note FROM contacts c JOIN contact_details d ON d.contact_id=c.id WHERE c.id=?", (person_id,)).fetchone()
    if not phone and not card_number:
        raise BankError("请至少填写一个电话或银行卡。")
    if bool(bank) != bool(card_number):
        raise BankError("添加银行卡时请同时填写银行名称和卡号。")
    if card_number and db.execute("SELECT 1 FROM contact_details WHERE card_number=?", (card_number,)).fetchone():
        raise BankError("该银行卡已在联系人库中，请勿重复添加。")
    if phone:
        db.execute("INSERT OR IGNORE INTO contact_phones VALUES (?,?)", (person_id, phone))
    if card_number:
        identifier = uid("contact_")
        db.execute("INSERT INTO contacts VALUES (?,?,?,?,?)", (identifier, original["name"], phone or original["phone"], bank, card_number[-4:]))
        db.execute("INSERT INTO contact_details VALUES (?,?,?)", (identifier, original["note"], card_number))
        db.execute("INSERT INTO contact_links VALUES (?,?)", (identifier, person_id))
    return contact(db, person_id)
