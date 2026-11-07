"""서류(PDF)와 그래프(PNG)에 쓸 한글 글꼴 찾기(VIZ-03).

찾는 순서: 환경 변수 KOREAN_FONT_PATH → Windows 맑은 고딕 → macOS 애플고딕 → Linux 글꼴.
- PDF(reportlab)는 TrueType 글꼴(.ttf, .ttc)만 넣을 수 있다. 하나도 없으면 PDF 표준 한글 글꼴(HYGothic)을 쓴다.
  이 글꼴은 파일에 들어가지 않아 보는 프로그램의 글꼴에 기대게 되므로 마지막 수단이다.
- 그래프(matplotlib)는 OpenType 글꼴도 쓸 수 있어 Noto Sans CJK도 후보에 넣는다.
회사 밖으로 배포하는 서류라면 오픈 라이선스 글꼴(Noto Sans KR 등)을 KOREAN_FONT_PATH로 지정하는 것을 권한다.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

TRUETYPE_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
OPENTYPE_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
]
PDF_FONT_NAME = "KoreanDoc"
PDF_FALLBACK_CID = "HYGothic-Medium"


def _configured() -> list[str]:
    path = os.environ.get("KOREAN_FONT_PATH")
    return [path] if path else []


def truetype_path() -> Path | None:
    for candidate in _configured() + TRUETYPE_CANDIDATES:
        if candidate.lower().endswith((".ttf", ".ttc")) and Path(candidate).exists():
            return Path(candidate)
    return None


@lru_cache(maxsize=1)
def pdf_font() -> str:
    """reportlab에 한글 글꼴을 등록하고 이름을 돌려준다."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    path = truetype_path()
    if path:
        pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, str(path), subfontIndex=0))
        return PDF_FONT_NAME
    pdfmetrics.registerFont(UnicodeCIDFont(PDF_FALLBACK_CID))
    return PDF_FALLBACK_CID


@lru_cache(maxsize=1)
def chart_font() -> str | None:
    """matplotlib에 한글 글꼴을 등록하고 글꼴 이름을 돌려준다. 없으면 None."""
    from matplotlib import font_manager

    # 굵은 글씨가 있는 글꼴을 먼저 쓴다. WenQuanYi는 굵기가 하나뿐이라 마지막에 둔다.
    ordered = [c for c in TRUETYPE_CANDIDATES if "wqy" not in c] + OPENTYPE_CANDIDATES + \
        [c for c in TRUETYPE_CANDIDATES if "wqy" in c]
    for candidate in _configured() + ordered:
        if Path(candidate).exists():
            font_manager.fontManager.addfont(candidate)
            return font_manager.FontProperties(fname=candidate).get_name()
    return None
