import time
import hmac
import hashlib
import base64
import requests
import json
import csv
import os
from collections import defaultdict
from typing import List, Dict
try:
    import pandas as pd  # 랭킹 표 출력용 (없으면 스킵)
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("pandas 없음. 기본 출력만 사용.")

# === 설정값 ===
API_KEY = os.environ.get('NAVER_SEARCHAD_API_KEY') or '0100000000522a213ece6cf38894977cc05cf6f964fd9faf8b9e2cdcf393d9ecb1290d68c9'  # 네이버 검색광고 API 키
SECRET_KEY = os.environ.get('NAVER_SEARCHAD_SECRET_KEY') or 'AQAAAADa+ZhviKqPxwWRvNl4Fuo6uhk2V3/o07I8QYQC5SOv4Q=='
CUSTOMER_ID = os.environ.get('NAVER_SEARCHAD_CUSTOMER_ID') or '3315879'  # 광고고객 ID
BASE_URL = 'https://api.searchad.naver.com'
URI = '/keywordstool'

def make_headers() -> Dict[str, str]:
    """네이버 검색광고 API 시그니처 헤더 생성"""
    timestamp = str(round(time.time() * 1000))
    message = f"{timestamp}.GET.{URI}"
    sign = hmac.new(
        SECRET_KEY.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    signature = base64.b64encode(sign).decode('utf-8')
    
    return {
        'Content-Type': 'application/json; charset=UTF-8',
        'X-Timestamp': timestamp,
        'X-API-KEY': API_KEY,
        'X-Customer': str(CUSTOMER_ID),
        'X-Signature': signature
    }

def fetch_keywords(keyword: str) -> List[Dict]:
    """특정 키워드로 연관 키워드 + 월간 검색량 조회"""
    url = BASE_URL + URI
    params = {
        'hintKeywords': keyword.replace(' ', ''),  # 공백 제거
        'showDetail': 1  # 상세 검색량 포함
    }
    
    try:
        res = requests.get(url, headers=make_headers(), params=params)
        res.raise_for_status()
        data = res.json()['keywordList']
        
        results = []
        for k in data:
            # < 10 값 처리 (0으로 보정)
            pc = 0 if k['monthlyPcQcCnt'] == '< 10' else int(k['monthlyPcQcCnt'])
            mo = 0 if k['monthlyMobileQcCnt'] == '< 10' else int(k['monthlyMobileQcCnt'])
            total = pc + mo
            
            results.append({
                'keyword': k['relKeyword'],
                'monthly_search': total,
                'monthly_pc': pc,
                'monthly_mobile': mo
            })
        return results
    except Exception as e:
        print(f"❌ API 호출 오류 ({keyword}): {e}")
        return []

def get_mobile_monthly(keyword: str) -> int:
    """
    단일 키워드의 모바일 월간 조회수만 반환.
    - 응답에 정확히 매칭되는 키워드가 있으면 그 값을 사용
    - 없으면 0 반환
    """
    kw_clean = keyword.replace(" ", "")
    items = fetch_keywords(keyword)
    for it in items:
        rel = it.get('keyword', '').replace(" ", "")
        if rel.lower() == kw_clean.lower():
            return it.get('monthly_mobile', 0) or 0
    return 0

def rank_keywords(seed_keywords: List[str]) -> List[Dict]:
    """여러 기준 키워드 입력 → 전체 연관키워드 랭킹"""
    all_items = []
    print("🔍 키워드 검색량 조회 중...")
    
    for kw in seed_keywords:
        print(f"  '{kw}' 연관키워드 조회...")
        items = fetch_keywords(kw)
        all_items.extend(items)
        time.sleep(0.1)  # API 제한 방지
    
    if not all_items:
        return []
    
    # 키워드 중복 합산
    agg = defaultdict(int)
    for it in all_items:
        agg[it['keyword']] += it['monthly_search']
    
    # 월간 검색량 기준 랭킹
    ranked = sorted(
        [{'keyword': k, 'monthly_search': v} for k, v in agg.items()],
        key=lambda x: x['monthly_search'],
        reverse=True
    )
    
    # 순위 부여
    for i, row in enumerate(ranked, start=1):
        row['rank'] = i
    
    return ranked

def format_number(num: int) -> str:
    """숫자에 쉼표 추가 (Python 버전 호환)"""
    return f"{num:,}" if hasattr(int, '__format__') else str(num)

def save_to_csv(ranked: List[Dict], filename: str = 'keyword_ranking.csv'):
    """CSV로 저장"""
    if ranked:
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['rank', 'keyword', 'monthly_search'])
            writer.writeheader()
            writer.writerows(ranked)
        print(f"\n✅ 랭킹 저장 완료: {filename}")

def print_ranking(ranked: List[Dict]):
    """콘솔 표 출력 (오류 수정)"""
    print("\n" + "="*80)
    print("📊 월간 검색량 TOP 키워드 랭킹")
    print("="*80)
    print(f"{'순위':<5} {'키워드':<25} {'월간 검색량':>15}")
    print("-"*80)
    
    for r in ranked[:20]:  # 상위 20개만
        search_str = format_number(r['monthly_search'])
        print(f"{r['rank']:<5} {r['keyword']:<25} {search_str:>15}")
    
    if len(ranked) > 20:
        print(f"\n... (총 {len(ranked)}개 키워드 중 상위 20개만 표시)")

def main():
    """메인 실행 함수"""
    print("🚀 네이버 검색광고 API 월간 검색량 랭킹 프로그램")
    print("="*50)
    
    # 입력 받기 (예시: 음식점 마케팅 키워드)
    user_input = input("기준 키워드 입력 (스페이스로 구분, 예: 부산맛집 해운대맛집): ").strip()
    seed_keywords = user_input.split() if user_input else ['부산맛집']  # 기본값
    
    # 단일 키워드 입력 시: 해당 키워드의 모바일 월간 조회수만 표시
    if len(seed_keywords) == 1:
        kw = seed_keywords[0]
        mo = get_mobile_monthly(kw)
        print(f"\n📱 '{kw}' 모바일 월간 조회수: {mo:,}")
        return
    
    # 여러 키워드 입력 시: 랭킹 모드
    print(f"🎯 분석할 기준 키워드: {seed_keywords}")
    
    ranked = rank_keywords(seed_keywords)
    
    if ranked:
        print_ranking(ranked)
        save_to_csv(ranked)
        
        # Pandas로 예쁜 표 (선택)
        if HAS_PANDAS:
            df = pd.DataFrame(ranked)
            print("\n📈 전체 데이터 요약 (상위 10):")
            print(df.head(10).to_string(index=False))
        
        print(f"\n💡 총 {len(ranked)}개 키워드 분석 완료!")
    else:
        print("❌ 데이터 조회 실패. API 키/고객ID 확인하세요.")

if __name__ == "__main__":
    main()
