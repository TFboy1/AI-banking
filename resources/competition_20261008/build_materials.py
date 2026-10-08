"""从已核对的项目事实生成参赛 Word 材料和不含凭证的源码包。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(__file__).resolve().parent
OUT = ROOT / "outputs" / "bankmate_参赛材料_20261008"
EVIDENCE = json.loads((WORK / "evidence.json").read_text(encoding="utf-8"))
TEAM = "哈基米摸南北绿豆"
PROJECT = "bankmate"
MEMBERS = [("汤范博文", "队长"), ("郭展志", "队员"), ("陈羽鑫", "队员"), ("林旸昊", "队员"), ("李志源", "队员")]
DATE = "2026年10月8日"
WIDTH = 17.2


def font_style(style, size, bold=False, east_asia="微软雅黑"):
    style.font.name = "Arial"
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.italic = False
    style.font.color.rgb = RGBColor(0, 0, 0)
    rf = style.element.get_or_add_rPr().get_or_add_rFonts()
    rf.set(qn("w:eastAsia"), east_asia)
    rf.set(qn("w:ascii"), "Arial")
    rf.set(qn("w:hAnsi"), "Arial")
    for item in style.element.get_or_add_rPr().xpath("w:color"):
        for key in list(item.attrib):
            if "theme" in key.lower():
                del item.attrib[key]


def new_doc(title):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin, section.bottom_margin = Cm(1.9), Cm(1.8)
    section.left_margin, section.right_margin = Cm(2.1), Cm(2.1)
    section.header_distance, section.footer_distance = Cm(0.8), Cm(0.8)
    for grid in section._sectPr.xpath("w:docGrid"):
        section._sectPr.remove(grid)
    for border in doc.styles.element.xpath(".//w:pBdr"):
        border.getparent().remove(border)
    for name, size, bold in [("Normal", 11, False), ("Title", 24, True), ("Subtitle", 12, False), ("Heading 1", 16, True), ("Heading 2", 12.5, True), ("Heading 3", 11, True), ("Caption", 9.5, False)]:
        font_style(doc.styles[name], size, bold)
    normal = doc.styles["Normal"].paragraph_format
    normal.line_spacing = Pt(17.5)
    normal.space_after = Pt(6)
    normal.widow_control = True
    snap = OxmlElement("w:snapToGrid")
    snap.set(qn("w:val"), "0")
    doc.styles["Normal"].element.get_or_add_pPr().append(snap)
    for name in ["Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"]:
        pf = doc.styles[name].paragraph_format
        pf.keep_with_next = True
        pf.line_spacing = 1.1
        pf.space_after = Pt(8)
        pf.space_before = Pt(10 if name.startswith("Heading") else 0)
    doc.styles["Title"].paragraph_format.space_after = Pt(10)
    header = section.header.paragraphs[0]
    header.add_run("bankmate  " + title)
    for run in header.runs:
        run.font.size, run.font.color.rgb = Pt(9), RGBColor(0, 0, 0)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("第 ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    footer.add_run(" 页")
    for run in footer.runs:
        run.font.size = Pt(9)
    doc.core_properties.title = "bankmate " + title
    doc.core_properties.author = TEAM
    doc.core_properties.subject = "银行 AI 智能体 AI Banking Agent 参赛材料"
    doc.core_properties.keywords = "bankmate,Vue,FastAPI,确认执行,现金流,银行沙箱"
    doc.add_paragraph("bankmate " + title, "Title")
    doc.add_paragraph("银行 AI 智能体参赛材料", "Subtitle")
    p(doc, f"队伍名称：{TEAM}    学校：暨南大学")
    p(doc, f"赛题方向：银行 AI 智能体（AI Banking Agent）")
    p(doc, f"材料日期：{DATE}    项目版本：0.1.0")
    return doc


def p(doc, text, bold_lead=None, small=False):
    para = doc.add_paragraph()
    if bold_lead and text.startswith(bold_lead):
        para.add_run(bold_lead).bold = True
        para.add_run(text[len(bold_lead):])
    else:
        para.add_run(text)
    if small:
        for run in para.runs:
            run.font.size = Pt(9.5)
    return para


def h(doc, text, level=1):
    return doc.add_paragraph(text, f"Heading {level}")


def page(doc, heading):
    h(doc, heading).paragraph_format.page_break_before = True


def table(doc, headers, rows, widths, center_cols=()):
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    pr = tbl._tbl.tblPr
    tw = pr.find(qn("w:tblW"))
    tw.set(qn("w:w"), str(int(sum(widths) / 2.54 * 1440)))
    tw.set(qn("w:type"), "dxa")
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        item = OxmlElement("w:" + side)
        item.set(qn("w:val"), "single")
        item.set(qn("w:sz"), "4")
        item.set(qn("w:color"), "D9D9D9")
        borders.append(item)
    pr.append(borders)
    for column, width in zip(tbl.columns, widths):
        column.width = Cm(width)
    for ri, values in enumerate([headers] + list(rows)):
        row = tbl.rows[0] if ri == 0 else tbl.add_row()
        no_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(no_split)
        if ri == 0:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for ci, value in enumerate(values):
            cell = row.cells[ci]
            cell.width = Cm(widths[ci])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            cell.text = str(value)
            tcp = cell._tc.get_or_add_tcPr()
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "233C56" if ri == 0 else ("F1F5F8" if ri % 2 == 0 else "FFFFFF"))
            tcp.append(shade)
            margins = OxmlElement("w:tcMar")
            for side, value in [("top", 80), ("bottom", 80), ("left", 110), ("right", 110)]:
                node = OxmlElement("w:" + side)
                node.set(qn("w:w"), str(value))
                node.set(qn("w:type"), "dxa")
                margins.append(node)
            tcp.append(margins)
            for para in cell.paragraphs:
                para.paragraph_format.line_spacing = Pt(15)
                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.space_after = Pt(2)
                para.paragraph_format.keep_with_next = ri == 0
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER if ci in center_cols else WD_ALIGN_PARAGRAPH.LEFT
                for run in para.runs:
                    run.font.size = Pt(10)
                    run.font.bold = ri == 0
                    run.font.color.rgb = RGBColor(255, 255, 255) if ri == 0 else RGBColor(0, 0, 0)
    blank = doc.add_paragraph().paragraph_format
    blank.line_spacing, blank.space_after = Pt(3), Pt(3)
    return tbl


def code(doc, lines):
    for line in lines.splitlines():
        para = doc.add_paragraph()
        para.paragraph_format.space_after = Pt(2)
        para.paragraph_format.line_spacing = 1.12
        run = para.add_run(line or " ")
        run.font.name, run.font.size = "Consolas", Pt(9)
        run._r.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    blank = doc.add_paragraph().paragraph_format
    blank.line_spacing, blank.space_after = Pt(3), Pt(3)


def formula(doc, fragments):
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    math = OxmlElement("m:oMath")
    for value in fragments:
        if isinstance(value, tuple):
            node = OxmlElement("m:sSub")
            for tag, content in [("m:e", value[0]), ("m:sub", value[1])]:
                part = OxmlElement(tag)
                run = OxmlElement("m:r")
                text = OxmlElement("m:t")
                text.text = content
                run.append(text)
                part.append(run)
                node.append(part)
            math.append(node)
        else:
            run = OxmlElement("m:r")
            text = OxmlElement("m:t")
            text.text = value
            run.append(text)
            math.append(run)
    para._p.append(math)


def save(doc, filename):
    output = OUT / filename
    doc.save(output)
    print(f"DOCX {filename}")
    return output


def diagram():
    image = Image.new("RGB", (1680, 950), "white")
    draw = ImageDraw.Draw(image)
    font_path = "C:/Windows/Fonts/msyh.ttc"
    f1 = ImageFont.truetype(font_path, 31)
    f2 = ImageFont.truetype(font_path, 25)

    def box(rect, title, lines, fill="#F1F5F8"):
        draw.rounded_rectangle(rect, radius=14, fill=fill, outline="#B6C5D1", width=2)
        x1, y1, x2, y2 = rect
        draw.text(((x1+x2)/2, y1+26), title, font=f1, fill="#172B3E", anchor="mm")
        for idx, line in enumerate(lines):
            draw.text(((x1+x2)/2, y1+67+idx*34), line, font=f2, fill="#253747", anchor="mm")

    def arrow(x1, y1, x2, y2, label=""):
        draw.line((x1,y1,x2,y2),fill="#41627C",width=4)
        if y2 > y1:
            draw.polygon([(x2,y2),(x2-10,y2-15),(x2+10,y2-15)],fill="#41627C")
        elif x2 > x1:
            draw.polygon([(x2,y2),(x2-15,y2-10),(x2-15,y2+10)],fill="#41627C")
        else:
            draw.polygon([(x2,y2),(x2+15,y2-10),(x2+15,y2+10)],fill="#41627C")
        if label:
            draw.text(((x1+x2)/2+12,(y1+y2)/2-8),label,font=f2,fill="#253747")

    box((55,35,1050,150),"Vue 3 与 TypeScript 界面",["聊天与历史会话  ·  方案与确认卡  ·  模拟银行面板"])
    box((1130,35,1630,150),"可选飞书私聊",["身份绑定  ·  桥接鉴权"])
    box((55,210,1050,335),"FastAPI 接口与事件通道",["连接 Cookie  ·  来源检查  ·  请求校验  ·  SSE 持久化事件"])
    box((1130,235,1630,370),"兼容大模型接口",["原生工具调用 / JSON 计划","仅输出建议与参数"])
    box((55,400,1050,530),"Agent 任务状态机",["单队列逐步处理  ·  11 类查询工具  ·  13 类确认工具","变更请求暂停  →  用户确认  →  固定参数执行"])
    box((55,590,530,730),"Bank 业务工具",["余额与限额  ·  风险匹配","扣款  ·  流水  ·  操作回执"])
    box((585,590,1050,730),"Planning 与 Cashflow",["资金预留  ·  计划版本","确定性预演  ·  单因素挑战"])
    box((55,800,1050,915),"SQLite 本地数据与审计记录",["WAL  ·  BEGIN IMMEDIATE  ·  SAVEPOINT  ·  操作 ID 去重"])
    box((1130,580,1630,760),"资金变更边界",["模型无直接数据库写权限","用户确认后重查当前账务","条件提醒不会自动转账"],"#EAF2F5")
    arrow(550,150,550,210)
    arrow(1380,150,1380,205)
    draw.line((1380,205,1050,205,1050,272),fill="#41627C",width=4)
    arrow(550,335,550,400)
    arrow(1050,430,1130,320)
    arrow(300,530,300,590)
    arrow(815,530,815,590)
    arrow(300,730,300,800)
    arrow(815,730,815,800)
    output = WORK / "architecture.png"
    image.save(output)
    return output


INTRO = (
    "bankmate 是面向个人的中文银行智能体，在本地模拟银行中将自然语言需求转换为可核对的查询、资金方案和操作确认。"
    "项目用 Vue 与 FastAPI 实现完整交互，采用兼容大模型接口理解需求，由后端工具计算账单、现金流和业务检查；"
    "资金及状态变更必须经过用户确认，并以落盘回执作为完成依据。项目重点解决购买前资金预演、跨会话预算冲突和多步骤任务中的执行控制。"
)


def technical(architecture):
    doc = new_doc("技术文档")
    h(doc, "作品概述")
    p(doc, INTRO)
    p(doc, "全部账户、联系人、银行卡、理财产品和交易均为模拟数据。作品当前支持单个演示身份；源码与界面中的“有数”是现有内部名称，对外参赛名称为 bankmate。")
    table(doc, ["项目项", "当前实现"], [
        ("技术栈", "Vue 3、TypeScript、Vite；FastAPI、Python；SQLite"),
        ("业务范围", "转账、账单、理财、卡片、订阅及跨场景资金计划"),
        ("工具边界", "24 个白名单工具，其中 11 个查询类、13 个确认变更类"),
        ("验证结果", "65 项参数化后端用例通过；HTTP/SSE 检查通过；前端 build 通过"),
    ], [3.2,14])
    h(doc, "参赛成员", 2)
    table(doc, ["姓名", "身份", "学校与专业"], [(name,role,"暨南大学  金融科技") for name,role in MEMBERS], [3.2,2.2,11.8], (1,))

    page(doc, "1 产品目标与业务范围")
    p(doc, "用户通常提出的是一个目标，例如“买一台电脑后还能否留够生活费”，而不是单个银行接口请求。bankmate 将需求拆成查询、预演、确认和执行，让用户在同一会话中看到资金依据、业务阻碍及最终回执。")
    table(doc, ["能力", "用户任务", "系统行为"], [
        ("转账", "给张三转 200 元", "查询或解析收款实体；重名澄清；生成确认卡"),
        ("账单分析", "本月的钱花在哪了", "按真实模拟流水汇总，展示分类及异常线索"),
        ("模拟理财", "比较产品并申购或赎回", "用户回答风险问卷；后端核对等级、持仓及金额"),
        ("卡片管理", "冻结卡片或调整日限额", "确认后变更，后续扣款重新检查卡片状态"),
        ("订阅管理", "查看并取消自动续费", "核对周期流水与协议；取消不退款"),
        ("资金预演", "购买 6000 元电脑并保留生活费", "比较执行时点；显示逐事件余额和资金缺口"),
        ("目标与承诺", "累计存下 2000 元", "记录目标完成额和预留；改口时仅处理差额"),
        ("条件触发", "工资到账后提醒存钱", "入账事件产生建议；采纳及资金执行仍需确认"),
        ("业务诊断", "为什么转不了账或买不了理财", "只读列出多个独立阻碍和相应依据"),
    ], [2.5,5.1,9.6])
    h(doc, "产品边界", 2)
    p(doc, "系统没有连接真实银行或商户，不执行真实代扣，也不提供投资收益承诺。理财净值固定、即时结算；风险问卷是三项回答形成的演示等级。飞书适配默认关闭，Web 可独立使用。")

    page(doc, "2 系统架构")
    picture = doc.add_picture(str(architecture), width=Cm(WIDTH))
    picture._inline.docPr.set("descr", "bankmate 架构图，展示 Vue、FastAPI、Agent、业务工具、现金流服务和 SQLite 及用户确认边界")
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.paragraphs[-1].paragraph_format.line_spacing = 1.0
    doc.add_paragraph("图 1 bankmate 系统架构与资金执行边界", "Caption")
    p(doc, "浏览器通过同源 HTTP 接口提交需求，通过 SSE 接收消息与银行状态事件。FastAPI 校验连接凭证、请求来源和参数，再将任务交给 Agent 队列。模型决定需要的工具；Bank 与 Planning 承担确定性业务判断、数据计算和事务写入。")
    table(doc, ["模块", "源码", "职责"], [
        ("API", "main.py", "连接、会话、确认、计划、SSE 与静态文件托管"),
        ("编排", "agent.py 与 llm.py", "工具协议、任务状态、暂停确认与结果回传"),
        ("业务", "bank.py", "参数验证、查询、业务复核、扣款与回执"),
        ("计划", "planning.py 与 cashflow.py", "计划版本、承诺、事件预演与压力边界"),
        ("存储", "db.py", "数据初始化、迁移、事务和持久化事件"),
    ], [2.1,6.0,9.1])

    page(doc, "3 Agent 编排与工具协议")
    h(doc, "3 1 工具执行流程", 2)
    p(doc, "需求先进入 queued，由单个异步队列转为 running。查询工具返回数据卡和模型可读结果；变更工具仅预检查，生成 15 分钟有效的确认卡，任务转为 waiting_confirmation。")
    p(doc, "确认后以已保存参数执行，事务内重查账务。成功回执交给模型，任务继续处理下一步。任一步失败停止后续；已完成操作保留。组合任务逐步提交，不提供整组回滚。")
    p(doc, "每个会话只接受一条处理中请求，不同会话依次处理。新需求取消本会话旧的待确认操作，已执行操作不撤销。")
    h(doc, "3 2 白名单与参数验证", 2)
    table(doc, ["类别", "工具示例", "执行约束"], [
        ("账户与事实查询", "get_accounts、analyze_bills、list_products", "由数据库返回金额、持仓与来源"),
        ("诊断与预演", "diagnose_transfer、diagnose_purchase、preview_cashflow", "无资金变更；预演会保存草稿及版本元数据"),
        ("资金与产品变更", "transfer、purchase_product、redeem_product", "用户确认后执行，并复核业务条件"),
        ("卡片与订阅变更", "set_card_status、set_card_limit、cancel_subscription", "固定对象和参数；生成状态回执"),
        ("计划与预算变更", "activate_plan、reserve_funds、save_to_goal", "版本及资金承诺检查；仍需用户确认"),
    ], [3.6,7.0,6.6])
    p(doc, "Pydantic 严格验证并禁止额外字段；金额须为元单位十进制字符串。未知工具、伪造字段、浮点数、负数、科学计数法或格式错误均被拒绝。")
    h(doc, "3 3 模型兼容与失败控制", 2)
    p(doc, "LLM_TOOL_MODE 支持 auto、native、structured。auto 优先原生 tools/tool_calls；无本轮查询证据却只返回正文时，改用同一模型的 JSON 计划。两种协议共用白名单、参数和确认流程。格式最多纠正一次，不重放业务。")
    p(doc, "默认模型请求超时 60 秒，每个任务最多 12 个模型步骤、32 个工具步骤；单次响应最多 8 个工具调用。模型未配置、网络失败、结果截断或协议无效时明确失败。成功状态以 executed 回执为依据，模型文字不作为记账凭证。")

    page(doc, "4 账单风险与资金精度算法")
    h(doc, "4 1 账单汇总与异常线索", 2)
    p(doc, "按 YYYY-MM 读取流水，将对外支出按类别累计。本人账户互转及理财本金进出排除在消费口径之外，收入采用相同排除规则。所有统计值均由整数金额计算，模型只做语言解释。")
    p(doc, "异常检查采用两条明确规则：单笔消费达到 3000 元；同月、同订阅、同金额出现多笔扣费。结果保留交易 ID，可回查原始记录。此处输出待核对线索，没有训练欺诈分类模型，也不认定交易欺诈。")
    h(doc, "4 2 演示风险等级", 2)
    table(doc, ["维度", "回答与分值"], [
        ("投资期限", "一年以内 1；一至三年 2；三年以上 4"),
        ("亏损承受", "不接受 1；小幅 2；中等 3；较大 4"),
        ("投资经验", "没有经验 2；有一些经验 3；较丰富 4"),
    ], [3.4,13.8])
    p(doc, "最终风险等级取三个维度分值的最小值，得到 R1 至 R4。回答由用户提供，保存测评需要确认；申购要求已测评且用户等级不低于产品等级。赎回依据实际持仓处理，不要求新增风险测评。该评分仅用于演示。")
    h(doc, "4 3 金额与份额精度", 2)
    p(doc, "账户金额、流水、计划和承诺均以整数分存储。工具接收元单位的十进制字符串，使用 Decimal 转换；理财份额保留四位小数。申购先将份额向下截到四位，再将实际扣款向上取整到分；赎回到账金额向下取整到分。")
    p(doc, "资金总额由账户余额与按固定模拟净值计价的持仓构成。本人账户互转应满足资产守恒，且不增加消费统计。金额精度、份额结算和不足一分赎回均有自动化用例。")
    h(doc, "4 4 周期性记录识别", 2)
    p(doc, "工资与房租仅从业务日期之前的完整月份寻找。按商户标识、方向、金额和日号分组，至少两个不同月份符合相同模式才采用；使用中位数形成金额与日号假设，并保留证据交易 ID。用户可显式提供假设参数，来源标记为 user。订阅结合跨月记录与当前协议检查，不从文本猜测商户状态。")

    page(doc, "5 现金流投影与单因素压力测试")
    p(doc, "现金流预演读取当前余额、有效资金承诺及预计事件，输出每个事件后的余额、受保护资金和可自由安排余额。预演不改变资金，只保存方案草稿。")
    formula(doc, [("A","t"), " = ", ("B","t"), " − ", ("P","t")])
    p(doc, "其中 B 是账面余额，P 是仍有效的预留金额，A 是可自由安排余额。收入增加 B；消费或转存减少 B；已预留的房租实际支出时，同时释放对应承诺，避免重复扣减预算。任一起点或中间事件 A 小于零，就记录首次缺口。")
    p(doc, "事件按日期排序；同日且缺少时刻时，普通支出先于动作、收入处理，明确依赖收入的动作放在收入之后。基线排序复杂度为 O(n log n)，扫描为 O(n)。")
    h(doc, "5 1 可复现的购买方案", 2)
    p(doc, "固定演示业务日为 2026-09-30。日常余额 6256 元，已预留房租 3200 元、生活费 3000 元、订阅 56 元；购买电脑需要 6000 元。以下回放显式指定每月 1 日工资 18000 元、每月 2 日房租 3200 元、期限 2026-10-31，未把预测工资记入实际余额。")
    table(doc, ["执行方案", "执行日期", "首次缺口", "期限末自由余额", "结论"], [
        ("立即购买", "2026-09-30", "6000 元", "12000 元", "不可行"),
        ("工资后购买", "2026-10-01", "无", "12000 元", "可行"),
    ], [3.5,3.4,3.2,4.0,3.1], (4,))
    p(doc, "两条路径期末余额相同，但立即购买路径在工资到账之前出现缺口。因此可行性检查使用完整事件路径，不能只检查期末余额。可行方案在预演起点的最低自由余额为 0 元；查询结果不等于允许立即消费。")
    h(doc, "5 2 压力边界与修复", 2)
    p(doc, "工资延期挑战在 0 至 30 天范围逐天回放，找到首个失效延迟。上述方案工资延期 1 天后，10 月 1 日购买出现 6000 元缺口；将购买推迟至 10 月 2 日并排在到账之后，重新回放可通过。修复另存为新版本，不能覆盖已经确认的历史版本。")
    p(doc, "预算外支出挑战依据指定日期及后续投影的最低余量计算金额边界，以整数分输出最大承受额及下一分的边界。该方法是当前事件和单个因素下的规则检查，未覆盖多因素联动、真实到账时刻或全局最优调度。")

    page(doc, "6 计划版本资金承诺与条件触发")
    h(doc, "6 1 计划与承诺的生命周期", 2)
    p(doc, "preview_cashflow 创建 draft 计划和版本，记录输入参数、假设、方案与证据。用户选择采用时，activate_plan 的确认参数固定 plan_id、plan_version 和 variant_key；实际确认会核对当前版本及资金安排。")
    p(doc, "采用计划后，房租、生活费或订阅预留写入 commitments。可用资金检查从账户余额扣除有效承诺，所有会话共享这份约束。另一会话尝试动用已经安排的资金，在预检查或最终确认阶段被拒绝。减少或解除承诺同样需要确认。")
    table(doc, ["动作", "数据影响", "是否发生资金执行"], [
        ("生成预演", "保存草稿与版本元数据", "否"),
        ("确认采用计划", "计划生效并保存资金承诺", "仅安排资金"),
        ("生成消费或转存确认", "固定对象、金额及关联版本", "否"),
        ("确认执行", "更新账户、流水、完成额和回执", "是  模拟资金"),
        ("修改累计目标", "新目标减已完成金额形成差额", "差额仍需确认"),
    ], [4.2,8.3,4.7])
    h(doc, "6 2 储蓄目标", 2)
    p(doc, "目标按 plan_id 追踪成功执行额。用户已存 500 元，再把同一目标改成累计 700 元时，只生成 200 元差额确认。长期目标可按预计工资周期分期，整数分余数分配到最后一期，保证分期合计等于目标金额。")
    h(doc, "6 3 工资事件与提醒", 2)
    p(doc, "登记规则需要用户确认。模拟工资事件以 event_key 去重，账务只入账一次；每条规则、月份和触发类型也有唯一约束。到账后按最新余额和承诺生成待采纳建议，用户采纳后才产生转存确认卡，点击确认才执行。")
    p(doc, "日期提醒每分钟检查本地规则，只提醒用户核对工资是否到账，不伪造收入。模型预测、日期到期、事件发生和资金执行是四个独立状态。工资没有实际到账时，不生成依赖工资的最终执行确认。")

    page(doc, "7 数据一致性与恢复")
    table(doc, ["数据组", "主要数据表", "用途"], [
        ("账务", "accounts、transactions、holdings", "余额、资金流、持仓与业务操作 ID"),
        ("业务对象", "contacts、cards、products、subscriptions", "固定实体、状态与约束"),
        ("会话与任务", "conversations、messages、tasks", "独立上下文、任务状态与历史展示"),
        ("确认与事件", "approvals、events", "固定参数、状态、回执和 SSE 序号"),
        ("计划与预算", "plans、plan_versions、commitments、plan_executions", "版本化方案、共享承诺和执行进度"),
        ("条件与通知", "trigger_rules、bank_events、trigger_runs、notifications", "到账去重、提醒与建议采纳"),
        ("连接与渠道", "sessions、binding_codes、feishu_binding 等", "连接令牌、一次性绑定和渠道去重"),
    ], [3.2,7.2,6.8])
    h(doc, "7 1 单项业务原子性", 2)
    p(doc, "SQLite 开启 WAL。写入采用 BEGIN IMMEDIATE，等待写锁超时为 10 秒；确认阶段使用 SAVEPOINT bank_action，将余额、份额、流水和回执的更新纳入同一事务。业务检查失败时回滚本次操作，保留失败卡片与任务结果。")
    p(doc, "approvals 状态从 pending 转为 executed 后，重复确认返回已有状态和回执，不再次执行。流水 action_id 及计划执行 approval_id 使用唯一约束；内部转账使用同一操作的派生流水标识。并发确认用例包含 8 个请求，结果仅扣款一次并返回同一回执 ID。")
    h(doc, "7 2 会话与恢复", 2)
    p(doc, "会话之间隔离消息、模型上下文和待确认操作，共享同一演示身份的余额和承诺。切换会话不取消原卡片；Web 确认须匹配原会话 ID。删除会话取消尚未确认操作并隐藏会话入口，已完成交易记录保留。")
    p(doc, "服务重启将处理中断的 running 任务标为失败，不自动重放；queued 任务继续入队，待确认卡片及执行回执持久化。SSE 使用自增事件序号，客户端可通过 Last-Event-ID 恢复连接。持久化审计记录便于追踪，但没有实现不可篡改存证。")

    page(doc, "8 安全设计与权限分级")
    p(doc, "项目采用能力分级和流程控制，不是多用户角色授权系统。默认只监听 127.0.0.1；连接 Cookie 用于识别浏览器连接及恢复选中会话，并不能证明真实银行账户身份。详细风险见独立安全自评报告。")
    table(doc, ["级别", "能力", "后端控制"], [
        ("L0", "初始化与健康状态", "bootstrap 自动创建连接；模型密钥不返回客户端"),
        ("L1", "账务查询、诊断、预演", "连接校验、工具白名单、严格参数；不扣款"),
        ("L2", "资金和业务状态变更", "固定参数确认、15 分钟有效期、版本与当前账务复核"),
        ("L3", "沙箱重置、载入场景及模拟入账", "连接校验及专用请求字段；当前没有独立管理员角色"),
        ("渠道", "飞书私聊接入", "桥接 Bearer 密钥、一次性绑定码及租户 用户 私聊三元组"),
    ], [1.6,6.0,9.6], (0,))
    h(doc, "8 1 输入与请求控制", 2)
    p(doc, "API 请求模型禁止额外字段；工具使用严格类型、长度范围与金额格式。收款人名称只用于预解析，确认前保存实际 ID；最终执行不按可变名称重新匹配。业务对象 ID 经绑定参数查询数据库，SQL 表名只来自固定程序代码。")
    p(doc, "API 对存在的 Origin 检查本地来源，Cookie 设置 HttpOnly 与 SameSite=Strict，在 HTTPS 时设置 Secure。响应包含 nosniff、same-origin 引用策略和 API no-store。来源检查没有对缺失 Origin 强制拒绝，不能单独替代身份认证。")
    h(doc, "8 2 模型与数据边界", 2)
    p(doc, "模型没有执行代码、SQL 或任意外链的工具。提示要求将联系人名称、产品说明和商户备注视为数据；真正的权限控制在后端白名单及确认路径。用户输入中的已知联系人手机号替换为稳定 ID，其他匹配手机号隐藏，查询结果只展示脱敏号码。")
    p(doc, "模型服务仍会收到当前任务必要的对话和银行结果，日志及数据库也可能包含用户原始输入。当前没有完整个人信息检测、存储加密、数据保留策略、二次身份校验或独立安全审计认证，故交付边界是本地模拟演示。")

    page(doc, "9 接口配置与部署")
    table(doc, ["接口", "用途"], [
        ("GET /api/bootstrap", "连接初始化、选中会话、历史与银行快照"),
        ("POST /api/messages", "提交自然语言需求并返回任务 ID"),
        ("GET /api/events", "SSE 消息及任务事件，可恢复事件序号"),
        ("POST /api/approvals/{id}/decision", "确认或取消固定参数操作"),
        ("GET /api/bank", "读取模拟银行状态"),
        ("POST /api/sandbox/inspect", "无需模型的白名单只读查询"),
        ("POST /api/plans/preview", "结构化现金流预演"),
        ("POST /api/plans/{id}/actions", "挑战、修复、采用、规则和执行请求"),
        ("POST /api/sandbox/events", "模拟工资到账事件"),
        ("POST /api/notifications/{id}/adopt", "采纳建议并生成待确认操作"),
        ("POST /api/sandbox/reset", "以 RESET 字段重置整个演示沙箱"),
    ], [9.0,8.2])
    h(doc, "9 1 本地部署", 2)
    p(doc, "环境为 Windows PowerShell、uv、Python 3.11 和 Node.js 22.12 或更新版本。后端依赖由 uv.lock 固定，前端依赖由 package-lock.json 固定。启动脚本安装依赖、构建前端，再由 FastAPI 托管 frontend/dist。")
    code(doc, r".\scripts\start.ps1" + "\n" + r".\scripts\start.ps1 -SkipBuild")
    p(doc, "浏览器访问 http://127.0.0.1:8000，API 说明为 /docs。模型只读取后端 .env 的 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL；接口地址须保留服务商版本前缀且不带 /chat/completions。模型未配置时，模拟面板仍可只读查询，聊天明确提示未连接。")
    p(doc, "依赖安装使用 uv 全局缓存及 hardlink，跨盘时可能回退复制。SQLite 文件默认 data/banking.sqlite3。当前服务不需要 Docker、Redis 或额外数据库；本地版本不包含公网部署、租户隔离或多实例调度。")

    page(doc, "10 验证结果与已知限制")
    p(doc, f"验证日期为 {DATE}。本次执行 scripts/check.ps1 和 scripts/smoke_backend.py，测试使用临时数据库与模型替身，不修改日常演示数据，也不访问真实银行。")
    table(doc, ["检查", "结果", "证据范围"], [
        ("Ruff", "通过", "backend 静态检查"),
        ("pytest", "65 项通过", "6 个测试文件，52 个测试函数经参数化展开"),
        ("HTTP 与 SSE", "通过", "实际本地 HTTP 服务，11 条事件、重放与多会话隔离"),
        ("前端构建", "通过", "vue-tsc --noEmit 与 vite build；不代表交互已验证"),
        ("真实模型", "未执行", "需要提供接口与凭证后另行联调"),
        ("真实飞书", "未执行", "自动化用例仅验证本地适配逻辑"),
        ("浏览器交互", "由参赛团队验证", "本次未进行浏览器自动化或前端交互检查"),
    ], [3.1,3.1,11.0])
    h(doc, "10 1 核心验证点", 2)
    p(doc, "测试覆盖确认前不扣款、并发确认仅执行一次、余额复核、收款重名、资金守恒、风险匹配、冻结与限额、精度、组合任务部分完成、确认过期、提示注入无法授权、会话隔离、服务重启、资金承诺、计划分期、诊断只读及手机号脱敏。")
    h(doc, "10 2 当前限制", 2)
    p(doc, "固定缺口场景的业务日期为 2026-09-30，种子流水依据启动时的实际月份生成。在本次 10 月 8 日回放中，固定日期前不足两个月匹配历史，自动工资与房租推断为空；因此固定场景演示显式提供假设参数，并展示其用户输入来源。按当前日期的普通种子数据可识别 8 月与 9 月的匹配记录。")
    p(doc, "现金流依赖所列假设与事件，压力测试只覆盖单个因素；同日事件粒度、联动扰动、长期真实交易行为和失败补偿仍有扩展空间。当前验证没有给出真实模型准确率、生产吞吐、可用性 SLA 或安全认证结论。")
    h(doc, "10 3 可复核源码", 2)
    p(doc, "核心入口为 backend/banking/main.py、agent.py、bank.py、llm.py、db.py、planning.py、cashflow.py；前端入口为 frontend/src/App.vue。完整测试在 backend/tests，部署与 HTTP 检查在 scripts。源码包提供当前文件 SHA256 清单与材料验证记录，便于评委核对对应版本。")
    return save(doc, "01_bankmate_技术文档.docx")


def security():
    doc = new_doc("安全自评报告")
    h(doc, "自评结论")
    p(doc, "bankmate 已将查询与资金变更分开，并在后端实施白名单、严格参数、固定参数确认、当前账务复核、事务执行和重复确认去重。自动化测试表明这些机制在既定本地沙箱场景中有效。")
    p(doc, "当前版本定位为单身份本地模拟演示。浏览器连接不等于真实用户登录，未实现多用户 RBAC、独立管理员、真实银行认证或不可篡改审计。现有实现不应作为真实资金服务的安全准入结论。")
    h(doc, "1 自评范围与资产", 2)
    table(doc, ["资产", "保护目标"], [
        ("模拟账户与持仓", "变更可确认、可复核、仅执行一次"),
        ("计划与资金承诺", "跨会话一致，旧版本不能直接执行"),
        ("对话及联系人信息", "最少对外传递，减少手机号暴露"),
        ("模型与飞书凭证", "只在服务端使用，不进入前端或提交包"),
        ("确认与流水记录", "保存操作依据、状态、回执和关联 ID"),
    ], [5.2,12.0])
    p(doc, "范围包括 Web API、模型工具路径、计划与资金执行、SQLite 存储及飞书本地适配。真实模型服务的安全能力、飞书线上权限、终端操作系统安全及真实银行接口不在本次离线验证覆盖范围内。")
    h(doc, "验证概况", 2)
    p(doc, "2026年10月8日完成后端静态检查、65 项 pytest 用例和实际 HTTP/SSE 检查，前端 build 通过。未执行浏览器交互、真实模型或真实飞书联调，也未开展外部渗透测试。")

    page(doc, "2 信任边界与威胁模型")
    p(doc, "用户输入、模型输出、历史引用、联系人名称和交易备注都应视为不可信数据。模型负责提议工具和参数，后端负责验证能力及执行资格。浏览器、模型服务、飞书桥接和本地数据库各形成独立边界。")
    table(doc, ["威胁", "可能结果", "已实现控制"], [
        ("模型越权工具", "执行代码、SQL 或未知业务", "仅接受 TOOLS 白名单；未知工具终止任务"),
        ("伪造工具参数", "改金额、跳过确认或注入额外权限", "严格类型与 extra=forbid；确认参数由服务端保存"),
        ("提示注入", "用商户备注或联系人文本授予权限", "数据标记与提示约束；实际变更仍走后端确认"),
        ("重复确认或回调", "多次扣款或多次入账", "确认状态、操作唯一 ID、到账与渠道事件去重"),
        ("确认后状态变化", "花费已被冻结或预留的资金", "确认事务重查余额、承诺、卡片、日限额与产品风险"),
        ("旧页面与会话混淆", "在错误会话确认旧需求", "Web 确认检查原会话；新需求取消旧待确认操作"),
        ("隐私与凭证暴露", "向模型或浏览器暴露敏感信息", "已知手机号替换或脱敏；密钥只由后端读取"),
        ("服务中断", "重启后重复业务或丢失执行结果", "结果落盘；running 标记失败，不自动重放"),
    ], [3.5,5.5,8.2])
    h(doc, "不可信数据的处理", 2)
    p(doc, "模型协议校验只证明工具计划的格式与参数符合约束，不证明用户意图一定被正确理解。用户必须核对确认卡上的收款实体、金额和操作类别；后端以已保存参数执行，而不是再次解析模型自由文本。")

    page(doc, "3 权限分级实现")
    p(doc, "下列分级描述当前能力与执行流程，不将演示风险等级 R1 至 R4 当成用户访问角色。Web 的所有有效连接共享同一个演示身份，当前没有管理员与普通用户的强隔离。")
    table(doc, ["层级", "允许能力", "实施位置", "限制"], [
        ("L0", "健康检查及连接初始化", "main.py health、bootstrap", "bootstrap 自动发连接令牌，不是账户登录"),
        ("L1", "查询、诊断和资金预演", "session、Bank.query、TOOLS", "只读账务；预演保存草稿元数据"),
        ("L2", "交易、状态、计划及承诺变更", "Bank.prepare、Agent.confirm、Bank.execute", "用户点击确认；15 分钟有效；当前状态再校验"),
        ("L3", "重置、载入演示、模拟工资入账", "sandbox 专用接口", "有连接与专用字段检查，但没有独立管理员角色"),
        ("模型", "提议上述工具名称与参数", "ModelClient 与 Agent", "不能调用确认接口或自由执行 SQL 和代码"),
        ("桥接", "转发已绑定飞书私聊与卡片", "bridge 与 feishu_event", "Bearer 密钥及租户 用户 私聊标识匹配"),
    ], [1.7,4.6,5.6,5.3], (0,))
    h(doc, "3 1 身份与请求来源", 2)
    p(doc, "bank_session 使用随机令牌，数据库只保存 SHA256；Cookie 为 HttpOnly、SameSite=Strict，HTTPS 时设置 Secure。API 校验令牌存在，支持恢复浏览器选中会话。Cookie 的 30 天寿命是浏览器侧配置，服务端没有令牌年龄失效检查。")
    p(doc, "API 对携带 Origin 的请求只接受指定的 localhost 与 127.0.0.1 地址；缺失 Origin 的请求仍可进入连接校验。防跨站配置降低本地误用风险，但不能代替真实登录或完整的 CSRF 设计。")
    h(doc, "3 2 沙箱管理边界", 2)
    p(doc, "重置使用 confirmation=RESET，载入缺口场景使用 confirmation=LOAD_DEMO。它们防止界面误操作，但不能构成管理权限认证。模拟工资事件会增加沙箱余额，这类直接业务测试入口仅适合本地演示。真实服务应限制或移除此类入口。")

    page(doc, "4 交易安全与执行保护")
    h(doc, "4 1 固定参数与二次复核", 2)
    p(doc, "准备阶段验证对象和参数，收款人重名不能生成有效转账确认。通过检查后保存实际收款实体 ID、金额及摘要。确认请求仅提交 confirm 或 cancel 与原会话 ID，不允许从确认请求另传金额、对象或 bypass 字段。")
    p(doc, "执行前按当前数据重新检查余额、已安排资金、卡片状态、累计日支付限额、产品起购额和风险匹配。诊断卡即使显示 ready，也不跳过最终复核；持仓与计划版本发生变化时同样可能阻止执行。")
    h(doc, "4 2 去重与事务", 2)
    p(doc, "同一确认 ID 只允许 pending 状态执行。BEGIN IMMEDIATE 保证写入互斥，SAVEPOINT 将单项资金变更隔离；成功状态与回执和账务一起提交。重复请求读取已有回执，模型请求和卡片重发不重新记账。")
    table(doc, ["风险点", "可运行验证", "验证结论"], [
        ("未经确认扣款", "test_transfer_requires_confirmation_and_writes_receipt", "确认前余额不变，确认后才写回执"),
        ("并发重复扣款", "test_simultaneous_confirmation_is_idempotent", "8 路确认只扣一次并返回同一回执"),
        ("过期确认", "test_expired_approval_cannot_execute", "超过 15 分钟后不执行"),
        ("状态变化", "test_balance_is_revalidated_at_confirmation", "余额改变后拒绝旧卡片执行"),
        ("伪造确认字段", "test_unknown_tool_and_fake_confirmation_cannot_execute", "未知工具及额外参数不获授权"),
    ], [3.0,8.2,6.0])
    h(doc, "4 3 中断与部分完成", 2)
    p(doc, "组合任务按步执行，一项失败停止后续，已经完成的操作保留并向用户说明完成数量。该行为未实现跨多笔交易的整体回滚。服务重启不重放处理中断的模型请求；失败后用户应先核对回执，再决定是否重新发起业务。")

    page(doc, "5 数据隐私渠道与审计")
    h(doc, "5 1 数据最小化", 2)
    p(doc, "模型上下文中的已知联系人手机号替换为稳定联系人 ID，其他符合手机号模式的数字隐藏；查询结果返回脱敏号码和尾号。最多取最近 30 条界面消息组成新任务历史，结构化历史数据有长度上限。内部异常不直接暴露上游正文或凭证。")
    p(doc, "原始用户消息仍会保存到本地 SQLite，交易描述和对话语义也可能包含姓名、地址等信息。当前脱敏主要覆盖手机号，不等于完整个人信息识别。模型服务可见本轮必要对话与工具结果；使用真实资料前应另行确定外部数据授权、保留和删除政策。")
    h(doc, "5 2 飞书绑定与重放保护", 2)
    p(doc, "飞书默认关闭。启用时，桥接令牌要求至少 32 字符，后端采用恒定时间比较验证 Bearer 凭证。绑定码为随机值，哈希存储、10 分钟有效、使用一次即删除；绑定同时核验 tenant_key、open_id 和 chat_id，只处理文本私聊及卡片回调。")
    p(doc, "入站 event_id 保存处理结果，重复事件不重复创建任务。发送端按消息 ID 维护 outbox，发送失败不会再次执行银行业务。Web 与飞书均使用同一审批事务；飞书可确认该演示身份的原操作，真实多账户服务仍需更细的主体与对象授权。")
    h(doc, "5 3 审计记录与留存", 2)
    table(doc, ["记录", "可追踪信息"], [
        ("tasks 与 messages", "需求、任务状态、错误、数据卡和确认卡"),
        ("approvals", "工具、固定参数、创建及到期时间、最终状态和回执"),
        ("transactions", "账户、金额、方向、业务时间与操作 ID"),
        ("plan_versions 与 executions", "版本输入、预演结果、关联确认及目标完成额"),
        ("events 与 trigger_runs", "SSE 序号、到账和条件触发处理依据"),
    ], [6.3,10.9])
    p(doc, "删除会话保留已执行银行交易；重置演示会清理聊天、任务、确认、计划与账务并重新生成数据。审计记录存于普通 SQLite，没有签名、外部备份或不可篡改存证，重置之前应保留所需评审证据。")

    page(doc, "6 已知风险清单")
    p(doc, "等级按当前单机演示用途判断。下列改进均属于尚未完成项；不将建议性控制写成已实现能力。")
    table(doc, ["编号", "等级", "已知风险", "现有边界与后续处理"], [
        ("R01", "高", "单身份，无真实登录与多租户隔离；初始化自动发连接令牌", "限制本机访问；真实服务需账户认证与对象级授权"),
        ("R02", "高", "沙箱重置与模拟入账没有独立管理员角色", "仅本地模拟；真实环境移除或分离管理接口"),
        ("R03", "中", "缺失 Origin 仍可访问；无独立 CSRF 令牌", "保留 SameSite 与来源检查；补严格跨站请求方案"),
        ("R04", "中", "连接令牌仅校验存在，服务端无年龄失效与主动撤销流程", "本地使用；补服务端 TTL、注销与会话轮换"),
        ("R05", "中", "数据库明文存储原始对话，脱敏主要覆盖手机号", "只用模拟数据；补数据分级、加密及完整脱敏规则"),
        ("R06", "中", "提示注入可能影响解释或误导工具建议", "后端强制确认与白名单；扩展对抗语料及意图核对"),
        ("R07", "中", "模型请求与结果依赖外部服务，尚无真实联调指标", "离线验证替身；提交前单独检验超时、协议和数据范围"),
        ("R08", "中", "普通 SQLite 审计可修改，重置会清理业务证据", "重置前备份；补访问控制、保留策略和外部审计"),
    ], [1.4,1.5,7.4,6.9], (0,1))
    h(doc, "风险处理优先顺序", 2)
    p(doc, "继续本地演示时优先保护凭证、保存提交快照并完成手动交互核验。准备接入真实数据或公网前，先处理身份及对象授权、管理入口和数据保护，再扩展模型与流量治理。")

    page(doc, "7 业务与验证剩余风险")
    table(doc, ["编号", "等级", "已知风险", "处理建议"], [
        ("R09", "中", "固定业务日期与按当前月份生成的种子流水可能不一致，自动工资与房租推断为空", "固定场景显式说明假设；后续对齐种子日期并补日期回归"),
        ("R10", "中", "压力挑战为单因素；同日事件粒度和联动风险尚未充分覆盖", "明确搜索范围；补逐事件回放和多因素组合验证"),
        ("R11", "中", "多步骤任务部分完成后不整体回滚", "回执披露完成步骤；按真实业务要求设计补偿"),
        ("R12", "低", "单个模型工作队列可能使长请求阻塞其他会话", "保持步数和超时上限；补队列限流及负载测量"),
        ("R13", "中", "只有本地模拟的飞书绑定与回调检查，真实私聊未联调", "启用前验证发布权限、租户身份、卡片和发送失败"),
        ("R14", "低", "前端交互、无障碍和跨浏览器行为尚未经本次检查", "构建已通过；团队手动检查焦点、卡片与错误提示"),
    ], [1.4,1.5,7.6,6.7], (0,1))
    h(doc, "7 1 安全用例的证据范围", 2)
    p(doc, "test_banking.py 验证未经确认、重复确认、过期、精度、未知工具、数据注入、失败后不重付及卡片业务约束。test_api.py 验证连接来源与篡改字段、模型未配置、沙箱重置、飞书绑定去重和跨渠道确认。")
    p(doc, "test_conversations.py 验证上下文分离、原会话确认、删除后保留资金数据、处理中保护及迁移。test_diagnostics.py 验证多阻碍只读诊断、确认后复核、证据来源和手机号脱敏。test_planning.py 验证承诺释放、现金流缺口、单因素边界、到账去重、固定日期及分期。test_llm.py 验证协议兼容、上游错误脱敏、无依据总结拒绝和格式纠正。")
    h(doc, "7 2 结论使用范围", 2)
    p(doc, "本报告是参赛作品的自评和风险披露，不包含生产级安全认证。已实现控制与自动化结果适用于所列代码快照和模拟场景；真实服务的认证、权限、数据与交易控制需要独立设计和重新验证。")
    return save(doc, "04_bankmate_安全自评报告.docx")


def manual():
    doc = new_doc("源代码部署与测试说明")
    h(doc, "交付用途")
    p(doc, "本文配套 bankmate 源代码压缩包，说明评委如何安装、启动、配置模型、回放银行沙箱任务并运行测试。项目默认使用一个演示身份及模拟资金，前端和 API 由同一本地服务提供。")
    table(doc, ["材料分类", "本次材料", "赛事上传要求"], [
        ("技术文档", "01 技术文档 Word", "DOCX 可上传，最大 50MB"),
        ("答辩材料", "02 逐页提纲及讲稿 Word", "提交前转成 PPT、PPTX 或 PDF，最大 80MB"),
        ("源代码与部署", "源码 ZIP 及本文 Word", "上传 ZIP；须含 README、部署说明及测试用例，最大 300MB"),
        ("安全自评", "04 安全自评报告 Word", "DOCX 可上传，最大 50MB"),
    ], [3.8,5.8,7.6])
    p(doc, "作品截止时间为 2026年11月2日18时。每个分类仅保留一个当前有效版本；仓库链接和演示视频不代替必交材料。仓库链接由队伍按实际地址填写，本次没有创建或发布远程仓库。")
    h(doc, "1 环境要求", 2)
    table(doc, ["组件", "要求"], [
        ("运行方式", "Windows PowerShell，本地单进程服务"),
        ("Python", "建议 3.11；项目范围为 >=3.11 且 <3.14"),
        ("依赖工具", "uv；使用 uv.lock 固定依赖并复用缓存"),
        ("前端", "Node.js 22.12 或更新版本，npm"),
        ("模型", "可选配置兼容 Chat Completions 的真实接口"),
        ("银行与存储", "无需真实银行；SQLite 自动初始化"),
    ], [3.8,13.4])

    page(doc, "2 安装启动与配置")
    h(doc, "2 1 一条命令启动", 2)
    p(doc, "解压源码包后，在项目根目录打开 PowerShell。启动脚本首次复制 .env.example 为 .env，安装后端依赖、安装并构建前端，最后启动 FastAPI。")
    code(doc, r".\scripts\start.ps1")
    p(doc, "打开 http://127.0.0.1:8000；后端接口说明为 http://127.0.0.1:8000/docs。Ctrl+C 停止服务。模型配置或前端产物改变后需要重启服务。已安装且已有有效构建产物时，可用 -SkipBuild。")
    h(doc, "2 2 分步安装与启动", 2)
    code(doc, "uv sync --frozen --link-mode hardlink\ncd frontend\nnpm ci --no-audit --no-fund\nnpm run build\ncd ..\nuv run --frozen uvicorn banking.main:app --host 127.0.0.1 --port 8000")
    p(doc, "uv 创建项目 .venv 并复用全局缓存。优先使用 hardlink；缓存和项目跨盘时可能自动退回复制。源码、说明及配置均使用 UTF-8。")
    h(doc, "2 3 配置真实模型", 2)
    code(doc, "LLM_BASE_URL=https://api.example.com/v1\nLLM_API_KEY=填写自己的模型密钥\nLLM_MODEL=填写自己的模型名称\nLLM_TOOL_MODE=auto\nFEISHU_ENABLED=false")
    p(doc, "以上地址是格式示例，须替换成实际服务。BASE_URL 不带 /chat/completions；auto 自动兼容原生工具和 JSON 计划，structured 直接使用 JSON 协议，native 只使用原生工具。密钥只存服务端 .env，提交包不包含此文件。")
    p(doc, "未配置模型时，健康接口仍可返回正常，模拟银行面板可执行只读查询；聊天明确显示未配置。配置状态不保证真实网络连通，需实际请求验证。")

    page(doc, "3 源码目录与配置项")
    table(doc, ["路径", "说明"], [
        ("backend/banking/main.py", "FastAPI 入口及 SSE、确认、计划、沙箱接口"),
        ("backend/banking/agent.py", "模型任务队列、状态机及确认恢复"),
        ("backend/banking/bank.py", "工具定义、查询、业务检查和资金执行"),
        ("backend/banking/llm.py", "真实模型客户端与工具协议兼容"),
        ("backend/banking/planning.py", "计划版本、资金承诺、到账与提醒"),
        ("backend/banking/cashflow.py", "确定性现金流投影及压力边界"),
        ("backend/banking/db.py", "SQLite 初始化、迁移与事务"),
        ("backend/tests/", "自动化测试与临时数据库 fixture"),
        ("frontend/src/", "Vue 界面、确认卡、现金流图与证据展示"),
        ("scripts/ 与 docs/", "启动、检查、模型检查及可选飞书说明"),
    ], [8.0,9.2])
    h(doc, "重要配置", 2)
    table(doc, ["配置项", "默认及含义"], [
        ("DATABASE_PATH", "data/banking.sqlite3；本地演示数据"),
        ("LLM_TIMEOUT_SECONDS", "60；单次模型请求超时"),
        ("LLM_MAX_STEPS", "12；单任务模型步骤上限"),
        ("LLM_TOOL_MODE", "auto；模型工具协议模式"),
        ("FEISHU_ENABLED", "false；可选私聊桥接默认关闭"),
    ], [6.5,10.7])
    p(doc, "飞书真实接入需额外权限和凭证，具体步骤见 docs/feishu.md。评委可完整使用 Web，无需开通飞书。源码当前界面中的“有数”对应本参赛项目 bankmate。")

    page(doc, "4 演示流程与预期结果")
    h(doc, "4 1 初始沙箱", 2)
    p(doc, "日常账户 12860.50 元，储蓄账户 8200.00 元，安心现金持仓 2000 份。联系人包括张三和两位李明；所有姓名、手机号、尾号及资金为模拟数据。历史账单按启动时月份生成，资金缺口场景使用固定业务日期。")
    table(doc, ["步骤", "输入或动作", "应看到的结果"], [
        ("只读", "这个月的钱花在哪了？", "账单分类与来源，余额不变化"),
        ("歧义", "给李明转 100 元", "澄清具体联系人，不能直接生成可执行转账"),
        ("准备", "给张三转 200 元，备注晚餐 AA", "展示实体与金额；余额仍是 12860.50 元"),
        ("执行", "核对后点击确认", "日常余额 12660.50 元，显示 executed 回执"),
        ("重复", "再次提交同一确认", "返回同一回执，余额仍为 12660.50 元"),
        ("诊断", "我想买 1000 元远见成长，为什么买不了？", "列出风险测评等当前阻碍，诊断不申购"),
        ("取消订阅", "取消星河视频会员并确认", "停止模拟协议，历史扣费不退款"),
    ], [1.5,7.1,8.6], (0,))
    h(doc, "4 2 组合任务与会话", 2)
    p(doc, "组合需求可输入“查看订阅，取消星河视频会员，再转 500 元到储蓄账户”。系统分步确认；前一步执行后才继续，任一步失败后续停止。每个确认均独立执行，已有成功步骤不会整体回滚。")
    p(doc, "新建会话形成独立上下文，余额和计划承诺仍共享。返回原会话可继续确认原卡片；发送新需求取消本会话旧的待确认操作，删除会话也取消待确认操作。重置会清空整个演示身份，应提前保留证据。")
    p(doc, "上述步骤是团队手动核验指南。本次已完成前端构建，尚未做浏览器交互验证。真实模型可能追问或采用不同查询顺序，但金额与完成状态必须对应工具数据和回执。")

    page(doc, "5 资金方案的确定性回放")
    p(doc, "源码包包含独立回放脚本，使用临时数据库和显式假设，不依赖模型、浏览器或日常 data 目录。运行后自动断言可行性、资金缺口及工资延期修复日期。")
    code(doc, "uv run --frozen python " + "resources/competition_20261008/replay_plan_demo.py")
    table(doc, ["条件", "设定值"], [
        ("业务日与期限", "2026-09-30 至 2026-10-31"),
        ("日常余额", "6256 元"),
        ("有效预留", "房租 3200 元、生活费 3000 元、订阅 56 元"),
        ("购买需求", "电脑 6000 元"),
        ("显式预计事件", "每月 1 日工资 18000 元；每月 2 日房租 3200 元"),
    ], [4.3,12.9])
    h(doc, "回放结论", 2)
    p(doc, "立即购买发生 6000 元缺口，虽然预计期末自由余额仍为 12000 元；工资到账后购买路径可行。工资延期 1 天会让原计划失效，将购买改到 10 月 2 日并放在到账之后可修复。预演只写方案，不入账工资也不扣款。")
    h(doc, "与固定场景的区别", 2)
    p(doc, "启动时生成的历史流水按实际月份排列。2026年10月8日载入固定 9 月 30 日业务日时，在该业务日之前的完整月份匹配样本不足，工资及房租自动识别可能为空。回放脚本显式提供假设并保留 user 来源，避免把不存在的历史证据写入演示结论。")
    h(doc, "手动演示输入建议", 2)
    p(doc, "载入缺口演示场景后，明确输入：“我想买台 6000 元的电脑，下个月至少留 3000 元生活费。假设每月 1 日工资 18000 元，2 日房租 3200 元，计划到 2026年10月31日。”确认方案卡已列出这些假设，再展示挑战与修复。真实模型解析能力需在正式答辩前自行联调。")
    p(doc, "采用方案只让资金安排生效；实际购买或转存还有单独确认。条件提醒也不会自动转账；工资事件必须真正进入沙箱账务后，才能采纳建议并生成最终操作。")

    page(doc, "6 测试方法与结果")
    code(doc, r".\scripts\check.ps1" + "\nuv run --frozen python scripts/smoke_backend.py")
    p(doc, "check.ps1 顺序执行 Ruff、pytest、前端类型检查和构建。HTTP/SSE 脚本启动临时端口，阻塞等待事件并检查重放，无需浏览器。2026年10月8日本次运行：Ruff 通过，pytest 65 项通过，HTTP/SSE 通过，前端 build 通过。")
    table(doc, ["编号", "测试主题", "关键断言", "测试文件"], [
        ("T01", "确认与回执", "确认前零扣款，确认后记录真实回执", "test_banking.py"),
        ("T02", "并发去重", "8 路重复确认只记一次账", "test_banking.py"),
        ("T03", "精度与守恒", "非法金额拒绝，互转资产守恒", "test_banking.py"),
        ("T04", "业务约束", "风险、冻结、限额、余额重新校验", "test_banking.py"),
        ("T05", "恶意输入", "未知工具、伪造字段与数据注入不能授权", "test_banking.py"),
        ("T06", "连接与渠道", "来源检查、绑定、去重及跨渠道确认", "test_api.py"),
        ("T07", "会话与重启", "原会话确认，删除保留资金，旧库迁移", "test_conversations.py"),
        ("T08", "模型协议", "无依据总结拒绝，格式纠正不重放交易", "test_llm.py"),
        ("T09", "资金计划", "承诺释放、缺口、到账去重及分期", "test_planning.py"),
        ("T10", "只读诊断", "多阻碍一次列出，诊断后不扣款", "test_diagnostics.py"),
        ("T11", "证据与脱敏", "流水来源保存，模型请求不含完整手机号", "test_diagnostics.py"),
        ("T12", "事件恢复", "11 条 SSE 事件、重放及会话过滤", "smoke_backend.py"),
    ], [1.3,3.0,8.0,4.9], (0,))
    p(doc, "已配置模型后，可单独运行 uv run --frozen python scripts/smoke_model.py，检查真实工具协议与查询。该脚本会产生真实模型调用，但不确认资金变更；本次没有运行。飞书也需另行真实私聊和卡片联调。")

    page(doc, "7 打包数据管理与提交")
    h(doc, "7 1 源码包内容", 2)
    p(doc, "bankmate_源代码与部署材料.zip 包含当前 backend、frontend 源码、测试用例、README、pyproject.toml、uv.lock、前端 lockfile、启动及检查脚本、.env.example、飞书说明、本文 Word 与回放脚本。文件 SHA256 清单位于 docs/source_manifest.json，验证记录位于 docs/verification.json。")
    p(doc, "源码包排除 .env、日常数据库、日志、.git、.venv、node_modules、dist 和缓存，评委通过安装与 build 生成运行环境。源码取当前工作区，包含已经通过本次检查的未提交变更；包内清单是本次评审版本的核对依据。")
    h(doc, "7 2 备份与重置", 2)
    p(doc, "SQLite 使用 WAL，备份时应停止服务并妥善保存数据库，或使用 sqlite3 backup 接口获得一致快照。不要只在写入中复制单独 .sqlite3 文件。示例命令需要当前存在 data/banking.sqlite3。")
    code(doc, "uv run --frozen python -c 'import sqlite3; a=sqlite3.connect(\"data/banking.sqlite3\"); b=sqlite3.connect(\"data/banking.backup.sqlite3\"); a.backup(b); b.close(); a.close()'")
    p(doc, "“重置演示数据”会清空所有会话并重建沙箱；“载入缺口演示场景”也会修改模拟账务与业务日期。正式演示前先备份所需记录，确认不在处理中，再由团队手动操作。")
    h(doc, "7 3 常见故障", 2)
    table(doc, ["现象", "处理"], [
        ("页面无法访问", "检查本地 8000 端口及后端启动输出；必要时停止占用进程"),
        ("模型未配置或无法连接", "核对 .env 三个字段及接口前缀，重启后端后重试"),
        ("工具计划无效", "检查模型工具能力；按实际接口选择 auto、structured 或 native"),
        ("确认失败", "查当前余额、预留、卡片、限额、风险及有效期；重新发起需求"),
        ("自动工资假设缺失", "核对业务日期与两个完整月份证据，或明确提供假设"),
        ("飞书未启用", "保持默认关闭即可使用 Web；真实接入按 docs/feishu.md"),
    ], [5.0,12.2])
    p(doc, "提交前核对队伍名称“哈基米摸南北绿豆”、项目名称“bankmate”、成员姓名与平台信息；答辩 Word 转成平台支持的演示格式。技术文档与安全报告可用 DOCX，源码类别上传 ZIP。")
    return save(doc, "03_bankmate_源代码部署与测试说明.docx")


SLIDES = [
    ("作品定位",30),("用户需求与任务范围",45),("系统架构",60),("确认执行与工具边界",55),
    ("现金流路径检查",60),("计划版本与共享承诺",55),("转账与诊断演示",80),("资金方案演示",75),
    ("验证与安全边界",50),("项目价值与后续工作",30),
]


def slide(doc, number, points, speech, visual, demo=None):
    title, seconds = SLIDES[number-1]
    h(doc, f"第 {number} 页 {title}",2)
    p(doc, f"建议用时：{seconds} 秒    画面建议：{visual}", small=True)
    for idx, point in enumerate(points,1):
        p(doc, f"{idx}. {point}")
    p(doc, "讲稿："+speech,bold_lead="讲稿：")
    if demo:
        p(doc,"演示操作："+demo,bold_lead="演示操作：")


def defense(architecture):
    doc = new_doc("答辩提纲与讲稿")
    h(doc, "汇报安排")
    p(doc, "本文提供可转成 PPT 或 PDF 的 10 页内容、建议画面、讲稿和现场演示顺序。建议总用时 9 分钟，为切换页面及网络波动预留 1 分钟；正式汇报不超过 10 分钟。现场讲稿由队伍结合实际模型联调及手动界面检查调整。")
    p(doc, "项目名称为 bankmate，队伍为哈基米摸南北绿豆。汤范博文担任队长，郭展志、陈羽鑫、林旸昊、李志源为队员，均来自暨南大学金融科技。")
    table(doc,["页码","主题","用时"],[(str(i),title,f"{sec} 秒") for i,(title,sec) in enumerate(SLIDES,1)]+[("合计","内容与演示","540 秒  9 分钟")],[2.0,11.6,3.6],(0,2))
    p(doc,"正式上传须为 PPT、PPTX 或 PDF；本文 Word 是编辑与准备材料，不能直接上传到答辩材料分类。可用下列逐页内容制作 10 页演示文稿，架构图可从技术文档复用。")

    page(doc,"第 1 至 2 页 产品与需求")
    slide(doc,1,["bankmate 是个人中文银行智能体。","用查询依据、资金预演和用户确认连接一个完整任务。","Vue、FastAPI 与本地模拟银行。"],
        "各位评委好，我们的作品是 bankmate。用户可以直接说一个目标，比如买电脑之后还要留够生活费。系统先读模拟银行数据，计算资金安排，再把需要执行的操作交给用户确认。我们的重点是把需求理解、业务证据和执行控制连成一个能复核的流程。",
        "项目名称、队伍、一个自然语言目标")
    slide(doc,2,["单个目标常跨转账、账单、订阅与储蓄。","只看当前余额或期末余额，可能忽略中途缺口。","用户需要知道建议依据和每一步是否真正完成。"],
        "我们选择的场景是个人财务任务。一个需求可能同时包含查支出、取消订阅和转存储蓄。银行工具提供事实，模型协助理解与组织；但资金能否安排、业务能否执行，都由后端按当前账务检查。这样用户可以核对建议，也能区分正在准备和已经完成。",
        "六类银行能力及购买计划示例")

    page(doc,"第 3 至 4 页 架构与执行")
    slide(doc,3,["Vue 展示聊天、数据、方案和确认。","FastAPI 负责请求校验、任务与 SSE 事件。","Agent 调度工具，Bank 与 Planning 完成业务计算和事务。"],
        "系统分为界面、编排、业务和存储四层。模型通过兼容接口提出工具调用，Agent 逐步执行。查询结果直接成为数据卡；资金工具进入确认等待。SQLite 保存任务、参数、流水、计划版本与事件，刷新或重启后仍可查到状态。我们没有引入额外中间件，便于评委本地复现。",
        "复用技术文档图 1")
    slide(doc,4,["24 个工具：11 个查询类，13 个确认变更类。","固定收款实体和金额，确认卡 15 分钟有效。","确认时复核，重复点击返回原回执。"],
        "模型只能提出操作，不能替用户点击确认。后端会把收款人解析成固定 ID，把金额和业务类型保存。确认时重新检查余额、预算、卡片、限额与风险。同一确认只执行一次；组合任务一步失败就停止后续，并保留已完成回执。这些约束在服务端执行。",
        "需求  查询  确认  事务  回执五步流程")

    page(doc,"第 5 至 6 页 资金预演与计划")
    slide(doc,5,["可自由安排余额等于账面余额减有效预留。","按事件顺序检查，每个时点都不能出现缺口。","工资延期挑战在 0 至 30 天内找首个失效点。"],
        "资金方案不是模型估一个余额。系统读取当前余额和承诺，按收入、房租、订阅和目标动作逐事件计算。我们的演示里，立即买电脑和等工资后买，期末自由余额都能到一万二，但立即买会先出现六千元缺口。所以期末相同，不代表路径可行。工资延期挑战还能指出方案最先失效的条件。",
        "两种执行时点比较与首次缺口")
    table(doc,["方案","期末自由余额","中途缺口","结果"],[("立即买电脑","12000 元","6000 元","不可行"),("工资后买","12000 元","无","可行")],[4.6,4.6,4.6,3.4],(3,))
    slide(doc,6,["预演保存版本，采用计划才让资金预留生效。","预算在会话间共享，阻止重复使用已安排资金。","累计目标仅执行差额，条件建议仍要用户采纳和确认。"],
        "不同聊天可以有独立上下文，但不能把同一笔钱安排两次。我们把预算和目标保存到共享账务层。用户改口时沿用原计划，比如已经存了五百，再改为累计七百，只处理二百元差额。工资到账后的提醒也只是建议，不会自动转账。",
        "版本与承诺示意、500 到 700 的目标差额")

    page(doc,"第 7 至 8 页 现场演示")
    slide(doc,7,["转账确认前余额不变，确认后出现回执。","两位李明需要澄清，不按姓名猜收款人。","理财诊断一次列出风险等独立阻碍，诊断不扣款。"],
        "先看一次普通转账。我发送给张三转二百元，系统显示收款信息和金额，此时余额不变。核对后确认，才看到扣款和回执。再问为什么买不了远见成长，系统按当前状态列出风险测评等阻碍，这条诊断不创建交易。用户既知道下一步，也保留最终控制。",
        "实际界面由团队提前手动核验", "从初始场景发起转账，检查金额后确认；随后做只读申购诊断。重名澄清可作为备用演示，网络延迟时直接使用已准备的静态回执画面。")
    slide(doc,8,["固定场景余额 6256 元，全额用于房租、生活费和订阅。","显式设定工资 18000 元每月 1 日、房租 3200 元每月 2 日。","延期 1 天使原计划失效，推迟购买至 10 月 2 日可修复。"],
        "第二个演示是买六千元电脑并保留生活费。这里使用固定业务日期和显式假设，不把预测工资当成已到账。原资金已经全部安排，立即购买有缺口，工资后购买可行。挑战工资延期一天，原购买时点失效；系统找到新的执行日期，保存新版本并重新核验。采用计划之后，具体扣款仍要另一次确认。",
        "现金流卡、挑战结果与修复版本", "完整确定性结果可由源码回放脚本生成。现场使用页面前，应确认方案卡列出了全部假设；当前固定日期可能缺少自动推断历史样本，不能声称所有假设都自动识别。")

    page(doc,"第 9 至 10 页 验证与价值")
    slide(doc,9,["65 项后端测试及实际 HTTP/SSE 检查通过。","前端类型检查和 build 通过，交互由团队手动验证。","当前为单身份本地沙箱，没有真实银行或多租户安全认证。"],
        "验证覆盖确认前零扣款、八路并发只执行一次、余额复核、风险限制、金额精度、提示注入、跨会话和服务重启。我们也检查了真实本地 HTTP 与事件恢复。需要明确，真实模型和飞书尚需联调，当前是单身份模拟产品。安全报告已列出认证、数据保护和审计方面的剩余风险。",
        "验证结果表与明确的产品边界")
    slide(doc,10,["把自然语言目标变成可核对的银行工作流。","确定性资金预演和共享预算约束可复现。","后续优先补真实联调、日期场景与身份授权。"],
        "bankmate 的价值是让用户在执行前看清资金路径，在执行时保留控制，在执行后拿到可追溯结果。我们交付了源码、部署与测试说明、技术文档和安全自评。下一步先完成真实接口联调和场景回归，再按实际需求完善身份授权。感谢各位评委。",
        "三项实现价值及可复现交付物")

    page(doc,"评委问答准备")
    for title,answer in [
        ("模型能否绕过用户直接转账", "模型只能调用白名单中的准备入口。13 类变更都暂停等待确认；真正的写入由确认事务执行。模拟工资和重置是专用沙箱管理入口，权限不足风险已单列。"),
        ("是不是普通聊天加接口", "除查询与确认外，系统保存目标、版本、资金承诺和实际完成额；跨会话检查共享预算，用完整事件路径而非单个余额判断计划。"),
        ("压力测试是否保证现实安全", "不能。当前只对既定假设进行单因素搜索，工资延期范围为 0 至 30 天；没有覆盖真实到账时刻、多因素冲击或全局最优策略。"),
        ("为什么固定场景要输入工资假设", "种子流水按实际月份生成，固定业务日可能缺少两个月前置证据。显式假设会标记来源，不伪造自动识别结论；问题已列为剩余风险。"),
        ("两个会话会不会重复使用同一笔钱", "会话上下文独立，账户和承诺共享。确认时在事务中再次检查已安排资金；业务重放用例及共享数据逻辑是核对依据，生产多用户仍需对象授权。"),
        ("任务中途失败怎么办", "停止后续，保存已执行步骤和回执；没有整组事务回滚。服务重启也不会自动重做已执行业务。用户应核对回执后再重试。"),
        ("是否接入真实银行和真实投资产品", "没有。余额、持仓、联系人及净值都是模拟数据，申赎即时结算，风险问卷为演示规则；没有收益承诺。"),
        ("如何复现和核对版本", "解压源码 ZIP 后运行 scripts/start.ps1，检查用 scripts/check.ps1，HTTP 验证和资金方案回放各有脚本。源码 SHA256 清单可核对每个提交文件。"),
    ]:
        h(doc,title,2)
        p(doc,answer)
    return save(doc,"02_bankmate_答辩提纲与讲稿.docx")


REPLAY = '''"""临时数据库回放，不连接模型、不改日常沙箱。"""
import tempfile
from pathlib import Path
from banking.db import Database
from banking.planning import PlanningService

with tempfile.TemporaryDirectory(prefix="bankmate_replay_") as tmp:
    database = Database(Path(tmp) / "bank.sqlite3")
    database.initialize()
    service = PlanningService(database)
    with database.transaction() as db:
        service.seed_demo_scenario(db)
        plan = service.preview(db, database.conversation_id(db), {
            "kind": "purchase", "title": "购买电脑方案",
            "action_label": "购买电脑", "amount_minor": 600000,
            "living_reserve_minor": 300000, "account_id": "daily",
            "salary_amount_minor": 1800000, "salary_day": 1,
            "rent_amount_minor": 320000, "rent_day": 2,
            "deadline": "2026-10-31",
        })
        variants = {v["key"]: v for v in plan["variants"]}
        assert not variants["now"]["result"]["feasible"]
        assert variants["now"]["result"]["first_failure"]["shortfall_minor"] == 600000
        assert variants["after_salary"]["result"]["feasible"]
        assert variants["after_salary"]["result"]["ending_available_minor"] == 1200000
        challenge = service.challenge(db, plan["plan_id"], plan["version"], "salary_delay")["challenge"]
        assert challenge["minimum_days"] == 1
        assert challenge["repair"]["date"] == "2026-10-02"
        assert challenge["repair"]["result"]["feasible"]
        print("回放通过：立即购买缺口 6000 元；工资后购买可行；延期 1 天失效；10 月 2 日修复通过。")
'''


def package(documents, deployment):
    (WORK / "replay_plan_demo.py").write_text(REPLAY,encoding="utf-8")
    verification = EVIDENCE["verification"] | {"date":"2026-10-08","team_name":TEAM,"project_name":PROJECT,"replay_plan_demo":"passed"}
    archive = OUT / "bankmate_源代码与部署材料.zip"
    selected = [ROOT/name for name in ["README.md","pyproject.toml","uv.lock",".env.example",".gitignore"]]
    for name in ["backend/banking","backend/tests","frontend/src","scripts","docs"]:
        selected.extend(pth for pth in (ROOT/name).rglob("*") if pth.is_file() and "__pycache__" not in pth.parts and pth.suffix not in {".pyc",".log"})
    selected.extend(ROOT/"frontend"/name for name in ["package.json","package-lock.json","tsconfig.json","vite.config.ts","index.html"])
    manifest = {str(path.relative_to(ROOT)).replace("\\","/"):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(selected)) if path.is_file()}
    with ZipFile(archive,"w",ZIP_DEFLATED) as zf:
        for name in manifest:
            zf.write(ROOT/name,name)
        zf.write(deployment,"docs/03_bankmate_源代码部署与测试说明.docx")
        zf.write(WORK/"replay_plan_demo.py","resources/competition_20261008/replay_plan_demo.py")
        manifest["docs/03_bankmate_源代码部署与测试说明.docx"] = hashlib.sha256(deployment.read_bytes()).hexdigest()
        manifest["resources/competition_20261008/replay_plan_demo.py"] = hashlib.sha256((WORK/"replay_plan_demo.py").read_bytes()).hexdigest()
        zf.writestr("docs/source_manifest.json",json.dumps(manifest,ensure_ascii=False,indent=2).encode("utf-8"))
        zf.writestr("docs/verification.json",json.dumps(verification,ensure_ascii=False,indent=2).encode("utf-8"))
    (WORK/"source_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    all_zip = OUT / "bankmate_参赛材料_Word版及源码.zip"
    with ZipFile(all_zip,"w",ZIP_DEFLATED) as zf:
        for path in documents+[archive]:
            zf.write(path,path.name)
    print(f"ZIP {archive.name} {archive.stat().st_size} bytes")
    print(f"ZIP {all_zip.name} {all_zip.stat().st_size} bytes")


if __name__ == "__main__":
    OUT.mkdir(parents=True,exist_ok=True)
    architecture=diagram()
    tech=technical(architecture)
    safe=security()
    deployment=manual()
    speech=defense(architecture)
    package([tech,speech,deployment,safe],deployment)
