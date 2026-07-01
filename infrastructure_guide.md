# Infrastructure & Deployment Guide

이 문서는 'Viral Marketing Gonggam'의 표준 인프라 환경 및 호스팅 아키텍처 가이드라인입니다. 
새로운 프로젝트의 웹/앱 애플리케이션을 설계, 빌드, 배포할 때는 반드시 아래의 호스팅 스택과 네트워크 흐름을 준수하여 최적의 빌드 플랜을 도출해야 합니다.

---

## 1. Core Hosting Stack Overview

우리 서비스는 고성능 가상 서버(VPS) 위에 오픈소스 PaaS 도구인 Coolify를 얹어, AWS 인프라 부럽지 않은 유연하고 독립적인 배포 환경을 운영하고 있습니다.

| 레이어 | 도입 서비스 / 플랫폼 | 핵심 역할 및 기능 |
| :--- | :--- | :--- |
| **Domain** | 호스팅KR (Hosting KR) | 도메인 소유권 관리 및 최상위 도메인(루트 도메인) 보유 |
| **DNS / Proxy** | 클라우드플레어 (Cloudflare) | 무료 DNS 관리, Edge SSL(보안), DDoS 방어, 프록시(주황색 구름) |
| **Compute (Server)** | 벌쳐 (Vultr VPS) | Ubuntu 기반 가상 프라이빗 서버 (물리적 연산 및 스토리지) |
| **PaaS (Orchestration)** | Coolify (Self-Hosted) | Docker 기반 컨테이너 자동 배포 관리 및 내부 Traefik 역방향 프록시 |

---

## 2. Network Traffic Architecture (흐름도)

사용자가 서비스 주소(Domain)를 브라우저에 입력하여 실제 서버 내부의 컨테이너 앱에 도달하기까지의 파이프라인입니다.

