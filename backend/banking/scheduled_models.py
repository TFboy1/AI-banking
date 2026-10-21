from pydantic import BaseModel,ConfigDict,Field

class ScheduleTransfer(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    recipient: str=Field(min_length=1,max_length=100,description='已查询并唯一确认的联系人银行卡编号，或本人目标账户编号；不能猜测同名或多卡')
    amount_yuan: str=Field(pattern=r'^(0|[1-9]\d{0,8})(\.\d{1,2})?$')
    execute_at: str=Field(min_length=16,max_length=64,description='用户明确要求的单次执行日期及时间，ISO 8601；无时区按北京时间，不能擅自选择时刻')
    account_id: str=Field(default='daily',min_length=1,max_length=100,description='仅在用户明确指定时填付款账户，否则在提前支付验证窗口选择')
    note: str=Field(default='',max_length=120)
    replaces_id: str|None=Field(default=None,max_length=100,description='修改已有待执行预约时填原预约编号；重新授权成功后才替换原预约')

class CancelSchedule(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    schedule_id: str=Field(min_length=1,max_length=100)
