"""지식 문서 검색(RAG). 문서는 knowledge/*.md, 검색은 BM25.

- 한국어는 형태소 분석기 없이 글자 2개씩 묶어(bigram) 색인한다(요구사항 5장 7번).
- 같은 검색어면 항상 같은 순서로 돌려준다(KNW-02). 점수가 같으면 조각 ID 순.
- 관련도가 기준보다 낮으면 빈 결과를 돌려준다(KNW-04).
- 이 모듈은 금액을 다루지 않는다. 문서에는 금액·할인율이 없다(KNW-03).

실행: python src/knowledge.py "프로 전환 할인 조건"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
MAX_TOP_K = 5

# BM25 기본값
K1 = 1.5
B = 0.75
# 빈 결과 기준(KNW-04). 검색어의 색인어 중 이 비율 이상이 조각에 있어야 하고, 점수도 이 값 이상이어야 한다.
# 값은 eval/retrieval_set.json의 빈 결과 질문과 tests/test_knowledge_search.py의 관련 없는 질문으로 정했다.
MIN_COVERAGE = 0.25
MIN_SCORE = 3.0

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
WORD = re.compile(r"[0-9A-Za-z]+|[가-힣]+")
# 어느 문서에나 나오는 말투 조각. 색인어에서 뺀다.
STOP_TERMS = {"어떻", "떻게", "해야", "하나", "나요", "까요", "니다", "습니", "합니", "있나", "는지"}


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    plan_ids: tuple[str, ...]
    promo_ids: tuple[str, ...]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str          # 문서ID#번호(1부터)
    document: Document
    section: str           # 소제목
    text: str              # 소제목 아래 본문
    terms: tuple[str, ...] = field(repr=False, default=())


def parse_document(path: Path) -> tuple[Document, str]:
    """머리말(---)과 본문을 나눈다. 머리말 형식은 'key: value', 목록은 쉼표로 구분."""
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER.match(raw)
    if not match:
        raise ValueError(f"{path.name}: 머리말(---)이 없습니다")
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()

    def ids(key: str) -> tuple[str, ...]:
        return tuple(v.strip() for v in meta.get(key, "").split(",") if v.strip())

    document = Document(meta["doc_id"], meta["title"], ids("plan_ids"), ids("promo_ids"))
    return document, raw[match.end():]


def tokenize(text: str) -> list[str]:
    """영문·숫자는 소문자 단어로, 한글은 글자 2개 묶음으로 나눈다. 한 글자 한글 단어는 그대로 둔다."""
    terms = []
    for word in WORD.findall(text.lower()):
        if re.fullmatch(r"[가-힣]+", word):
            grams = [word] if len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)]
            terms.extend(g for g in grams if g not in STOP_TERMS)
        else:
            terms.append(word)
    return terms


def split_chunks(document: Document, body: str) -> list[Chunk]:
    """'## ' 소제목마다 조각 하나. 문서 제목과 소제목도 색인에 넣어 검색에 반영한다."""
    parts = re.split(r"^## +(.+)$", body, flags=re.M)
    chunks = []
    for number, i in enumerate(range(1, len(parts), 2), start=1):
        section, text = parts[i].strip(), parts[i + 1].strip()
        terms = tokenize(f"{document.title} {section} {section} {text}")
        chunks.append(Chunk(f"{document.doc_id}#{number}", document, section, text, tuple(terms)))
    return chunks


class KnowledgeBase:
    def __init__(self, directory: Path = KNOWLEDGE_DIR):
        self.chunks: list[Chunk] = []
        for path in sorted(directory.glob("*.md")):
            document, body = parse_document(path)
            if document.doc_id != path.stem:
                raise ValueError(f"{path.name}: doc_id가 파일 이름과 다릅니다")
            self.chunks.extend(split_chunks(document, body))
        self._tf = [Counter(c.terms) for c in self.chunks]
        self._len = [len(c.terms) for c in self.chunks]
        self._avg_len = sum(self._len) / len(self._len)
        df = Counter(term for tf in self._tf for term in tf)
        n = len(self.chunks)
        self._idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def documents(self) -> dict[str, Document]:
        return {c.document.doc_id: c.document for c in self.chunks}

    def _score(self, index: int, query_terms: list[str]) -> float:
        tf, length = self._tf[index], self._len[index]
        score = 0.0
        for term in query_terms:
            if term in tf:
                freq = tf[term]
                score += self._idf[term] * freq * (K1 + 1) / (freq + K1 * (1 - B + B * length / self._avg_len))
        return score

    def search(self, query: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        if not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k는 1~{MAX_TOP_K}이어야 합니다")
        query_terms = sorted(set(tokenize(query)))
        if not query_terms:
            return []
        hits = []
        for index, chunk in enumerate(self.chunks):
            matched = sum(1 for t in query_terms if t in self._tf[index])
            if matched / len(query_terms) < MIN_COVERAGE:
                continue
            score = self._score(index, query_terms)
            if score >= MIN_SCORE:
                hits.append((chunk, round(score, 4)))
        hits.sort(key=lambda hit: (-hit[1], hit[0].chunk_id))
        return hits[:top_k]


def main() -> int:
    parser = argparse.ArgumentParser(description="지식 문서 검색")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()
    results = KnowledgeBase().search(args.query, args.top_k)
    print(json.dumps([{"chunk_id": c.chunk_id, "title": c.document.title, "section": c.section, "score": s}
                      for c, s in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