[User Browser]
│
▼ (HTTPS 보호 상태로 진입)
[Cloudflare DNS & Proxy (주황색 구름)]
│
▼ (SSL/TLS Mode: Full Strict / 안전한 암호화 토스)
[Vultr VPS Server IP (Ubuntu)]
│
▼ (Port 80/443 진입)
[Coolify / Traefik Reverse Proxy]
│
▼ (내부 라우팅 테이블 매칭: http:// 규격으로 무한 리디렉션 방지)
[Target App Container (Node.js Port 3000 등)]

## 3. 핵심 세팅 가이드라인 및 주의사항 (AI 권장 숙지 사항)

### ⚠️ 한글 도메인 및 퓨니코드(Punycode) 매칭 규칙
* 대한민국 로컬 타겟팅 특성상 한글 도메인을 자주 사용합니다.
* **Cloudflare DNS 레코드:** 네임서버가 호스팅KR에서 클라우드플레어로 이관되어 있습니다. 서브도메인 등록 시 Name 칸에 한글(예: `전자계약`)을 입력하면 클라우드플레어가 내부적으로 자동 인코딩합니다.
* **Coolify Domains 설정:** Coolify 앱 설정의 Domains 칸에는 한글을 그대로 넣으면 매칭 에러(`no available server`)가 납니다. **반드시 접두사를 포함한 전체 퓨니코드 주소** 형태로 입력해야 합니다.
  * *올바른 예시:* `http://xn--989aw28bqobin.xn--439azq000auzav0zikav51fhlh.com`

### 🔒 [중요] 클라우드플레어 연동 시 Coolify 프로토콜 규칙 (HTTP 필수)
* 클라우드플레어의 암호화 모드는 **전체 엄격(Full Strict)** 모드를 기본으로 채택하여 앞단에서 HTTPS를 강제합니다.
* **🚨 무한 리디렉션 예방 필수 규칙:** 클라우드플레어의 **주황색 구름(프록시됨)**을 사용하는 서브도메인을 Coolify에 등록할 때, Coolify Domains 칸의 프로토콜은 **반드시 `https://`가 아닌 `http://`로 시작**해야 합니다.
  * `https://`로 등록 시 클라우드플레어와 Coolify Traefik 엔진이 서로 HTTPS 전환 명령을 핑퐁으로 주고받으며 무한 루프(`ERR_TOO_MANY_REDIRECTS`)가 발생합니다.
  * `http://`로 등록하더라도 외부 사용자는 클라우드플레어의 프록시망 덕분에 완벽하게 안전한 `https://` 안전 프로토콜로 접속되므로 안심해도 됩니다.

---

# Coolify - 신규 프로젝트(Private Repo) 호스팅 배포 매뉴얼

### 1단계: Coolify 상세 페이지 탈출 및 새 리소스 생성 진입
1. **Coolify 콘솔 대시보드**에 접속합니다.
2. 화면 최상단 좌측의 빵부스러기 경로 중에서 **`production`** 글자를 클릭합니다.
3. 우측 상단에 나타나는 **`+ New`** 또는 **`+ New Resource`** 버튼을 클릭합니다.

### 2단계: 깃허브 프라이빗 앱(GitHub App) 연동 (최초 1회만 수행)
1. New Resource 메인 화면에서 **`Private Repository (with GitHub App)`** 버튼을 클릭합니다.
2. `Name` 칸에 관리용 이름을 적고 아래 **`Continue`** 버튼을 클릭합니다.
3. 왼쪽 **Automated Installation** 박스 맨 아래에 있는 보라색 **`Register Now`** 버튼을 클릭합니다.
4. 열린 깃허브 창에서 **`All repositories`**를 선택하거나 배포할 저장소를 체크한 후 초록색 **`Install & Authorize`** 버튼을 클릭하여 승인합니다.

### 3단계: 비공개 리포지토리(Repository) 선택 및 기본 검증
1. Coolify 좌측 메뉴에서 **`Projects`** ➔ **`production`** 환경을 순서대로 다시 클릭하여 진입합니다.
2. 우측 상단의 **`+ New`** ➔ **`Private Repository (with GitHub App)`** 버튼을 클릭합니다.
3. 목록에서 배포할 저장소(예: `allin1`)를 선택하고 **`Load Repository`**를 누릅니다.
4. **Configuration** 기본 설정값들을 확인합니다 (`Branch: main`, `Build Pack: Nixpacks`, `Port: 3000`). 확인 후 맨 아래 **`Continue`** 버튼을 클릭합니다.

### 4단계: 도메인 매칭 및 최종 배포(Deploy) 개시
1. 생성된 애플리케이션의 **General 설정 화면**으로 진입하여 화면 중간의 **`Domains`** 입력 칸을 찾습니다.
2. **[🚨 절대 주의]** 무한 리디렉션 방지를 위해, 도메인 주소 맨 앞에 반드시 **`http://`** 프로토콜을 명시하고 이어서 한글 퓨니코드 주소를 입력합니다.
   * *올바른 입력 예시:* **`http://xn--3k5bulhq.xn--439azq000auzav0zikav51fhlh.com`**
3. 도메인 입력 후 바로 위에 있는 **`Save`** 버튼을 눌러 설정을 저장합니다.
4. 우측 상단에 활성화된 보라색 **`Deploy`** (또는 **`Redeploy`**) 버튼을 클릭하여 빌드를 시작합니다.
5. 상단의 상태 표시가 **`Running`**으로 바뀌면 브라우저 주소창에 설정한 한글 도메인(`https://...`)을 입력하고 접속하여 최종 수동 검증합니다.

### 🔒 SSL / TLS 및 초기 배포 프로토콜
* 클라우드플레어의 암호화 모드는 **전체 엄격(Full Strict)** 모드를 기본으로 채택합니다.
* **초기 배포 트러블슈팅:** 최초 배포 시 SSL Let's Encrypt 인증서 발급 병목이 생길 경우, 클라우드플레어의 **'주황색 구름(프록시됨)'을 잠시 끄고 '회색 구름(DNS Only)' 상태**로 길을 뚫어 인증서를 발급받은 뒤 다시 프록시를 켜는 프로토콜을 사용합니다.

### 🐳 Coolify 기반의 컨테이너 배포 규격
* 기본적으로 Node.js(Next.js, Express 등) 앱은 내부적으로 `0.0.0.0:3000` 포트로 바인딩되어 도커 컨테이너로 실행됩니다.
* AI는 새로운 앱을 설계할 때, 복잡한 인프라 설정 코드를 작성할 필요 없이 **Coolify가 쉽게 빌드 팩(Buildpack)이나 Dockerfile을 인식할 수 있도록 표준적인 프로젝트 구조(예: package.json, start 스크립트 명시)**를 짜는 데 집중해야 합니다.

---

## 🤖 AI 에이전트를 위한 지침 (Instruction)
1. 새로운 기능이나 프로젝트 설계를 요청받으면, 위 인프라 환경에서 단일 Vultr 서버의 리소스를 효율적으로 쓸 수 있는 **경량화된 Docker 기반 아키텍처**를 제안하세요.
2. 배포 가이드를 작성할 때는 `Docker Compose`를 직접 다루는 방식 대신, **Coolify Web UI에서 환경 변수(Environment Variables)와 도메인, 포트를 어떻게 입력하면 되는지**의 관점으로 설명서를 도출하세요.
3. 한국 로컬 마케팅 플랫폼(네이버 플레이스, 카롯마켓 등)과의 데이터 연동이나 서브도메인 라우팅이 필요한 경우, 퓨니코드 규칙이 누락되지 않았는지 사전에 체크하세요.