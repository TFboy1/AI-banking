from pydantic import BaseModel,ConfigDict,Field
from typing import Literal

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,str_strip_whitespace=True)

class BirthdayContext(Input):
    keyword:str=Field(default='',max_length=100,description='姓名、备注、联系人编号或关系；老婆/老公等称呼按联系人关系爱人查询，多人必须澄清')

class BirthdayPreview(Input):
    contact_id:str=Field(min_length=1,max_length=100,description='先get_birthday_context查询真实人员编号；不是银行卡编号')
    budget_yuan:str=Field(pattern=r'^(0|[1-9]\d{0,8})(\.\d{1,2})?$')
    account_id:str=Field(default='daily',min_length=1,max_length=100,description='预算预留来源，默认日常账户；预览及确认卡明确展示，不代表扣款')
    event_date:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$',description='用户明确指定时填写生日日期，必须与联系人生日匹配；未指定由后台计算下一次生日')
    occasion_month:str|None=Field(default=None,pattern=r'^\d{4}-(0[1-9]|1[0-2])$',description='用户限定今年几月或下个月时填写YYYY-MM，后台核对，不能忽略月份约束')
    remind_days:int=Field(default=2,ge=0,le=60)
    remind_time:str=Field(default='09:00',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')

class BirthdayCreate(BirthdayPreview):
    plan_id:str|None=Field(default=None,max_length=100,description='后台生成，无需模型填写')

class BirthdayList(Input):
    pass

class BirthdayManage(Input):
    plan_id:str=Field(min_length=1,max_length=100)
    version:int=Field(ge=1)
    action:Literal['pause','resume','cancel']
