"""추천 상세 화면과 서류에 넣는 그래프 3개(VIZ-01~03). 값은 서류 데이터(시뮬레이터 결과)만 쓴다.

색은 시각화 지침의 검증된 기본 팔레트 앞 세 칸(파랑·주황·청록)이다. 라이트 모드 검사 통과,
청록은 배경 대비가 3:1 미만이라 같은 값을 표로 함께 보여준다(VIZ-02, 서류의 표).
글자는 계열 색이 아니라 본문 잉크색으로 쓴다. 축은 하나, 두 측정값(API 호출, 스토리지)은 그림을 나눈다.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

import fonts  # noqa: E402

SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]  # 범주 1·2·3: 순서 고정, 순환하지 않음
DPI = 150


def _style():
    font = fonts.chart_font()
    plt.rcParams.update({
        "font.family": [font] if font else ["sans-serif"],
        "axes.unicode_minus": False,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "text.color": INK,
        "xtick.color": INK_2, "ytick.color": INK_2, "xtick.labelsize": 8, "ytick.labelsize": 8,
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
        "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8, "legend.frameon": False,
        "legend.fontsize": 8, "axes.axisbelow": True,  # 격자는 막대·선 아래에
    })


def _month_label(month: str) -> str:
    year, mm = month.split("-")
    return f"{year[2:]}.{mm}"


def _won_axis(ax):
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 10000:,.0f}만"))


def _title(fig, text: str, sub: str):
    fig.text(0.012, 0.96, text, fontsize=11, color=INK, va="top", ha="left", weight="bold")
    fig.text(0.012, 0.885, sub, fontsize=8, color=INK_2, va="top", ha="left")


def _png(fig) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=DPI)
    plt.close(fig)
    return buffer.getvalue()


def cumulative(doc: dict) -> bytes:
    """① 12개월 누적 비용: 현재 요금제 유지 vs 추천."""
    _style()
    months = [_month_label(m["month"]) for m in doc["monthly"]]
    base, rec, acc_b, acc_r = [], [], 0, 0
    for m in doc["monthly"]:
        acc_b += m["baseline"]["supply"]
        acc_r += m["recommended"]["supply"]
        base.append(acc_b)
        rec.append(acc_r)
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.subplots_adjust(left=0.09, right=0.84, top=0.80, bottom=0.12)
    names = [f"현재 유지({doc['current_plan']})", f"추천({doc['recommended_plan']})"]
    for values, color, name in ((base, SERIES[0], names[0]), (rec, SERIES[1], names[1])):
        ax.plot(months, values, color=color, linewidth=2, solid_capstyle="round", label=name)
        ax.plot(months[-1], values[-1], "o", color=color, markersize=7, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(f"{values[-1]:,}원", (len(months) - 1, values[-1]), xytext=(8, 0), textcoords="offset points",
                    va="center", fontsize=8, color=INK)
    _won_axis(ax)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left")
    _title(fig, "12개월 누적 비용", f"부가세 별도(공급가액) · 12개월 절감 {doc['comparison'][2]['supply']:,}원")
    return _png(fig)


def composition(doc: dict) -> bytes:
    """② 추천 요금제의 월별 청구액 구성: 기본 요금(할인 반영)·API 초과·스토리지 초과."""
    _style()
    months = [_month_label(m["month"]) for m in doc["monthly"]]
    parts = [("base_after_discount", "기본 요금(할인 반영)"), ("api_overage", "API 초과 요금"),
             ("storage_overage", "스토리지 초과 요금")]
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.80, bottom=0.12)
    bottom, drawn = [0] * len(months), []
    for (key, name), color in zip(parts, SERIES):
        values = [m["parts"][key] for m in doc["monthly"]]
        if not any(values):
            continue  # 없는 항목은 범례에도 넣지 않는다.
        ax.bar(months, values, bottom=bottom, width=0.36, color=color, edgecolor=SURFACE, linewidth=2, label=name)
        bottom = [b + v for b, v in zip(bottom, values)]
        drawn.append(name)
    _won_axis(ax)
    ax.set_ylim(0, max(bottom) * 1.18)
    if len(drawn) >= 2:  # 계열이 하나면 범례 대신 부제목에 이름을 적는다.
        ax.legend(loc="upper left", ncol=3)
    promo = doc["promotion"]
    sub = f"{doc['recommended_plan']} · 부가세 별도"
    if len(drawn) == 1:
        sub += f" · 막대: {drawn[0]}"
    if promo:
        sub += f" · {promo['name']}: 처음 {promo['months']}개월 할인"
    _title(fig, "추천 요금제 월별 청구액 구성", sub)
    return _png(fig)


def usage(doc: dict) -> bytes:
    """③ 사용량과 기본 제공량. API 호출과 스토리지는 단위가 달라 그림을 나눈다(축은 하나씩).

    (VIZ-05) 성수기 고객은 24개월을 보여주고 성수기 달을 주황 점으로 표시한다.
    """
    _style()
    months = [_month_label(u["month"]) for u in doc["usage"]]
    peaks = set(doc.get("peak_months") or [])
    peak_index = [i for i, u in enumerate(doc["usage"]) if int(u["month"][5:]) in peaks]
    inc = doc["included"]
    fig, axes = plt.subplots(1, 2, figsize=(8, 4))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.78, bottom=0.12, wspace=0.28)
    panels = (("api_calls", "API 호출(회)", "{:,.0f}"), ("storage_gb", "스토리지(GB)", "{:,.0f}"))
    marker = 6 if len(months) <= 12 else 4
    for ax, (key, label, fmt) in zip(axes, panels):
        values = [u[key] for u in doc["usage"]]
        ax.plot(range(len(months)), values, color=SERIES[0], linewidth=2, solid_capstyle="round", label="사용량")
        if len(months) <= 12:  # 24개월이면 점을 빼고 선만 그린다(점이 많으면 선이 끊겨 보임)
            ax.plot(range(len(months)), values, "o", color=SERIES[0], markersize=marker, markeredgecolor=SURFACE,
                    markeredgewidth=1.5)
        if peak_index:
            ax.plot(peak_index, [values[i] for i in peak_index], "o", color=SERIES[1], markersize=marker + 2,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, label="성수기 달", linestyle="none")
        refs = [(inc["current"][key], f"{inc['current']['plan']} 제공량")]
        if inc["recommended"]["plan"] != inc["current"]["plan"]:
            refs.append((inc["recommended"][key], f"{inc['recommended']['plan']} 제공량"))
        for value, name in refs:
            ax.axhline(value, color=INK_2, linewidth=1, linestyle=(0, (4, 3)))
            ax.annotate(f"{name} {fmt.format(value)}", (0, value), xytext=(2, 3), textcoords="offset points",
                        fontsize=7.5, color=INK_2, bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1})
        top = max(values + [r[0] for r in refs])
        ax.set_ylim(0, top * 1.2)
        step = 1 if len(months) <= 12 else 3  # 24개월이면 석 달마다 눈금
        ax.set_xticks(range(0, len(months), step))
        ax.set_xticklabels(months[::step], fontsize=7.5)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt.format(v)))
        ax.set_title(label, fontsize=9, color=INK, loc="left")
    if peak_index:
        axes[0].legend(loc="upper right", fontsize=7.5)
        sub = f"최근 {len(months)}개월 · 점선은 기본 제공량 · 주황 점은 성수기 달 · 예상은 계절 반영"
    else:
        sub = f"점선은 기본 제공량 · 예상 사용량은 {', '.join(doc['forecast']['months'])} 평균"
    _title(fig, "사용량과 기본 제공량" if peak_index else "최근 사용량과 기본 제공량", sub)
    return _png(fig)


def render_all(doc: dict) -> dict[str, bytes]:
    return {"cumulative": cumulative(doc), "composition": composition(doc), "usage": usage(doc)}
