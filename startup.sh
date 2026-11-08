#!/bin/sh
# App Service(리눅스) 시작 명령(DEP-01). 가상 DB를 만들고, 두 화면을 한 주소의 두 페이지로 연다.
# 이 PC에서는 쓰지 않는다(로컬 화면은 127.0.0.1 전용, UI-01). 배포한 화면은 로그인이 앞에서 막는다(DEP-04).
set -e

# 서류·그래프의 한글 글꼴(VIZ-03). 실패해도 화면은 뜨지만 그래프 글자가 깨질 수 있다.
if [ ! -f /usr/share/fonts/truetype/nanum/NanumGothic.ttf ]; then
  (apt-get update -qq && apt-get install -y -qq fonts-nanum) || echo "한글 글꼴 설치 실패"
fi

mkdir -p "$(dirname "${BILLING_APP_DB:-/home/data/app.db}")"
python database/init_db.py  # 가상 데이터만(시드 고정, DEP-04)
exec python -m streamlit run src/app_main.py \
  --server.address 0.0.0.0 --server.port "${PORT:-8000}" --server.headless true
