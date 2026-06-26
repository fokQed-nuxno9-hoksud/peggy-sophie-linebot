"""
Eva 知識庫更新腳本
-------------------
每次 JID/牌價 的牌價表更新後，在本地執行這個腳本，
它會自動找最新的檔案、抽出品牌和型號，重新產生 eva_knowledge.txt。

執行方式：
    python3 build_eva_knowledge.py

執行完後把 eva_knowledge.txt commit + push 即可（Render 會自動重新部署）。

注意：不含任何價格資訊，Eva 只回答「有代理 / 沒有代理」。
"""

import os, glob, re
from datetime import datetime

try:
    import openpyxl
except ImportError:
    os.system("pip install openpyxl -q")
    import openpyxl

try:
    import xlrd
except ImportError:
    os.system("pip install xlrd -q")
    import xlrd

JID_PRICE_DIR = "/Users/user/Library/CloudStorage/GoogleDrive-s9220320@gmail.com/我的雲端硬碟/JID/牌價"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "eva_knowledge.txt")

# ── 找最新的牌價檔案 ─────────────────────────────────────────────

def find_latest(pattern: str) -> str | None:
    """找符合 pattern 的最新檔案（依修改時間）"""
    files = glob.glob(os.path.join(JID_PRICE_DIR, pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def extract_date_from_filename(path: str) -> str:
    """從檔名抽日期字串，找不到就用檔案修改時間"""
    name = os.path.basename(path)
    m = re.search(r'(\d{4}-\d{1,2}-\d{1,2}|\d{8})', name)
    if m:
        return m.group(1)
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")

# ── 讀 .xls（xlrd）─────────────────────────────────────────────

def read_xls_brands_models(path: str) -> dict[str, list[str]]:
    """回傳 {sheet_name: [model1, model2, ...]}，只取第一欄非空值作為型號"""
    wb = xlrd.open_workbook(path)
    result: dict[str, list[str]] = {}
    skip_sheets = {
        "Sheet1", "Power supply #1", "VS日幣", "mViz software",
        "Basler(封存)", "Basler凌華價格", "非主流產品", "PV500&PV200",
        "Saber1(IMPERX)", "NorPix", "CCS BTC",
    }
    for name in wb.sheet_names():
        if name in skip_sheets:
            continue
        ws = wb.sheet_by_name(name)
        models = []
        for i in range(ws.nrows):
            raw = ws.row_values(i)
            val = str(raw[0]).strip() if raw else ""
            # 跳過標題列、空列、明顯非型號的列
            if not val or len(val) < 2:
                continue
            if val in ("型號", "ACE SERIES", "GIGE", "USB3", "CXP", "GigE", "品名", ""):
                continue
            if val.startswith(("●", "※", "#", "(")):
                continue
            if re.match(r'^[\d,\.]+$', val):  # 純數字跳過
                continue
            models.append(val)
        if models:
            result[name] = models
    return result

# ── 讀 .xlsx（openpyxl）─────────────────────────────────────────

def read_xlsx_brands_models(path: str) -> dict[str, list[str]]:
    """回傳 {sheet_name: [model1, model2, ...]}"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    result: dict[str, list[str]] = {}
    skip_sheets = {"Sheet1"}
    for name in wb.sheetnames:
        if name in skip_sheets:
            continue
        ws = wb[name]
        models = []
        for row in ws.iter_rows(max_col=1, values_only=True):
            val = str(row[0]).strip() if row[0] is not None else ""
            if not val or len(val) < 2:
                continue
            if val in ("型號", "品   名", "品名", ""):
                continue
            if val.startswith(("●", "※", "#", "(", '"')):
                continue
            if re.match(r'^[\d,\.]+$', val):
                continue
            models.append(val)
        if models:
            result[name] = models
    wb.close()
    return result

# ── 產生知識庫文字 ───────────────────────────────────────────────

def build_knowledge(cam_path: str | None, lens_path: str | None) -> str:
    lines = []
    today = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"# JIDIN_Peggy 產品代理清單（Eva 專用知識庫）")
    lines.append(f"# 更新日期：{today}")
    lines.append(f"# 用途：Eva 只回答「有代理 / 沒有代理」，不透露任何價格")
    lines.append("")

    if cam_path:
        cam_date = extract_date_from_filename(cam_path)
        lines.append(f"## 工業相機 & 光源 & 周邊設備（牌價表日期：{cam_date}）")
        lines.append("")
        brands_models = read_xls_brands_models(cam_path)
        for brand, models in brands_models.items():
            lines.append(f"### {brand}")
            for m in models[:30]:  # 每品牌最多 30 個型號
                lines.append(f"  - {m}")
            if len(models) > 30:
                lines.append(f"  （共 {len(models)} 筆，以上為部分型號）")
            lines.append("")
    else:
        lines.append("## 工業相機（牌價表未找到）")
        lines.append("")

    if lens_path:
        lens_date = extract_date_from_filename(lens_path)
        lines.append(f"## 鏡頭（牌價表日期：{lens_date}）")
        lines.append("")
        brands_models = read_xlsx_brands_models(lens_path)
        for brand, models in brands_models.items():
            lines.append(f"### {brand}")
            for m in models[:30]:
                lines.append(f"  - {m}")
            if len(models) > 30:
                lines.append(f"  （共 {len(models)} 筆，以上為部分型號）")
            lines.append("")
    else:
        lines.append("## 鏡頭（牌價表未找到）")
        lines.append("")

    lines.append("## Eva 回覆規則")
    lines.append("")
    lines.append("- 客戶問「有沒有代理 X 品牌」→ 查上方清單，有就回「有代理」，沒有就回「目前未代理」")
    lines.append("- 客戶問「有沒有 X 型號」→ 查上方清單，有就確認，沒有或不確定就 [CONFIDENCE: LOW]")
    lines.append("- 任何報價、折扣、價格相關問題 → 一律 [CONFIDENCE: LOW]，轉 Peggy 確認")
    lines.append("- 交期、庫存、客製規格 → 一律 [CONFIDENCE: LOW]，轉 Peggy 確認")
    lines.append("- 應用場景建議（AOI、外觀檢測、尺寸量測等）→ 根據品牌知識建議，[CONFIDENCE: HIGH]")
    lines.append("")

    return "\n".join(lines)

# ── 主程式 ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("正在尋找最新牌價表...")

    # 相機牌價表：匹配「(業務)Vision牌價表20*.xls」（排除鏡頭）
    cam_path = find_latest("(業務)Vision牌價表[0-9]*.xls")
    if not cam_path:
        cam_path = find_latest("*牌價表[0-9]*.xls")
    print(f"  相機牌價表：{os.path.basename(cam_path) if cam_path else '未找到'}")

    # 鏡頭牌價表：匹配「(業務)Vision牌價表-鏡頭*.xlsx」
    lens_path = find_latest("*鏡頭*.xlsx")
    print(f"  鏡頭牌價表：{os.path.basename(lens_path) if lens_path else '未找到'}")

    print("正在抽取品牌/型號資料（不含價格）...")
    content = build_knowledge(cam_path, lens_path)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    lines = content.count("\n")
    size = len(content.encode("utf-8"))
    print(f"完成！eva_knowledge.txt 已更新（{lines} 行，{size} bytes）")
    print(f"路徑：{OUTPUT_PATH}")
    print("")
    print("下一步：")
    print("  cd /Users/user/Downloads/Peggy_agent/line_bot")
    print("  git add eva_knowledge.txt && git commit -m 'update: Eva knowledge base' && git push origin main")
