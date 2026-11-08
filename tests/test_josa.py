"""별칭을 이름·가칭 코드로 바꿀 때 조사 맞추기(SEC-03, SEC-12, T6에서 찾은 결함)."""

import pytest

import josa


@pytest.mark.parametrize("word, sound", [
    ("(주)카파", josa.NONE), ("(주)베타소프트", josa.NONE),      # 받침 없음
    ("(주)에타시스템", josa.OTHER), ("(주)오미크론", josa.OTHER),  # 받침 있음
    ("(주)델타랩", josa.OTHER), ("(주)하이몰", josa.RIEUL),        # ㄹ 받침
    ("고객 UQM-9GU", josa.NONE),    # U "유"
    ("고객 PVY-HE9", josa.NONE),    # 9 "구"
    ("고객 ABC-DE1", josa.RIEUL),   # 1 "일"
    ("고객 ABC-DE3", josa.OTHER),   # 3 "삼"
    ("고객 ABC-DEM", josa.OTHER),   # M "엠"
    ("고객 ABC-DER", josa.RIEUL),   # R "알"
    ("", None), ("가나#", None),
])
def test_final_sound(word, sound):
    assert josa.final_sound(word) == sound


def test_the_reported_case_is_fixed():
    # T5b 실제 실행(20261107-134416-c20837)에서 나온 문장
    assert josa.replace_alias("C1은 아직 이용 기록이 없습니다.", "C1", "(주)카파") == "(주)카파는 아직 이용 기록이 없습니다."


@pytest.mark.parametrize("written, no_final, with_final, rieul", [
    ("C1은 ", "(주)카파는 ", "(주)오미크론은 ", "(주)하이몰은 "),
    ("C1는 ", "(주)카파는 ", "(주)오미크론은 ", "(주)하이몰은 "),
    ("C1이 ", "(주)카파가 ", "(주)오미크론이 ", "(주)하이몰이 "),
    ("C1가.", "(주)카파가.", "(주)오미크론이.", "(주)하이몰이."),
    ("C1을 ", "(주)카파를 ", "(주)오미크론을 ", "(주)하이몰을 "),
    ("C1과 ", "(주)카파와 ", "(주)오미크론과 ", "(주)하이몰과 "),
    ("C1으로 ", "(주)카파로 ", "(주)오미크론으로 ", "(주)하이몰로 "),
    ("C1이나 ", "(주)카파나 ", "(주)오미크론이나 ", "(주)하이몰이나 "),
])
def test_particle_pairs(written, no_final, with_final, rieul):
    assert josa.replace_alias(written, "C1", "(주)카파") == no_final
    assert josa.replace_alias(written, "C1", "(주)오미크론") == with_final
    assert josa.replace_alias(written, "C1", "(주)하이몰") == rieul


def test_only_the_first_particle_changes_and_others_stay():
    name = "(주)베타소프트"
    assert josa.replace_alias("C1과의 상담", "C1", name) == "(주)베타소프트와의 상담"
    assert josa.replace_alias("C1으로는", "C1", name) == "(주)베타소프트로는"
    # 받침과 상관없는 조사는 그대로
    for tail in ("의 ", "에게는 ", "에 ", "도 ", "께 ", "님", " 고객", ".", ""):
        assert josa.replace_alias(f"C1{tail}", "C1", name) == f"{name}{tail}"


def test_copula_is_left_alone():
    # "이며", "이고", "입니다"는 서술격 조사. 받침 없는 이름 뒤에서도 그대로 써도 된다.
    for text in ("C1이며", "C1이고", "C1입니다", "C1이었습니다"):
        assert josa.replace_alias(text, "C1", "(주)카파") == text.replace("C1", "(주)카파")


def test_codes_use_spoken_sound():
    assert josa.replace_alias("C1은 ", "C1", "고객 PVY-HE9") == "고객 PVY-HE9는 "
    assert josa.replace_alias("C1은 ", "C1", "고객 ABC-DE3") == "고객 ABC-DE3은 "
    assert josa.replace_alias("C1으로 ", "C1", "고객 ABC-DE1") == "고객 ABC-DE1로 "


def test_other_aliases_and_longer_tokens_are_untouched():
    text = "C1은 C12와 다르고 XC1은 별칭이 아님. C2는 그대로."
    assert josa.replace_alias(text, "C1", "(주)카파") == "(주)카파는 C12와 다르고 XC1은 별칭이 아님. C2는 그대로."


def test_every_alias_in_a_line_is_replaced():
    text = "C1은 이번 달 C1의 기록과 C1을 비교합니다."
    assert josa.replace_alias(text, "C1", "(주)카파") == "(주)카파는 이번 달 (주)카파의 기록과 (주)카파를 비교합니다."
