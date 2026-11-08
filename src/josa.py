"""별칭(C1)을 고객 이름·가칭 코드로 바꿀 때 뒤의 조사를 받침에 맞춘다.

Claude는 별칭 "C1"을 "씨원"으로 읽어 받침이 있는 조사("C1은", "C1이")를 쓴다.
이름이 받침 없이 끝나면("(주)카파") 그대로 바꿀 때 "(주)카파은"이 된다. 그래서 바꿀 때
바꿀 말의 마지막 글자 소리로 조사 짝(은/는, 이/가, 을/를, 과/와, 으로/로 …)을 다시 고른다.

- 한글은 글자의 받침을 본다. 숫자·영문(가칭 코드 "UQM-9GU")은 읽는 소리로 본다(9 "구", L "엘").
- 별칭 바로 뒤의 첫 조사만 고친다("C1과의" → "카파와의"). 다만 "이"는 뒤에 한글이 이어지면("C1이며",
  "C1이고") 서술격 조사로 보고 그대로 둔다(받침 없는 이름 뒤에서도 자연스럽다).
- 소리를 알 수 없는 끝 글자면 Claude가 쓴 조사를 그대로 둔다.
"""

from __future__ import annotations

import re

NONE, RIEUL, OTHER = "없음", "ㄹ", "있음"  # 받침 없음 / ㄹ 받침 / 그 밖의 받침

# 숫자와 영문 대문자를 읽을 때의 받침(0 영, 1 일, 3 삼, 6 육, 7 칠, 8 팔, L 엘, M 엠, N 엔, R 알)
_SPOKEN = {**{d: NONE for d in "0123456789"}, **{c: NONE for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}}
_SPOKEN.update({"0": OTHER, "1": RIEUL, "3": OTHER, "6": OTHER, "7": RIEUL, "8": RIEUL,
                "L": RIEUL, "M": OTHER, "N": OTHER, "R": RIEUL})

# (받침 있을 때, 받침 없을 때). 긴 것부터 맞춘다.
_PAIRS = [("이랑", "랑"), ("이나", "나"), ("으로", "로"), ("은", "는"), ("이", "가"),
          ("을", "를"), ("과", "와"), ("아", "야")]
_FORMS = {form: pair for pair in _PAIRS for form in pair}
# "이"·"아"는 뒤에 한글이 이어지면 서술격 조사나 다른 말("C1이며", "C1아닌")일 수 있어 고치지 않는다.
# 나머지는 뒤에 조사가 더 붙어도("C1과의", "C1으로는") 앞 조사만 고친다.
_AMBIGUOUS = {"이", "아"}
_PARTICLE = "|".join(sorted(set(_FORMS) - _AMBIGUOUS, key=len, reverse=True)) \
    + "|" + "|".join(f"{form}(?![가-힣])" for form in sorted(_AMBIGUOUS))


def final_sound(word: str) -> str | None:
    """말의 마지막 소리가 받침 없음·ㄹ 받침·그 밖의 받침 중 무엇인지. 알 수 없으면 None."""
    word = word.rstrip()
    if not word:
        return None
    last = word[-1]
    if "가" <= last <= "힣":
        jong = (ord(last) - 0xAC00) % 28
        return NONE if jong == 0 else RIEUL if jong == 8 else OTHER
    return _SPOKEN.get(last.upper())


def choose(word: str, particle: str) -> str:
    """word 뒤에 붙일 조사. particle은 짝 중 아무 쪽이나 받는다."""
    pair = _FORMS.get(particle)
    sound = final_sound(word)
    if pair is None or sound is None:
        return particle
    with_final, without_final = pair
    if pair == ("으로", "로"):
        return "로" if sound in (NONE, RIEUL) else "으로"
    return without_final if sound == NONE else with_final


def replace_alias(text: str, alias: str, replacement: str) -> str:
    """text 속 alias를 replacement로 바꾸고, 바로 뒤의 조사를 replacement의 받침에 맞춘다."""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?!\d)({_PARTICLE})?")

    def swap(match: re.Match) -> str:
        particle = match.group(1)
        return replacement + (choose(replacement, particle) if particle else "")

    return pattern.sub(swap, text)
