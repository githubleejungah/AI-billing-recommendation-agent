"""비용처리 서류(DOC). 서류 데이터 하나를 만들고, 그것을 PDF·DOCX·XLSX로 각각 쓴다(plan.md 4.3).

- 숫자는 모두 시뮬레이터 결과(SIM-05 월별 일정)에서 나온다. AI가 쓴 문장은 넣지 않는다(DOC-02).
- 금액은 부가세 별도(공급가액)로 계산하고, 세액은 월마다 공급가액의 10%, 원 미만 버림(SIM-06).
  12개월 합계는 월별 값을 더한 것이다.
- 같은 입력이면 같은 내용이 나온다. 문서 번호와 발행일만 다르다(DOC-06).
- 서류에는 고객 이름이 들어가므로 보호 구역에서만 만들고, 외부 AI로 보내지 않는다(DOC-07).
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import audit
import captions
import charts
import fonts
import history
import simulator

VAT_RATE_PCT = 10  # T5a에서 정책 파일 값으로 바꾼다.
REVIEW_VALID_DAYS = 30  # CHG-07: 발행 후 30일과 이벤트 신청 마감일 중 이른 날
KINDS = {"review": ("비용 절감 검토서", "RV"), "confirmation": ("요금제 변경 확인서", "CF")}
NOTICE = [
    "이 서류는 고객사 내부 결재에 첨부하기 위한 자료입니다. 비용 증빙은 매월 발행되는 세금계산서입니다.",
    "금액은 부가세 별도(공급가액) 기준으로 계산하고, 세액은 월마다 공급가액의 10%(원 미만 버림)로 계산했습니다.",
    "예상 금액은 최근 이용 기록의 평균 사용량이 앞으로도 같다고 가정한 값입니다. 실제 청구액은 실제 사용량에 따라 달라집니다.",
]


class DocumentError(Exception):
    pass


# --- 금액 ---

def vat(supply: int, rate_pct: int = VAT_RATE_PCT) -> dict:
    tax = supply * rate_pct // 100
    return {"supply": supply, "tax": tax, "total": supply + tax}


def _sum(rows: list[dict]) -> dict:
    return {key: sum(row[key] for row in rows) for key in ("supply", "tax", "total")}


def won(amount: int) -> str:
    return f"{amount:,}원"


def discount_text(promo: simulator.Promotion) -> str:
    return f"{promo.discount_value}%" if promo.discount_type == "percent" else f"{promo.discount_value:,}원"


# --- 서류 데이터 ---

def _identity(conn: sqlite3.Connection, user_id: str) -> dict:
    row = conn.execute(
        "SELECT u.name, c.code FROM users u JOIN customer_codes c USING (user_id) WHERE u.user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise simulator.CustomerNotFound(user_id)
    return {"name": row[0], "code": row[1]}


def _promotion(conn: sqlite3.Connection, promo_id: str | None) -> dict | None:
    if not promo_id:
        return None
    promo = next(p for p in simulator.load_promotions(conn) if p.promo_id == promo_id)
    return {"promo_id": promo.promo_id, "name": promo.promo_name, "discount": discount_text(promo),
            "months": promo.duration_months, "valid_to": promo.valid_to,
            "min_tenure": f"{promo.min_tenure_months}개월" if promo.min_tenure_months else "제한 없음"}


def build_data(conn: sqlite3.Connection, user_id: str, kind: str = "review", change: dict | None = None) -> dict:
    """서류 데이터(문서 번호·발행일 제외). 같은 입력이면 같은 값이다."""
    if kind not in KINDS:
        raise DocumentError(f"알 수 없는 서류 종류: {kind}")
    if kind == "confirmation" and not change:
        raise DocumentError("변경 확인서에는 변경 접수 정보가 필요합니다.")
    sim = simulator.simulate(conn, user_id)
    if sim["status"] != "ok":
        raise DocumentError("이용 기록이 없어 서류를 만들 수 없습니다.")
    rec, sched = sim["recommendation"], sim["schedule"]
    plans = {p.plan_id: p for p in simulator.load_plans(conn)}
    current_plan = plans[sim["current_plan_id"]]
    promo = _promotion(conn, rec["promo_id"])

    monthly = []
    for base_row, rec_row in zip(sched["baseline"], sched["recommended"]):
        monthly.append({"month": base_row["month"], "discount": rec_row["discount"],
                        "baseline": vat(base_row["total"]), "recommended": vat(rec_row["total"]),
                        "parts": {"base_after_discount": rec_row["base"] - rec_row["discount"],
                                  "api_overage": rec_row["api_overage"], "storage_overage": rec_row["storage_overage"]}})
    baseline_12m = _sum([m["baseline"] for m in monthly])
    recommended_12m = _sum([m["recommended"] for m in monthly])
    saving_12m = {key: baseline_12m[key] - recommended_12m[key] for key in baseline_12m}
    if baseline_12m["supply"] != rec["current_total_12m"] or recommended_12m["supply"] != rec["recommended_total_12m"]:
        raise DocumentError("월별 일정과 12개월 총액이 맞지 않습니다.")  # 시뮬레이터 결과와 어긋나면 만들지 않는다.

    title, _prefix = KINDS[kind]
    label = rec["plan_name"] + (f" + {promo['name']}" if promo else "")
    data = {
        "kind": kind,
        "title": title,
        "customer": _identity(conn, user_id),
        "user_id": user_id,
        "as_of": sim["as_of"],
        "current_plan": current_plan.plan_name,
        "recommended_plan": rec["plan_name"],
        "action": rec["action"],
        "promotion": promo,
        "comparison": [
            {"label": f"현재 요금제 유지({current_plan.plan_name})", **baseline_12m},
            {"label": f"추천: {label}", **recommended_12m},
            {"label": "절감액", **saving_12m},
        ],
        "saving_pct": rec["saving_pct"],
        "monthly": monthly,
        "after_promotion_monthly": vat(sched["recommended"][-1]["total"]),
        "usage": [{"month": u["target_month"], "api_calls": u["api_calls_used"], "storage_gb": u["storage_gb_used"]}
                  for u in sim["usage_history"]],
        "forecast": _forecast(sim["forecast"]),
        "peak_months": sim["seasonality"]["peak_months"],
        "season": _season(sim, monthly),
        "included": {"current": {"plan": current_plan.plan_name, "api_calls": current_plan.included_api_calls,
                                 "storage_gb": current_plan.included_storage_gb},
                     "recommended": {"plan": rec["plan_name"], "api_calls": plans[rec["plan_id"]].included_api_calls,
                                     "storage_gb": plans[rec["plan_id"]].included_storage_gb}},
        "stability": sim["stability"]["status"],
        "stability_detail": _stability_detail(sim),
        "basis": [
            f"요금표 기준일: {sim['as_of']}",
            _forecast_basis(sim["forecast"]),
            f"비교 기간: {sched['months'][0]} ~ {sched['months'][-1]} (요금제 변경이 적용되는 다음 청구 주기부터 12개월)",
            f"추천 안정성: {sim['stability']['status']} (사용량 ±{sim['stability']['change_pct']}% 기준)",
        ],
        "notice": NOTICE,
        "change": change,
    }
    data["captions"] = captions.all_captions(data)  # 해시에 포함되도록 서류 데이터에 넣는다(DOC-06).
    return data


def _stability_detail(sim: dict) -> dict:
    """안정성 검사에서 추천이 바뀌는 쪽만 남긴다. 바뀌지 않는 쪽은 None."""
    st, rec = sim["stability"], sim["recommendation"]
    key = (rec["plan_id"], rec["promo_id"])
    detail = {"change_pct": st["change_pct"]}
    for case in ("if_down", "if_up"):
        alt = st[case]
        detail[case] = None if (alt["plan_id"], alt["promo_id"]) == key else alt["plan_name"]
    return detail


def _forecast(f: dict) -> dict:
    out = {"method": f["method"], "months": f["months"], "api_calls": f["api_calls"], "storage_gb": f["storage_gb"]}
    if f["method"] == "계절 반영":
        out["growth"] = f["growth"]
        out["monthly"] = [{"month": m["month"], "base_month": m["base_month"], "api_calls": m["api_calls"],
                           "storage_gb": m["storage_gb"]} for m in f["monthly"]]
    return out


def _forecast_basis(f: dict) -> str:
    if f["method"] == "계절 반영":
        return (f"예상 사용량: 계절 반영(SIM-08). 최근 12개월 기록 중 같은 달 사용량 × 성장률"
                f"(API {f['growth']['api_calls']}배, 스토리지 {f['growth']['storage_gb']}배)")
    return (f"예상 사용량: {', '.join(f['months'])} 평균 (API {f['api_calls']:,}회, "
            f"스토리지 {f['storage_gb']:g}GB)")


def _season(sim: dict, monthly: list[dict]) -> dict | None:
    """다가오는 성수기(DOC-08). 성수기가 없거나 비교 기간 안에 없으면 None."""
    season = sim["seasonality"]
    upcoming = season.get("upcoming")
    if season["status"] != "있음" or not upcoming:
        return None
    rows = [m for m in monthly if m["month"] in upcoming["months"]]
    current, recommended = _sum([m["baseline"] for m in rows]), _sum([m["recommended"] for m in rows])
    if current["supply"] != upcoming["current_bill"] or recommended["supply"] != upcoming["recommended_bill"]:
        raise DocumentError("성수기 청구액이 시뮬레이터 결과와 맞지 않습니다.")
    evidence = []
    for bundle in season["bundles"]:
        first = next(e for e in season["evidence"] if e["calendar_month"] == bundle[0])
        evidence.append({"label": simulator.month_label(bundle), "occurrences": first["occurrences"]})
    return {"peak_label": ", ".join(simulator.month_label(b) for b in season["bundles"]),
            "upcoming_label": upcoming["label"], "months": upcoming["months"], "deadline": upcoming["deadline"],
            "current": current, "recommended": recommended, "evidence": evidence}


def season_rows(doc: dict) -> tuple[list[list[str]], str]:
    """서류의 "다가오는 성수기" 표. 모든 형식이 같은 행을 쓴다."""
    season = doc["season"]
    period = f"{season['upcoming_label']} ({season['months'][0]} ~ {season['months'][-1]})"
    return [["구분", "공급가액", "세액", "합계"],
            [f"현재 요금제 유지({doc['current_plan']})", *(f"{season['current'][k]:,}" for k in ("supply", "tax", "total"))],
            [f"추천({doc['recommended_plan']})", *(f"{season['recommended'][k]:,}" for k in ("supply", "tax", "total"))]], period


def content_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def stamp(data: dict, issued: date) -> dict:
    """문서 번호, 발행일, 유효기간을 붙인다. 유효기간은 발행 후 30일과 이벤트 신청 마감일 중 이른 날(CHG-07)."""
    digest = content_hash(data)
    _title, prefix = KINDS[data["kind"]]
    valid_until = issued + timedelta(days=REVIEW_VALID_DAYS)
    if data["promotion"]:
        valid_until = min(valid_until, date.fromisoformat(data["promotion"]["valid_to"]))
    if data["kind"] == "confirmation":
        valid_until = None  # 확인서는 기록 문서라 유효기간이 없다.
    number = f"{prefix}-{issued:%Y%m%d}-{data['customer']['code'].replace('-', '')}-{digest[:6].upper()}"
    return {**data, "document_no": number, "issued_at": issued.isoformat(),
            "valid_until": valid_until.isoformat() if valid_until else None, "sha256": digest}


# --- 형식별 쓰기 ---

def _summary_rows(doc: dict) -> list[tuple[str, str]]:
    rows = [("문서 번호", doc["document_no"]), ("발행일", doc["issued_at"])]
    if doc["valid_until"]:
        rows.append(("유효기간", f"{doc['valid_until']}까지"))
    rows += [("고객", f"{doc['customer']['name']} ({doc['customer']['code']})"),
             ("현재 요금제", doc["current_plan"]), ("추천 요금제", doc["recommended_plan"])]
    promo = doc["promotion"]
    if promo:
        rows += [("이벤트", promo["name"]),
                 ("할인", f"{promo['discount']}, {promo['months']}개월 (신청 마감 {promo['valid_to']})"),
                 ("할인 종료 후 평소 월 요금" if doc["season"] else "할인 종료 후 월 요금",
                  f"{won(doc['after_promotion_monthly']['supply'])} (부가세 별도)")]
    if doc["season"]:
        rows += [("성수기", f"{doc['season']['peak_label']} (2년 연속)"),
                 ("다음 성수기 신청 마감", f"{doc['season']['deadline']}까지")]
    change = doc["change"]
    if change:
        rows += [("변경 접수 번호", change["request_id"]), ("적용일", change["effective_date"]),
                 ("취소 가능 기한", f"{change['cancel_deadline']}까지")]
    return rows


def to_xlsx(doc: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = Font(bold=True)
    fill = PatternFill("solid", fgColor="EEEDEA")
    money = "#,##0"

    ws = wb.active
    ws.title = "요약"
    ws.append([doc["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for key, value in _summary_rows(doc):
        ws.append([key, value])
    ws.append([])
    ws.append(["12개월 비교(원)", "공급가액", "세액", "합계"])
    for cell in ws[ws.max_row]:
        cell.font, cell.fill = head, fill
    for row in doc["comparison"]:
        ws.append([row["label"], row["supply"], row["tax"], row["total"]])
        for cell in ws[ws.max_row][1:]:
            cell.number_format = money
    ws.append(["절감률(공급가액 기준)", f"{doc['saving_pct']}%"])
    ws.append([])
    if doc["season"]:
        rows, period = season_rows(doc)
        ws.append([f"다가오는 성수기(원): {period}, 신청 마감 {doc['season']['deadline']}"])
        ws.append(rows[0])
        for cell in ws[ws.max_row]:
            cell.font, cell.fill = head, fill
        for key, row in zip(("current", "recommended"), rows[1:]):
            amount = doc["season"][key]
            ws.append([row[0], amount["supply"], amount["tax"], amount["total"]])
            for cell in ws[ws.max_row][1:]:
                cell.number_format = money
        ws.append([])
    for line in doc["notice"]:
        ws.append([line])
    ws.column_dimensions["A"].width = 34
    for col in "BCD":
        ws.column_dimensions[col].width = 18

    ws = wb.create_sheet("월별")
    ws.append(["월", "현재 공급가액", "현재 세액", "현재 합계", "추천 공급가액", "추천 세액", "추천 합계", "추천 할인액"])
    for cell in ws[1]:
        cell.font, cell.fill = head, fill
        cell.alignment = Alignment(horizontal="center")
    for m in doc["monthly"]:
        b, r = m["baseline"], m["recommended"]
        ws.append([m["month"], b["supply"], b["tax"], b["total"], r["supply"], r["tax"], r["total"], m["discount"]])
    for row in ws.iter_rows(min_row=2, min_col=2):
        for cell in row:
            cell.number_format = money
    for col in "ABCDEFGH":
        ws.column_dimensions[col].width = 14

    ws = wb.create_sheet("근거")
    ws.append(["월", "API 호출(회)", "스토리지(GB)"])
    for cell in ws[1]:
        cell.font, cell.fill = head, fill
    for u in doc["usage"]:
        ws.append([u["month"], u["api_calls"], u["storage_gb"]])
    ws.append([])
    inc = doc["included"]
    ws.append(["기본 제공량", "API 호출(회)", "스토리지(GB)"])
    ws.append([inc["current"]["plan"] + "(현재)", inc["current"]["api_calls"], inc["current"]["storage_gb"]])
    ws.append([inc["recommended"]["plan"] + "(추천)", inc["recommended"]["api_calls"], inc["recommended"]["storage_gb"]])
    if doc["forecast"]["method"] == "계절 반영":
        ws.append([])
        ws.append(["예상 월", "API 호출(회)", "스토리지(GB)", "기준 달"])
        for cell in ws[ws.max_row]:
            cell.font, cell.fill = head, fill
        for m in doc["forecast"]["monthly"]:
            ws.append([m["month"], m["api_calls"], m["storage_gb"], m["base_month"]])
    ws.append([])
    for line in doc["basis"]:
        ws.append([line])
    ws.column_dimensions["A"].width = 24
    for col in "BC":
        ws.column_dimensions[col].width = 16

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def to_docx(doc: dict, chart_pngs: dict[str, bytes] | None = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    d = Document()
    normal = d.styles["Normal"]
    normal.font.name = "Malgun Gothic"
    normal.font.size = Pt(10)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")
    for style in ("Title", "Heading 1", "Heading 2"):
        d.styles[style].element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "맑은 고딕")

    d.add_heading(doc["title"], level=0)

    def table(rows: list[list[str]], header: bool = True, numeric_from: int = 1):
        t = d.add_table(rows=0, cols=len(rows[0]))
        t.style = "Light Grid Accent 1"
        for i, values in enumerate(rows):
            cells = t.add_row().cells
            for j, value in enumerate(values):
                cells[j].text = value
                if j >= numeric_from and (not header or i > 0):
                    cells[j].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        return t

    table([[k, v] for k, v in _summary_rows(doc)], header=False, numeric_from=99)
    d.add_heading("12개월 비교 (원)", level=1)
    table([["구분", "공급가액", "세액", "합계"]] +
          [[r["label"], f"{r['supply']:,}", f"{r['tax']:,}", f"{r['total']:,}"] for r in doc["comparison"]])
    d.add_paragraph(f"절감률(공급가액 기준): {doc['saving_pct']}%")
    if doc["season"]:
        rows, period = season_rows(doc)
        d.add_heading("다가오는 성수기 (원)", level=1)
        d.add_paragraph(f"{period} · 신청 마감 {doc['season']['deadline']} · 예산 편성용 예상 금액")
        table(rows)
    for key in ("cumulative", "composition", "usage"):
        if chart_pngs and key in chart_pngs:
            d.add_picture(io.BytesIO(chart_pngs[key]), width=Cm(16))
            note = d.add_paragraph(" ".join(doc["captions"][key]))
            for run in note.runs:
                run.font.size = Pt(9)
    d.add_heading("월별 예상 금액 (원)", level=1)
    table([["월", "현재 합계", "추천 공급가액", "추천 세액", "추천 합계"]] +
          [[m["month"], f"{m['baseline']['total']:,}", f"{m['recommended']['supply']:,}",
            f"{m['recommended']['tax']:,}", f"{m['recommended']['total']:,}"] for m in doc["monthly"]])
    d.add_heading("산출 기준", level=1)
    for line in doc["basis"]:
        d.add_paragraph(line, style="List Bullet")
    d.add_heading("유의사항", level=1)
    for line in doc["notice"]:
        d.add_paragraph(line, style="List Bullet")
    buffer = io.BytesIO()
    d.save(buffer)
    return buffer.getvalue()


def to_pdf(doc: dict, chart_pngs: dict[str, bytes] | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = fonts.pdf_font()
    ink, muted, line = colors.HexColor("#0b0b0b"), colors.HexColor("#52514e"), colors.HexColor("#d9d8d4")
    title = ParagraphStyle("t", fontName=font, fontSize=18, leading=24, textColor=ink, spaceAfter=8)
    h1 = ParagraphStyle("h", fontName=font, fontSize=12, leading=16, textColor=ink, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("b", fontName=font, fontSize=9, leading=13, textColor=ink)
    small = ParagraphStyle("s", fontName=font, fontSize=8, leading=11, textColor=muted)
    caption = ParagraphStyle("c", fontName=font, fontSize=8.5, leading=12.5, textColor=ink, leftIndent=4 * mm,
                             rightIndent=4 * mm, spaceAfter=2 * mm)

    def grid(rows, widths, numeric_from=1, header=True):
        t = Table(rows, colWidths=widths)
        style = [("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                 ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("LINEBELOW", (0, 0), (-1, -1), 0.4, line),
                 ("ALIGN", (numeric_from, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeedea"))]
        t.setStyle(TableStyle(style))
        return t

    story = [Paragraph(doc["title"], title)]
    story.append(grid([[k, v] for k, v in _summary_rows(doc)], [45 * mm, 125 * mm], numeric_from=9, header=False))
    story += [Paragraph("12개월 비교 (원)", h1),
              grid([["구분", "공급가액", "세액", "합계"]] +
                   [[r["label"], f"{r['supply']:,}", f"{r['tax']:,}", f"{r['total']:,}"] for r in doc["comparison"]],
                   [82 * mm, 30 * mm, 28 * mm, 30 * mm]),
              Spacer(1, 3 * mm), Paragraph(f"절감률(공급가액 기준): {doc['saving_pct']}%", body)]
    if doc["season"]:
        rows, period = season_rows(doc)
        story += [Paragraph("다가오는 성수기 (원)", h1),
                  Paragraph(f"{period} · 신청 마감 {doc['season']['deadline']} · 예산 편성용 예상 금액", body),
                  Spacer(1, 2 * mm), grid(rows, [82 * mm, 30 * mm, 28 * mm, 30 * mm])]
    for key in ("cumulative", "composition", "usage"):
        if chart_pngs and key in chart_pngs:
            story += [Spacer(1, 3 * mm), Image(io.BytesIO(chart_pngs[key]), width=170 * mm, height=85 * mm),
                      Spacer(1, 1 * mm), Paragraph(" ".join(doc["captions"][key]), caption)]
    story += [Paragraph("월별 예상 금액 (원)", h1),
              grid([["월", "현재 합계", "추천 공급가액", "추천 세액", "추천 합계"]] +
                   [[m["month"], f"{m['baseline']['total']:,}", f"{m['recommended']['supply']:,}",
                     f"{m['recommended']['tax']:,}", f"{m['recommended']['total']:,}"] for m in doc["monthly"]],
                   [30 * mm, 35 * mm, 35 * mm, 35 * mm, 35 * mm])]
    story.append(Paragraph("산출 기준", h1))
    story += [Paragraph("· " + text, body) for text in doc["basis"]]
    story.append(Paragraph("유의사항", h1))
    story += [Paragraph("· " + text, small) for text in doc["notice"]]

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=doc["title"],
                            author="요금제 추천 시스템", subject=doc["document_no"])
    pdf.build(story)
    return buffer.getvalue()


# --- 발행 ---

FORMATS = ("pdf", "docx", "xlsx", "png")


def issue(conn: sqlite3.Connection, user_id: str, requester_user_id: str, kind: str = "review",
          change: dict | None = None, issued: date | None = None, formats: tuple[str, ...] = FORMATS,
          app_db: Path = history.APP_DB_PATH, audit_path: Path = audit.AUDIT_PATH) -> dict:
    """서류를 만들어 형식별 바이트를 돌려준다. 다른 고객의 서류는 만들지 않는다(DOC-07).

    파일로 저장하지 않는다. 발행 기록(문서 번호, 해시)만 app.db와 감사 기록에 남긴다(DOC-05, security.md 8장).
    """
    if requester_user_id != user_id:
        audit.record("document_denied", audit_path, kind=kind)
        raise PermissionError("본인 고객의 서류만 내려받을 수 있습니다.")
    data = build_data(conn, user_id, kind, change)
    doc = stamp(data, issued or date.today())
    pngs = charts.render_all(doc)
    files = {}
    if "pdf" in formats:
        files["pdf"] = to_pdf(doc, pngs)
    if "docx" in formats:
        files["docx"] = to_docx(doc, pngs)
    if "xlsx" in formats:
        files["xlsx"] = to_xlsx(doc)
    if "png" in formats:
        files.update({f"{key}.png": png for key, png in pngs.items()})
    with closing(history.connect(app_db)) as app:
        history.record_document(app, doc["document_no"], kind, doc["customer"]["code"], doc["issued_at"],
                                doc["valid_until"], doc["sha256"], sorted(files))
    audit.record("document_issued", audit_path, document_no=doc["document_no"], kind=kind,
                 code=doc["customer"]["code"], sha256=doc["sha256"], formats=sorted(files))
    return {"document": doc, "files": files}


def file_name(doc: dict, ext: str) -> str:
    return f"{doc['title'].replace(' ', '_')}_{doc['document_no']}.{ext}"
