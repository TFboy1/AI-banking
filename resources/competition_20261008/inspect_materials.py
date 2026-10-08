"""Word 原生导出后的逐页 PNG 与结构检查，不交付 QA 中间产物。"""
import json
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

WORK = Path(__file__).resolve().parent
ROOT = WORK.parents[1]
OUT = ROOT / "outputs/bankmate_参赛材料_20261008"
POPPLER = r"C:\Users\86150\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin"
report = {}
for docx in sorted(OUT.glob("*.docx")):
    key = docx.name[:2]
    folder = WORK / "qa" / key
    pdf = next(folder.glob("*.pdf"))
    reader = PdfReader(pdf)
    page_texts = [page.extract_text() or "" for page in reader.pages]
    png_paths = convert_from_path(str(pdf),dpi=140,fmt="png",output_folder=str(folder),paths_only=True,poppler_path=POPPLER)
    normalized=[]
    for idx,path in enumerate(png_paths,1):
        destination=folder/f"page-{idx}.png"
        Path(path).replace(destination)
        normalized.append(destination)
    doc=Document(docx)
    content="\n".join([p.text for p in doc.paragraphs]+[c.text for t in doc.tables for r in t.rows for c in r.cells])
    assert "哈基米摸南北绿豆" in content and "bankmate" in content
    with ZipFile(docx) as zf:
        assert zf.testzip() is None
        assert "word/document.xml" in zf.namelist()
    font=ImageFont.truetype("C:/Windows/Fonts/msyh.ttc",20)
    groups=[]
    for start in range(0,len(normalized),6):
        subset=normalized[start:start+6]
        sheet=Image.new("RGB",(1260,970),"#E6E9ED")
        draw=ImageDraw.Draw(sheet)
        for j,path in enumerate(subset):
            with Image.open(path) as im:
                im.thumbnail((395,430))
                x=(j%3)*420+(420-im.width)//2
                y=(j//3)*485+38
                sheet.paste(im,(x,y))
                draw.text(((j%3)*420+18,(j//3)*485+8),f"{key} / page {start+j+1}",font=font,fill="black")
        preview=folder/f"contact-{start//6+1}.png"
        sheet.save(preview)
        groups.append(str(preview))
    report[key]={"document":docx.name,"pages":len(page_texts),"chars_per_page":[len(x.strip()) for x in page_texts],"sparse_pages":[i+1 for i,t in enumerate(page_texts) if len(t.strip())<150],"page_starts":[t[:90].replace("\n"," ") for t in page_texts],"contact_sheets":groups}
    (folder/"page_text.json").write_text(json.dumps(page_texts,ensure_ascii=False,indent=2),encoding="utf-8")
(WORK/"qa_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
