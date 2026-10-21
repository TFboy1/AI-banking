"""Trusted worker: interpret a bounded Python subset; never exec/eval generated code.

No imports, attributes, files, network, reflection, functions or arbitrary calls are
available to the interpreted program. This is logical isolation, not an OS VM.
"""
import ast,json,sys,time
from types import MappingProxyType

class Rejected(ValueError):pass

class Interpreter:
    def __init__(self,rows):
        self.env={'rows':tuple(MappingProxyType(r) for r in rows)}
        self.steps=0;self.started=time.monotonic();self.allocations=0

    def tick(self):
        self.steps+=1
        if self.steps>600000 or time.monotonic()-self.started>3:raise Rejected('计算超过执行预算。')

    def value(self,v):
        if isinstance(v,int) and abs(v)>10**15:raise Rejected('数值超过限制。')
        if isinstance(v,str) and len(v)>512:raise Rejected('文字超过限制。')
        if isinstance(v,(list,tuple,dict)) and len(v)>12000:raise Rejected('集合超过限制。')
        return v

    def put(self,target,value):
        self.tick();self.allocations+=1
        if self.allocations>150000:raise Rejected('存储操作超过限制。')
        value=self.value(value)
        if isinstance(target,ast.Name):
            if target.id.startswith('_') or target.id=='rows':raise Rejected('不能修改输入数据。')
            if len(self.env)>100:raise Rejected('变量数量超过限制。')
            self.env[target.id]=value
        elif isinstance(target,ast.Subscript):
            container=self.expr(target.value);key=self.expr(target.slice)
            if type(container) is not dict or type(key) not in (str,int):raise Rejected('只允许写入自建字典。')
            # Aggregation cells are scalars; no cycles or nested memory graphs.
            if type(value) not in (int,bool,str,type(None)):raise Rejected('字典值只能是简单数值或文字。')
            if len(container)>=4000 and key not in container:raise Rejected('结果分组过多。')
            container[key]=value
        else:raise Rejected('不支持的赋值目标。')

    def expr(self,node):
        self.tick()
        if isinstance(node,ast.Constant):
            if type(node.value) not in (int,str,bool,type(None)):raise Rejected('只支持整数、文字和布尔值。')
            return self.value(node.value)
        if isinstance(node,ast.Name):
            if node.id.startswith('_') or node.id not in self.env:raise Rejected('使用了未授权变量。')
            return self.env[node.id]
        if isinstance(node,ast.Dict):
            if node.keys or node.values:raise Rejected('请先创建空字典，再逐项汇总。')
            return {}
        if isinstance(node,ast.Subscript):
            container=self.expr(node.value);key=self.expr(node.slice)
            if not isinstance(container,(dict,MappingProxyType,tuple)) or type(key) not in (str,int):raise Rejected('不支持的索引。')
            return container[key]
        if isinstance(node,ast.BinOp):
            a,b=self.expr(node.left),self.expr(node.right)
            if isinstance(node.op,ast.Add) and type(a) is str and type(b) is str:return self.value(a+b)
            if type(a) is not int or type(b) is not int:raise Rejected('计算只能使用整数金额。')
            if isinstance(node.op,ast.Add):v=a+b
            elif isinstance(node.op,ast.Sub):v=a-b
            elif isinstance(node.op,ast.Mult):v=a*b
            elif isinstance(node.op,ast.FloorDiv):v=a//b
            elif isinstance(node.op,ast.Mod):v=a%b
            else:raise Rejected('不支持的运算符。')
            return self.value(v)
        if isinstance(node,ast.UnaryOp):
            v=self.expr(node.operand)
            if isinstance(node.op,ast.Not):return not v
            if type(v) is int and isinstance(node.op,ast.USub):return self.value(-v)
            raise Rejected('不支持的一元运算。')
        if isinstance(node,ast.Compare):
            left=self.expr(node.left)
            for op,rightnode in zip(node.ops,node.comparators):
                right=self.expr(rightnode)
                if isinstance(op,ast.In):ok=left in right
                elif isinstance(op,ast.NotIn):ok=left not in right
                elif isinstance(op,ast.Eq):ok=left==right
                elif isinstance(op,ast.NotEq):ok=left!=right
                elif isinstance(op,ast.Lt):ok=left<right
                elif isinstance(op,ast.LtE):ok=left<=right
                elif isinstance(op,ast.Gt):ok=left>right
                elif isinstance(op,ast.GtE):ok=left>=right
                else:raise Rejected('不支持的比较。')
                if not ok:return False
                left=right
            return True
        if isinstance(node,ast.BoolOp):
            for item in node.values:
                v=self.expr(item)
                if isinstance(node.op,ast.And) and not v:return False
                if isinstance(node.op,ast.Or) and v:return True
            return isinstance(node.op,ast.And)
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and not node.keywords:
            values=[self.expr(n) for n in node.args]
            if node.func.id=='len' and len(values)==1 and isinstance(values[0],(tuple,dict,str)):return len(values[0])
            if node.func.id=='get' and len(values)==3 and isinstance(values[0],(dict,MappingProxyType)):
                return values[0].get(values[1],values[2])
            raise Rejected('只允许len和get这两个受限函数。')
        raise Rejected('不支持的表达式；禁止导入、属性访问和任意函数。')

    def block(self,body):
        for node in body:
            self.tick()
            if isinstance(node,ast.Assign) and len(node.targets)==1:self.put(node.targets[0],self.expr(node.value))
            elif isinstance(node,ast.For) and isinstance(node.target,ast.Name) and not node.orelse:
                items=self.expr(node.iter)
                if not isinstance(items,(tuple,dict)) or len(items)>12000:raise Rejected('循环只能遍历受限输入。')
                for item in items:self.put(node.target,item);self.block(node.body)
            elif isinstance(node,ast.If):self.block(node.body if self.expr(node.test) else node.orelse)
            else:raise Rejected('只允许赋值、有限for循环和if语句。')

    def run(self,code):
        if len(code)>12000:raise Rejected('代码过长。')
        tree=ast.parse(code)
        if sum(1 for _ in ast.walk(tree))>600:raise Rejected('代码结构过于复杂。')
        # Validate the entire AST, including branches which would not run.
        forbidden=(ast.Import,ast.ImportFrom,ast.Attribute,ast.While,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda,ast.With,ast.Try,ast.ListComp,ast.DictComp,ast.SetComp,ast.GeneratorExp)
        if any(isinstance(n,forbidden) for n in ast.walk(tree)):raise Rejected('代码包含禁止的操作。')
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and (not isinstance(node.func,ast.Name) or
                    node.func.id not in {'len','get'} or node.keywords):
                raise Rejected('只允许len和get这两个受限函数，包括未执行分支。')
        self.block(tree.body)
        result=self.env.get('result')
        if type(result) is not dict or any(type(k) is not str or type(v) is not int for k,v in result.items()):raise Rejected('结果必须是分组名称到整数金额的字典。')
        return {'result':result,'steps':self.steps}

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(2_000_001)
        if len(raw)>2_000_000:raise Rejected('输入过大。')
        payload=json.loads(raw.decode('utf-8'))
        output={'ok':True,**Interpreter(payload['rows']).run(payload['code'])}
    except Exception:
        # No generated error text or local paths cross the worker boundary.
        output={'ok':False,'error':'分析代码不符合受限执行规则，或计算超出限制。'}
    sys.stdout.buffer.write(json.dumps(output,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
