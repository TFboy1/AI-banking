from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator

class AnalysisData(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    kind:Literal['trend','categories','comparison']='trend'
    start_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$',description='北京时间，包含当天；根据本句或本对话报告范围解析')
    end_date:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$',description='北京时间，包含当天；不能伪造不存在的流水')
    group_by:Literal['day','week','month']='month'
    measure:Literal['expense','income','net']='expense'
    categories:list[str]=Field(default_factory=list,max_length=12,description='按实际账单类别筛选；空数组表示全部日常类别')
    merchant_contains:str|None=Field(default=None,min_length=1,max_length=60,description='用户明确指定的商户或用途关键词，在收款对象和流水说明中匹配；外卖可用外卖关键词，不得将全部餐饮当外卖')
    compare_start:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    compare_end:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    @model_validator(mode='after')
    def comparison(self):
        if self.kind=='comparison' and (not self.compare_start or not self.compare_end):raise ValueError('对比分析必须提供两个完整时间范围。')
        if self.kind!='comparison' and (self.compare_start or self.compare_end):raise ValueError('仅comparison可以提供对比时间。')
        return self

class AnalysisCode(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    dataset_id:str=Field(min_length=1,max_length=80)
    code:str=Field(min_length=1,max_length=12000,description='受限Python代码：输入rows包含bucket、series、amount_minor；以bucket+"|"+series为键累计amount_minor，输出result字典。仅支持赋值、for、if、字典索引、整数运算、len和get。不允许import、属性方法、任意函数、文件、网络、数据库或交易。不要输出Markdown代码围栏。')
