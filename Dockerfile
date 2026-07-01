# 공식 Playwright Python base 이미지 사용 (Chromium 및 시스템 라이브러리 자동 탑재)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 작업 디렉토리 설정
WORKDIR /app

# 시스템 언어 설정 (한글 파일명 및 인코딩 지원 강화)
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# 의존성 패키지 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스코드 복사
COPY . .

# 한글 파일명으로 인한 런타임/빌드 인코딩 충돌을 원천 차단하기 위해 app.py로 이름 변경 복사
RUN cp 네이버플레이스_순위검색.py app.py

# Coolify 영구 볼륨을 매핑할 데이터 디렉토리 생성
RUN mkdir -p /app/data

# Flask 포트 노출
EXPOSE 5000

# 기본 환경 변수 설정
ENV PORT=5000
ENV PLAYWRIGHT_HEADLESS=True
ENV DATA_DIR=/app/data
ENV FLASK_DEBUG=False

# 안정적인 동시성 처리를 위해 gunicorn 웹서버 구동 (스케줄러 중복 실행 방지를 위해 워커 수 1로 제한)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "app:app"]
