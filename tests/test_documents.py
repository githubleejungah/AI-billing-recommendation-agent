"""비용처리 서류와 그래프 검증(DOC-01~07, VIZ-01~03, SIM-06).

기대 금액은 손으로 계산한 값이다.
(T3d) 장기 이용 혜택을 반영해 다시 계산했다(계산식은 test_simulator.py).
- user_002(2단계, 2027-04부터 3단계): 베이직 유지 124,500원 × 4 + 123,500원 × 8 = 1,486,000원(세액 148,600원).
  프로 + 3개월 30% 할인: 56,000원 × 3 + 80,000원 × 9 = 888,000원. 절감 598,000원(40.2% → 40%).
- user_007(3단계, 프로 제공 57,500회): 엔터프라이즈 유지 200,000원 × 12 = 2,400,000원. 프로 84,500원 × 12 = 1,014,000원.
  절감 1,386,000원(57.75% → 58%). 0.5 올림(52.5% → 53%)은 test_simulator.py의 saving_percent 테스트에서 본다.
- 세액은 달마다 공급가액의 10%(1원 미만 버림), 12개월 값은 월별 값의 합이다.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "database"))

import audit  # noqa: E402
import charts  # noqa: E402
import documents  # noqa: E402
import fonts  # noqa: E402
import init_db  # noqa: E402
import simulator  # noqa: E402

ISSUED = date(2026, 11, 7)
CHANGE = {"request_id": "CR-20261107-0001", "effective_date": "2026-12-01", "cancel_deadline": "2026-11-30"}


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "billing_mock.db"
    init_db.build(db_path)
    connection = simulator.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def issued(conn, tmp_path_factory):
    """user_002 검토서를 모든 형식으로 한 번 발행한다(그래프 렌더링이 느려서 모듈에서 공유)."""
    base = tmp_path_factory.mktemp("app")
    app_db, audit_path = base / "app.db", base / "audit.jsonl"
    result = documents.issue(conn, "user_002", "user_002", issued=ISSUED, app_db=app_db, audit_path=audit_path)
    return {**result, "app_db": app_db, "audit_path": audit_path}


def digits(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --- 금액 계산 ---

@pytest.mark.parametrize("supply, tax, total", [
    (129000, 12900, 141900),
    (56000, 5600, 61600),
    (12345, 1234, 13579),  # 1,234.5원 → 1원 미만 버림
    (9, 0, 9),
    (0, 0, 0),
])
def test_vat_is_ten_percent_rounded_down(supply, tax, total):
    assert documents.vat(supply) == {"supply": supply, "tax": tax, "total": total}


def test_review_amounts_match_hand_calculation(conn):
    data = documents.build_data(conn, "user_002")
    baseline, recommended, saving = data["comparison"]
    assert (baseline["supply"], baseline["tax"], baseline["total"]) == (1486000, 148600, 1634600)
    assert (recommended["supply"], recommended["tax"], recommended["total"]) == (888000, 88800, 976800)
    assert (saving["supply"], saving["tax"], saving["total"]) == (598000, 59800, 657800)
    assert data["saving_pct"] == 40
    assert [m["recommended"]["supply"] for m in data["monthly"]] == [56000] * 3 + [80000] * 9
    assert [m["discount"] for m in data["monthly"]] == [24000] * 3 + [0] * 9
    assert data["after_promotion_monthly"] == {"supply": 80000, "tax": 8000, "total": 88000}


def test_review_without_promotion(conn):
    data = documents.build_data(conn, "user_007")
    assert data["promotion"] is None
    assert [row["supply"] for row in data["comparison"]] == [2400000, 1014000, 1386000]
    assert data["saving_pct"] == 58


def test_review_states_loyalty_tier(conn):
    # LOY-05: 서류에 적용한 단계와 늘어난 제공량을 적는다. 값은 시뮬레이터 값이다.
    data = documents.build_data(conn, "user_002")
    assert data["included"]["current"] == {"plan": "베이직", "api_calls": 11000, "storage_gb": 55}  # 2단계 10%
    assert data["included"]["recommended"] == {"plan": "프로", "api_calls": 55000, "storage_gb": 220}
    assert "장기 이용 혜택: 2단계(모든 요금제 기본 제공량 10% 추가), 2027-04부터 3단계(15% 추가) 반영" in data["basis"]
    top = documents.build_data(conn, "user_015")  # 44개월, 최고 단계
    assert "장기 이용 혜택: 3단계(모든 요금제 기본 제공량 15% 추가)" in top["basis"]
    new = documents.build_data(conn, "user_004")  # 3개월, 6개월이 되는 2027-02부터 1단계
    assert "장기 이용 혜택: 아직 없음, 2027-02부터 1단계(5% 추가) 반영" in new["basis"]


def test_twelve_month_totals_are_sums_of_months(conn):
    for user_id in ("user_002", "user_007", "user_013", "user_019"):
        data = documents.build_data(conn, user_id)
        baseline, recommended, _saving = data["comparison"]
        for key in ("supply", "tax", "total"):
            assert baseline[key] == sum(m["baseline"][key] for m in data["monthly"])
            assert recommended[key] == sum(m["recommended"][key] for m in data["monthly"])
        rec = simulator.simulate(conn, user_id)["recommendation"]
        assert baseline["supply"] == rec["current_total_12m"]
        assert recommended["supply"] == rec["recommended_total_12m"]


def test_schedule_mismatch_blocks_document(conn, monkeypatch):
    real = simulator.simulate

    def broken(connection, user_id, *args, **kwargs):
        result = real(connection, user_id, *args, **kwargs)
        result["recommendation"]["recommended_total_12m"] += 1000
        return result

    monkeypatch.setattr(simulator, "simulate", broken)
    with pytest.raises(documents.DocumentError):
        documents.build_data(conn, "user_002")


def test_confirmation_needs_change_and_has_no_expiry(conn):
    with pytest.raises(documents.DocumentError):
        documents.build_data(conn, "user_002", kind="confirmation")
    doc = documents.stamp(documents.build_data(conn, "user_002", "confirmation", CHANGE), ISSUED)
    assert doc["title"] == "요금제 변경 확인서"
    assert doc["document_no"].startswith("CF-20261107-")
    assert doc["valid_until"] is None


def test_unknown_kind_is_rejected(conn):
    with pytest.raises(documents.DocumentError):
        documents.build_data(conn, "user_002", kind="invoice")


# --- 문서 번호와 유효기간 ---

def test_document_number_and_validity(conn):
    data = documents.build_data(conn, "user_002")
    doc = documents.stamp(data, ISSUED)
    code = data["customer"]["code"].replace("-", "")
    assert re.fullmatch(rf"RV-20261107-{code}-[0-9A-F]{{6}}", doc["document_no"])
    assert doc["valid_until"] == "2026-12-07"  # 발행 후 30일
    late = documents.stamp(data, date(2026, 12, 15))
    assert late["valid_until"] == "2026-12-31"  # 이벤트 신청 마감일이 더 이르다


def test_same_input_same_hash(conn):
    first = documents.stamp(documents.build_data(conn, "user_002"), ISSUED)
    second = documents.stamp(documents.build_data(conn, "user_002"), date(2026, 11, 20))
    assert first["sha256"] == second["sha256"]
    assert first["document_no"][-6:] == second["document_no"][-6:]
    other = documents.stamp(documents.build_data(conn, "user_007"), ISSUED)
    assert other["sha256"] != first["sha256"]


# --- 형식별 값이 시뮬레이터와 같은지 ---

def test_xlsx_values_match(issued):
    from openpyxl import load_workbook

    doc = issued["document"]
    wb = load_workbook(io.BytesIO(issued["files"]["xlsx"]))
    assert wb.sheetnames == ["요약", "월별", "근거"]
    summary = [row for row in wb["요약"].iter_rows(values_only=True)]
    table = {row[0]: row[1:4] for row in summary if row and isinstance(row[1], int)}
    for row in doc["comparison"]:
        assert table[row["label"]] == (row["supply"], row["tax"], row["total"])
    monthly = list(wb["월별"].iter_rows(min_row=2, values_only=True))
    assert len(monthly) == 12
    for sheet_row, m in zip(monthly, doc["monthly"]):
        assert sheet_row[0] == m["month"]
        assert sheet_row[1:7] == (m["baseline"]["supply"], m["baseline"]["tax"], m["baseline"]["total"],
                                  m["recommended"]["supply"], m["recommended"]["tax"], m["recommended"]["total"])
    # 회계 장부에 붙여 넣을 수 있게 수식이 아닌 값이어야 한다.
    for row in wb["월별"].iter_rows(min_row=2):
        assert not any(isinstance(cell.value, str) and cell.value.startswith("=") for cell in row)


def test_docx_values_match(issued):
    from docx import Document

    doc = issued["document"]
    d = Document(io.BytesIO(issued["files"]["docx"]))
    cells = " ".join(cell.text for table in d.tables for row in table.rows for cell in row.cells)
    text = " ".join(p.text for p in d.paragraphs) + " " + cells
    assert doc["document_no"] in text
    for row in doc["comparison"]:
        for key in ("supply", "tax", "total"):
            assert f"{row[key]:,}" in cells
    assert "비용 증빙은 매월 발행되는 세금계산서" in text
    assert len(d.inline_shapes) == 3  # 그래프 3개


def test_pdf_values_match(issued):
    from pypdf import PdfReader

    doc = issued["document"]
    reader = PdfReader(io.BytesIO(issued["files"]["pdf"]))
    text = digits(" ".join(page.extract_text() for page in reader.pages))
    assert doc["document_no"] in text
    for row in doc["comparison"]:
        for key in ("supply", "tax", "total"):
            assert f"{row[key]:,}" in text
    for m in doc["monthly"]:
        assert f"{m['recommended']['total']:,}" in text


def test_chart_pngs_open(issued):
    from PIL import Image

    pngs = {key: value for key, value in issued["files"].items() if key.endswith(".png")}
    assert sorted(pngs) == ["composition.png", "cumulative.png", "usage.png"]
    for data in pngs.values():
        image = Image.open(io.BytesIO(data))
        assert image.format == "PNG"
        assert image.width >= 900


def test_chart_palette_order_is_fixed():
    assert charts.SERIES == ["#2a78d6", "#eb6834", "#1baf7a"]


# --- 기록·권한 ---

def test_issue_records_document_and_audit_without_name(issued):
    doc = issued["document"]
    with sqlite3.connect(issued["app_db"]) as app:
        row = app.execute("SELECT document_no, kind, code, sha256 FROM documents").fetchone()
    assert row == (doc["document_no"], "review", doc["customer"]["code"], doc["sha256"])
    entries = audit.read(issued["audit_path"])
    assert [e["event"] for e in entries] == ["document_issued"]
    raw = issued["audit_path"].read_text(encoding="utf-8")
    assert doc["customer"]["name"] not in raw
    assert "user_002" not in raw
    assert entries[0]["sha256"] == doc["sha256"]


def test_issue_does_not_write_files(conn, tmp_path):
    before = set(ROOT.rglob("*.pdf")) | set(ROOT.rglob("*.xlsx")) | set(ROOT.rglob("*.docx"))
    documents.issue(conn, "user_007", "user_007", issued=ISSUED, formats=("xlsx",),
                    app_db=tmp_path / "app.db", audit_path=tmp_path / "audit.jsonl")
    after = set(ROOT.rglob("*.pdf")) | set(ROOT.rglob("*.xlsx")) | set(ROOT.rglob("*.docx"))
    assert before == after


def test_other_customer_is_denied(conn, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    with pytest.raises(PermissionError):
        documents.issue(conn, "user_002", "user_003", issued=ISSUED,
                        app_db=tmp_path / "app.db", audit_path=audit_path)
    entries = audit.read(audit_path)
    assert [e["event"] for e in entries] == ["document_denied"]
    assert not (tmp_path / "app.db").exists() or not sqlite3.connect(tmp_path / "app.db").execute(
        "SELECT COUNT(*) FROM documents").fetchone()[0]


def test_audit_rejects_forbidden_keys(tmp_path):
    with pytest.raises(ValueError):
        audit.record("document_issued", tmp_path / "a.jsonl", name="홍길동")
    assert not (tmp_path / "a.jsonl").exists()


def test_file_name_has_no_customer_name(issued):
    doc = issued["document"]
    name = documents.file_name(doc, "pdf")
    assert name.endswith(".pdf")
    assert doc["customer"]["name"] not in name
    assert doc["document_no"] in name


def test_korean_pdf_font_is_registered():
    from reportlab.pdfbase import pdfmetrics

    name = fonts.pdf_font()
    assert name in pdfmetrics.getRegisteredFontNames()


# --- 그래프 해설(VIZ-04) ---

def test_captions_for_user_002_match_hand_calculation(conn):
    caps = documents.build_data(conn, "user_002")["captions"]
    assert caps["cumulative"] == [
        "지금 요금제(베이직)를 유지하면 12개월 동안 1,486,000원, 추천 요금제(프로)로 바꾸면 888,000원이 듭니다(부가세 별도).",
        "두 선의 마지막 간격 598,000원(40%)이 12개월 절감액입니다.",
        "처음 3개월은 이벤트 할인이 더해져 간격이 더 빠르게 벌어집니다.",
    ]
    assert caps["composition"][0] == \
        "추천 요금제(프로)의 월 청구액(부가세 별도)은 2026-12~2027-02 56,000원, 2027-03~2027-11 80,000원입니다."
    assert caps["composition"][1] == "2026-12~2027-02에는 기본 요금 80,000원에서 24,000원이 할인됩니다."
    # 예상 42,000회·120GB, 베이직 제공량(2단계) 11,000회·55GB → 31,000회·65GB 초과
    assert "API 호출은 31,000회 초과, 스토리지는 65GB 초과입니다." in caps["usage"][1]
    # LOY-05: 해설에 적용 단계와 비교 기간 중 다음 단계
    assert caps["usage"][3:5] == [
        "기본 제공량에는 장기 이용 혜택 2단계(10% 추가)가 반영되어 있습니다.",
        "2027-04부터는 3단계(15% 추가)가 적용되어 제공량이 더 늘어납니다.",
    ]
    assert caps["usage"][-1] == "사용량이 20% 늘거나 줄어도 추천은 바뀌지 않습니다."


def test_captions_overage_and_keep(conn):
    over = documents.build_data(conn, "user_007")["captions"]
    # 예상 60,000회 - 프로 제공량(3단계) 57,500회 = 2,500회 초과 → ⌈2.5⌉ × 1,500원 = 4,500원
    assert "API 호출은 2,500회 초과" in over["usage"][2]
    assert over["composition"][-1] == "예상 사용량 기준으로 API 초과 요금 월 4,500원이 포함됩니다."
    keep = documents.build_data(conn, "user_001")["captions"]
    assert "바꿀 필요가 없습니다" in keep["cumulative"][0]
    assert keep["composition"][0].startswith("지금 요금제(베이직)를 유지할 때")


def test_captions_explain_stability_and_short_history(conn):
    down = documents.build_data(conn, "user_005")["captions"]["usage"]
    assert down[-1] == "사용량이 20% 줄면 베이직 요금제가 더 유리해질 수 있습니다."
    up = documents.build_data(conn, "user_019")["captions"]["usage"]
    assert up[-1] == "사용량이 20% 늘면 프로 요금제가 더 유리해질 수 있습니다."
    short = documents.build_data(conn, "user_013")["captions"]["usage"]
    assert "이용 기록이 1개월뿐이라" in short[1]


def test_caption_numbers_come_from_document(conn):
    """해설 속 금액은 모두 서류의 표에 있는 값이거나 그 차이다(새 숫자를 만들지 않음)."""
    for user_id in ("user_002", "user_007", "user_019"):
        data = documents.build_data(conn, user_id)
        known = {r[k] for r in data["comparison"] for k in ("supply", "tax", "total")}
        for m in data["monthly"]:
            known |= {m["recommended"]["supply"], m["discount"], m["parts"]["base_after_discount"] + m["discount"],
                      *m["parts"].values()}
        for line in data["captions"]["cumulative"] + data["captions"]["composition"]:
            for amount in re.findall(r"([\d,]+)원", line):
                assert int(amount.replace(",", "")) in known, (user_id, line)


def test_captions_in_pdf_and_docx(issued):
    from docx import Document
    from pypdf import PdfReader

    doc = issued["document"]
    pdf = digits(" ".join(p.extract_text() for p in PdfReader(io.BytesIO(issued["files"]["pdf"])).pages))
    docx = " ".join(p.text for p in Document(io.BytesIO(issued["files"]["docx"])).paragraphs)
    for lines in doc["captions"].values():
        for line in lines:
            assert line in docx
        # PDF는 줄바꿈 위치에 따라 띄어쓰기가 달라질 수 있어 금액만 확인한다.
        for amount in re.findall(r"[\d,]{4,}원", " ".join(lines)):
            assert amount in pdf


def test_captions_module_uses_no_ai():
    source = (ROOT / "src" / "captions.py").read_text(encoding="utf-8")
    assert "anthropic" not in source and "import agent" not in source
