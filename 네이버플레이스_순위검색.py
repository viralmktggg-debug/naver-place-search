"""
네이버 플레이스 키워드 검색 - 매장 순위 확인 프로그램 (웹앱)
특정 placeId를 가진 매장이 키워드 검색 결과에서 몇 위인지 확인
"""
import re
import csv
import time
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from playwright.sync_api import sync_playwright
from flask import Flask, render_template, request, jsonify, send_file
from threading import Thread, Lock
from apscheduler.schedulers.background import BackgroundScheduler
import io
import requests
from naver_searchad_keyword_volume import get_mobile_monthly
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("[경고] pandas가 설치되지 않았습니다. 엑셀 파일 불러오기 기능을 사용하려면 'pip install pandas openpyxl'을 실행하세요.")


# Place ID 추출 정규식
PLACE_ID_RE = re.compile(r"/(?:restaurant|place|hospital|accommodation|cafe|hairshop|academy|pharmacy|shopping|kids|etc)/(\d+)")


def extract_place_id(url: str):
    """URL에서 placeId 추출"""
    if not url:
        return None
    m = PLACE_ID_RE.search(url)
    return m.group(1) if m else None


class NaverPlaceRankSearch:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None
        self.is_connected_browser = False
        
        # 설정값
        self.max_rank = 300
        self.scroll_pause = 0.4  # 스크롤 대기 시간 최적화 (속도 향상)
        
        # 검색 상태
        self.search_status = {"status": "ready", "message": "준비", "progress": 0, "total": 0}
        self.last_results = []
        
        # 데이터 저장 경로 설정 (환경변수 DATA_DIR 지원)
        data_dir = Path(os.environ.get("DATA_DIR", "."))
        if not data_dir.exists():
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"[경고] DATA_DIR({data_dir}) 생성 실패: {e}. 현재 디렉토리를 사용합니다.")
                data_dir = Path(".")
        
        self.data_file = data_dir / "naver_place_rank_data.json"
        self.data_lock = Lock()
        
        # 캐시 (메모리 최적화: 자주 조회하는 데이터 캐싱)
        self._cache = {}
        self._cache_timeout = 300  # 5분 캐시
        
        self.load_data()

    def _scroll_list_down(self, repeat: int = 1):
        """스크롤 컨테이너(목록 영역)를 아래로 내림. 내부 스크롤이 있으면 그것을 우선 사용."""
        # page 유효성 확인
        try:
            _ = self.page.url
        except Exception:
            print("[경고] 스크롤 중 page가 유효하지 않습니다.")
            return
        
        scroll_js = """
        (repeat) => {
            const scrollOnce = () => {
                // 스크롤 가능한 컨테이너 후보 찾기
                const candidates = Array.from(
                    document.querySelectorAll('div, section, main, ul, ol, article')
                );
                let target = null;
                for (const el of candidates) {
                    const style = getComputedStyle(el);
                    const sh = el.scrollHeight;
                    const ch = el.clientHeight;
                    if (!sh || !ch) continue;
                    // 세로 스크롤이 가능하고, 어느 정도 높이가 있는 영역만 대상
                    if (
                        sh - ch > 100 &&
                        ch > 300 &&
                        (style.overflowY === 'auto' ||
                         style.overflowY === 'scroll' ||
                         style.overflowY === 'overlay')
                    ) {
                        target = el;
                        break;
                    }
                }
                if (target) {
                    target.scrollTop = target.scrollHeight;
                } else {
                    // 적절한 컨테이너를 못 찾으면 윈도우 스크롤
                    window.scrollTo(0, document.body.scrollHeight);
                }
            };
            for (let i = 0; i < repeat; i++) {
                scrollOnce();
            }
        }
        """
        try:
            self.page.evaluate(scroll_js, repeat)
        except Exception as e:
            if "cannot switch to a different thread" in str(e):
                print("[경고] 스크롤 중 스레드 문제 감지.")
                raise
            # 실패 시 기본 윈도우 스크롤로 폴백
            try:
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
    
    
    def init_browser(self, force_reinit=False):
        """브라우저 초기화"""
        # 강제 재초기화 또는 page가 None이거나 스레드 문제가 있을 때 재초기화
        if force_reinit or self.page is None:
            # 기존 브라우저 정리
            try:
                if self.page:
                    try:
                        self.page.close()
                    except:
                        pass
                if self.browser and not self.is_connected_browser:
                    try:
                        self.browser.close()
                    except:
                        pass
            except:
                pass
            
            self.page = None
            self.browser = None
            
            if self.playwright is None:
                self.playwright = sync_playwright().start()
            
            # 기존 Chrome 브라우저에 연결 시도
            connected = False
            try:
                self.browser = self.playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
                contexts = self.browser.contexts
                if contexts:
                    context = contexts[0]
                    pages = context.pages
                    if pages:
                        self.page = pages[0]
                    else:
                        self.page = context.new_page()
                else:
                    context = self.browser.new_context()
                    self.page = context.new_page()
                
                connected = True
                self.is_connected_browser = True
                self.search_status = {"status": "connected", "message": "기존 Chrome 브라우저에 연결됨", "progress": 0, "total": 0}
                print("[연결 성공] 기존 Chrome 브라우저에 연결되었습니다.")
                
            except Exception as e:
                # 연결 실패 시 새 브라우저 실행
                print(f"[연결 실패] 기존 Chrome에 연결할 수 없습니다: {e}")
                print("[대체 방법] 새 브라우저를 실행합니다.")
                
                try:
                    # 새 브라우저 구동 (서버/로컬 환경 분기를 위한 headless 처리)
                    headless_mode = os.environ.get('PLAYWRIGHT_HEADLESS', 'False').lower() == 'true'
                    launch_args = [
                        '--disable-blink-features=AutomationControlled',
                        '--disable-dev-shm-usage',
                        '--no-sandbox',
                    ]
                    if not headless_mode:
                        launch_args.extend([
                            '--incognito',                 # 시크릿 모드
                            '--start-minimized',
                            '--window-position=-2000,0'    # 화면 밖 위치
                        ])
                    
                    self.browser = self.playwright.chromium.launch(
                        headless=headless_mode,
                        args=launch_args
                    )
                    context = self.browser.new_context(
                        viewport={"width": 1200, "height": 900},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        locale="ko-KR",
                        timezone_id="Asia/Seoul"
                    )
                    self.page = context.new_page()
                    
                    # 자동화 감지 스크립트 제거
                    self.page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });
                    """)
                    
                    self.is_connected_browser = False
                    self.search_status = {"status": "ready", "message": "새 브라우저가 실행되었습니다", "progress": 0, "total": 0}
                    print("[성공] 새 브라우저가 실행되었습니다.")
                    connected = True
                    
                except Exception as e2:
                    error_msg = (
                        "브라우저를 시작할 수 없습니다.\n\n"
                        f"연결 오류: {str(e)}\n"
                        f"실행 오류: {str(e2)}\n\n"
                        "기존 Chrome을 사용하려면 다음 명령어로 실행하세요:\n\n"
                        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222'
                    )
                    self.search_status = {"status": "error", "message": f"브라우저 시작 실패: {str(e2)}", "progress": 0, "total": 0}
                    raise Exception(error_msg)
            
            if not connected:
                raise Exception("브라우저 연결 또는 실행에 실패했습니다.")
        
        # page가 있지만 스레드 문제가 있을 수 있으므로 테스트
        try:
            # 간단한 테스트로 page가 유효한지 확인
            _ = self.page.url
        except Exception:
            # page가 유효하지 않으면 재초기화
            print("[경고] 기존 page가 유효하지 않습니다. 재초기화합니다.")
            self.init_browser(force_reinit=True)
    
    def check_rank_for_keyword(self, keyword: str, target_place_id: str, max_rank: int):
        """특정 키워드로 검색하여 타겟 placeId의 순위 확인 (최적화 버전)"""
        # page 유효성 확인
        try:
            _ = self.page.url
        except Exception:
            print("[경고] page가 유효하지 않습니다. 브라우저를 재초기화합니다.")
            self.init_browser(force_reinit=True)
        
        # 불필요한 리소스 차단 (이미지, 폰트, 미디어, CSS 등) - 속도 최적화
        try:
            def route_handler(route):
                resource_type = route.request.resource_type
                # 이미지, 폰트, 미디어, CSS, 웹소켓 등 차단 (텍스트와 스크립트만 허용)
                if resource_type in ['image', 'font', 'media', 'stylesheet', 'websocket']:
                    route.abort()
                else:
                    route.continue_()
            
            # 기존 route가 있으면 제거 후 새로 설정
            self.page.unroute('**/*')
            self.page.route('**/*', route_handler)
        except Exception as e:
            print(f"[경고] 리소스 차단 설정 중 오류 (계속 진행): {e}")
        
        url = f"https://m.place.naver.com/place/list?query={quote(keyword)}"
        print(f"[페이지 로드] {url}")
        
        # 페이지 로드 최적화: domcontentloaded로 빠르게 시작 (더 짧은 타임아웃)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=8000)
        except Exception as e:
            if "cannot switch to a different thread" in str(e):
                print("[경고] 스레드 문제 감지. 브라우저를 재초기화합니다.")
                self.init_browser(force_reinit=True)
                self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
            else:
                raise
        time.sleep(0.3)  # 최소한의 대기 시간 (속도 향상을 위해 추가 단축)

        # 상단 헤더(검색 키워드 영역)가 나타날 때까지 추가 대기 (더 짧은 타임아웃)
        # 예: <header class="place_tab_shadow FFTct"> ... <h1 id="_header">대구맛집</h1> ...
        try:
            self.page.wait_for_selector("header.place_tab_shadow", timeout=3000)
            # 디버깅용으로 현재 헤더 텍스트 출력
            try:
                header_text = self.page.evaluate(
                    "() => document.querySelector('header.place_tab_shadow h1')?.textContent?.trim() || ''"
                )
                if header_text:
                    print(f"[헤더 확인] 현재 키워드 헤더: {header_text}")
            except:
                pass
        except:
            # 헤더를 못 찾더라도 치명적이지 않으므로 계속 진행
            pass
        
        # "목록보기" 버튼 클릭 (최적화된 대기, 더 짧은 타임아웃)
        try:
            # 가장 빠른 선택자부터 시도
            list_button_selectors = [
                ".AtjOO",
                "a.AtjOO",
                "a:has-text('목록보기')"
            ]
            
            list_button_clicked = False
            for selector in list_button_selectors:
                try:
                    # 짧은 타임아웃으로 빠르게 시도
                    self.page.wait_for_selector(selector, timeout=2000, state="visible")
                    list_button = self.page.locator(selector).first
                    
                    if list_button.count() > 0 and list_button.is_visible():
                        list_button.click()
                        time.sleep(0.3)  # 목록 로드 대기 시간 추가 단축
                        list_button_clicked = True
                        print(f"[목록보기] 버튼 클릭 성공!")
                        break
                except:
                    continue
            
            if not list_button_clicked:
                print("[경고] 목록보기 버튼을 찾을 수 없습니다. 계속 진행합니다.")
                time.sleep(0.3)
        except Exception as e:
            print(f"[경고] 목록보기 버튼 클릭 중 오류: {e}")
            time.sleep(0.3)
        
        seen_place_ids = set()  # 중복 체크용 (set으로 O(1) 조회)
        last_len = 0
        no_change_count = 0
        max_no_change = 4   # 연속으로 변화 없으면 더 빨리 중단
        scroll_count = 0
        max_scrolls = 80    # 최대 스크롤 횟수 감소 (속도 우선)
        
        # JavaScript로 빠르게 모든 링크를 한 번에 수집하는 함수
        extract_links_js = """
        () => {
            const links = Array.from(document.querySelectorAll('a.place_bluelink[href*="m.place.naver.com/place/"]'));
            return links.map(link => ({
                href: link.href,
                title: (link.querySelector('span.YwYLL')?.textContent || link.textContent || '').trim()
            }));
        }
        """
        
        print(f"[검색 시작] 최대 {max_rank}위까지 확인...")
        
        while len(seen_place_ids) < max_rank and scroll_count < max_scrolls:
            # JavaScript로 한 번에 모든 링크 정보 추출 (더 빠름)
            try:
                links_data = self.page.evaluate(extract_links_js)
            except:
                # JavaScript 실패 시 Playwright 방식으로 폴백
                place_links = self.page.locator("a.place_bluelink[href*='m.place.naver.com/place/']").all()
                links_data = []
                for link in place_links:
                    try:
                        href = link.get_attribute("href")
                        # 광고(PLACE_AD, ader.naver.com 등) 링크는 스킵
                        if not href:
                            continue
                        href_lower = href.lower()
                        if (
                            "place_ad" in href_lower
                            or "from%3dplace_ad" in href_lower
                            or "ader.naver.com" in href_lower
                        ):
                            continue

                        title_elem = link.locator("span.YwYLL").first
                        title = title_elem.inner_text().strip() if title_elem.count() > 0 else link.inner_text().strip()
                        if href:
                            links_data.append({"href": href, "title": title})
                    except:
                        continue
            
            # 추출한 링크들을 순서대로 처리
            for link_info in links_data:
                href = link_info.get("href", "")
                if not href:
                    continue

                # 광고(PLACE_AD, ader.naver.com 등) 링크는 순위에서 제외
                href_lower = href.lower()
                if (
                    "place_ad" in href_lower
                    or "from%3dplace_ad" in href_lower
                    or "ader.naver.com" in href_lower
                ):
                    continue
                
                pid = extract_place_id(href)
                if not pid:
                    continue
                
                # 중복 체크 (이미 set에 있으면 스킵)
                if pid in seen_place_ids:
                    continue
                
                seen_place_ids.add(pid)
                rank = len(seen_place_ids)
                
                # 50위마다 진행 상황 출력 (너무 많은 로그 방지)
                if rank % 50 == 0 or rank <= 10:
                    title = link_info.get("title", "")
                    print(f"[순위 {rank}] {title[:30]}... (placeId: {pid})")
                
                # 타겟 찾으면 즉시 반환
                if pid == target_place_id:
                    title = link_info.get("title", "")
                    print(f"[발견!] 순위 {rank}위: {title} (placeId: {pid})")
                    return rank, href, title  # 매장명도 함께 반환
                
                # 목표 순위 도달
                if rank >= max_rank:
                    break
            
            # 새로운 항목이 없으면 카운트 증가
            current_count = len(seen_place_ids)
            if current_count == last_len:
                no_change_count += 1
                
                # 변화 없을 때 더 적극적인 스크롤 시도
                if no_change_count >= 3:
                    print(f"[스크롤 강화] 변화 없음 ({no_change_count}회). 적극적 스크롤 시도 중... (현재: {current_count}개)")
                    # 여러 번 연속 스크롤 (더 많은 항목 로드)
                    for i in range(5):
                        # 목록 컨테이너 기준으로 스크롤
                        self._scroll_list_down()
                        time.sleep(self.scroll_pause * 0.5)
                    
                    # 스크롤 후 충분히 대기
                    time.sleep(self.scroll_pause * 2)
                    
                    # 다시 링크 확인하고 업데이트
                    try:
                        new_links_data = self.page.evaluate(extract_links_js)
                        if len(new_links_data) > len(links_data):
                            no_change_count = 0  # 새로운 항목 발견 시 리셋
                            print(f"[성공] 추가 항목 발견! ({len(new_links_data)}개)")
                            links_data = new_links_data  # 링크 데이터 업데이트
                            # 업데이트된 링크 데이터로 다시 처리하도록 루프 계속
                    except:
                        pass
                
                if no_change_count >= max_no_change:
                    print(f"[경고] 연속 {max_no_change}회 변화 없음. 최종 추가 시도 중... (현재: {current_count}개)")
                    # 최종 추가 스크롤 시도 (매우 적극적으로)
                    for _ in range(10):
                        # 목록 컨테이너 기준으로 강하게 스크롤
                        self._scroll_list_down(2)
                        time.sleep(self.scroll_pause)
                    
                    time.sleep(self.scroll_pause * 3)  # 충분한 대기
                    
                    # 최종 확인
                    try:
                        final_links_data = self.page.evaluate(extract_links_js)
                        if len(final_links_data) > len(links_data):
                            no_change_count = 0
                            print(f"[성공] 최종 시도에서 추가 항목 발견!")
                            links_data = final_links_data  # 링크 데이터 업데이트
                            # 업데이트된 링크 데이터로 다시 처리하도록 루프 계속
                    except:
                        pass
                    
                    # 여전히 변화 없으면 중단
                    if len(seen_place_ids) == last_len:
                        print(f"[완료] 더 이상 새로운 항목이 없습니다. (현재: {len(seen_place_ids)}개)")
                        break
            else:
                no_change_count = 0  # 새로운 항목이 있으면 리셋
            
            last_len = len(seen_place_ids)
            
            # 목표 순위 미도달 시 스크롤
            if len(seen_place_ids) < max_rank:
                scroll_count += 1
                
                # 기본 스크롤 (목록 컨테이너 기준)
                self._scroll_list_down()
                time.sleep(self.scroll_pause)
                
                # 매번 추가 스크롤 (조금 더)
                self._scroll_list_down()
                time.sleep(self.scroll_pause * 0.5)
                
                # 3회마다 더 적극적으로 스크롤
                if scroll_count % 3 == 0:
                    for _ in range(2):
                        self._scroll_list_down(2)
                        time.sleep(self.scroll_pause * 0.7)
        
        print(f"[검색 완료] 총 {len(seen_place_ids)}개 매장 확인")
        return None, None, None  # max_rank 안에 없음
    
    def check_ranks(self, place_id: str, keywords_text: str, max_rank: int = 300, thread_local_browser=None, group_name: str = None):
        """순위 확인 실행
        
        Args:
            place_id: 매장 Place ID
            keywords_text: 검색 키워드 (쉼표로 구분)
            max_rank: 최대 순위
            thread_local_browser: 스레드 로컬 브라우저 객체 (스레드 안전성을 위해)
            group_name: 그룹명 (순위 저장용)
        """
        if not place_id:
            raise ValueError("매장 Place ID를 입력해주세요.")
        
        if not keywords_text:
            raise ValueError("검색 키워드를 입력해주세요.")
        
        self.max_rank = max_rank
        self.current_group = group_name  # 그룹 정보 저장
        
        # 키워드 파싱 (쉼표로 구분)
        keywords = [kw.strip() for kw in keywords_text.split(",") if kw.strip()]
        
        if not keywords:
            raise ValueError("검색 키워드를 입력해주세요.")
        
        # 결과 초기화
        self.last_results = []
        self.search_status = {"status": "searching", "message": "검색 중...", "progress": 0, "total": len(keywords)}
        
        # 스레드 로컬 브라우저 사용 (스레드 안전성)
        use_thread_local = thread_local_browser is not None
        
        try:
            # 스레드 로컬 브라우저가 있으면 사용, 없으면 기존 방식
            if use_thread_local:
                # 스레드 로컬 브라우저 사용
                original_playwright = self.playwright
                original_browser = self.browser
                original_page = self.page
                original_is_connected = self.is_connected_browser
                
                self.playwright = thread_local_browser['playwright']
                self.browser = thread_local_browser['browser']
                self.page = thread_local_browser['page']
                self.is_connected_browser = thread_local_browser['is_connected_browser']
            else:
                # 재검색 시 브라우저 재초기화 (스레드 문제 방지)
                self.init_browser(force_reinit=True)
            
            results = []
            
            # 배치 저장을 위해 저장할 순위 데이터 수집
            rankings_to_save = []
            
            for idx, keyword in enumerate(keywords, 1):
                self.search_status = {
                    "status": "searching",
                    "message": f"검색 중... ({idx}/{len(keywords)}) - {keyword}",
                    "progress": idx,
                    "total": len(keywords)
                }
                
                try:
                    rank, link, place_name = self.check_rank_for_keyword(keyword, place_id, self.max_rank)
                    
                    rank_text = f"{rank}위" if rank else f"{self.max_rank}위 밖/미노출"
                    
                    # 그룹 정보가 있으면 순위 저장 (배치 저장용 데이터 수집)
                    if hasattr(self, 'current_group') and self.current_group:
                        if rank and place_name:
                            rankings_to_save.append({
                                "group_name": self.current_group,
                                "keyword": keyword,
                                "place_id": place_id,
                                "place_name": place_name,
                                "rank": rank,
                                "link": link or ""
                            })
                    
                    results.append({
                        "keyword": keyword,
                        "rank": rank_text,
                        "link": link if link else ""
                    })
                    
                    print(f"[{keyword}] -> {rank_text}")
                    
                except Exception as e:
                    error_msg = f"키워드 '{keyword}' 검색 중 오류: {str(e)}"
                    print(f"[오류] {error_msg}")
                    results.append({
                        "keyword": keyword,
                        "rank": "오류",
                        "link": str(e)
                    })
            
            # 배치 저장 (모든 키워드 검색 후 한 번만 저장)
            if rankings_to_save:
                for ranking_data in rankings_to_save:
                    self.save_ranking(
                        ranking_data["group_name"],
                        ranking_data["keyword"],
                        ranking_data["place_id"],
                        ranking_data["place_name"],
                        ranking_data["rank"],
                        ranking_data["link"],
                        skip_save=True  # 개별 저장은 하지 않고
                    )
                # 모든 순위 저장 후 한 번만 파일에 저장 (I/O 최적화)
                self.save_data()
                
                # 재검색 완료 후 해당 키워드들의 카드 데이터 캐시 무효화
                for ranking_data in rankings_to_save:
                    cache_key = f"card_data:{ranking_data['group_name']}:{ranking_data['keyword']}"
                    if cache_key in self._cache:
                        del self._cache[cache_key]
            
            self.search_status = {
                "status": "completed",
                "message": f"완료: {len(keywords)}개 키워드 검색 완료",
                "progress": len(keywords),
                "total": len(keywords)
            }
            
            # 결과 저장
            self.last_results = results
            
            # 스레드 로컬 브라우저 사용 시 원래 객체 복원
            if use_thread_local:
                self.playwright = original_playwright
                self.browser = original_browser
                self.page = original_page
                self.is_connected_browser = original_is_connected
            
            return results
            
        except Exception as e:
            self.search_status = {"status": "error", "message": f"오류: {str(e)}", "progress": 0, "total": 0}
            
            # 스레드 로컬 브라우저 사용 시 원래 객체 복원
            if use_thread_local:
                try:
                    self.playwright = original_playwright
                    self.browser = original_browser
                    self.page = original_page
                    self.is_connected_browser = original_is_connected
                except:
                    pass
            
            raise
    
    def load_data(self):
        """저장된 데이터 로드"""
        with self.data_lock:
            if self.data_file.exists():
                try:
                    with open(self.data_file, 'r', encoding='utf-8') as f:
                        self.data = json.load(f)
                except:
                    self.data = {
                        "groups": [],
                        "keywords": {},  # {group_name: [keyword1, keyword2, ...]}
                        "rankings": {},  # {group_name: {keyword: {place_id: {place_name, rankings: [{date, rank, link}]}}}}
                        "place_details": {},  # {place_id: {details: [{date, saveCount, visitorReviewCount, blogReviewCount}]}}
                        "keyword_mobile_volume": {}  # {group_name: {keyword: mobile_monthly_search}}
                    }
            else:
                self.data = {
                    "groups": [],
                    "keywords": {},
                    "rankings": {},
                    "place_details": {},
                    "keyword_mobile_volume": {}
                }
            
            # 기존 데이터에 place_details가 없으면 추가
            if "place_details" not in self.data:
                self.data["place_details"] = {}
            # 기존 데이터에 keyword_mobile_volume이 없으면 추가
            if "keyword_mobile_volume" not in self.data:
                self.data["keyword_mobile_volume"] = {}
    
    def save_data(self):
        """데이터 저장"""
        with self.data_lock:
            try:
                with open(self.data_file, 'w', encoding='utf-8') as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[데이터 저장 오류] {str(e)}")
    
    def add_group(self, group_name: str, skip_save: bool = False):
        """그룹 추가

        skip_save=True 이면 메모리만 갱신하고 파일 저장은 나중에 한 번에 수행
        """
        if group_name not in self.data["groups"]:
            self.data["groups"].append(group_name)

        # 키워드/랭킹 구조가 없으면 초기화
        if group_name not in self.data["keywords"]:
            self.data["keywords"][group_name] = []
        if group_name not in self.data["rankings"]:
            self.data["rankings"][group_name] = {}

        if not skip_save:
            self.save_data()
        return True
    
    def delete_group(self, group_name: str):
        """그룹 삭제"""
        if group_name in self.data["groups"]:
            self.data["groups"].remove(group_name)
            if group_name in self.data["keywords"]:
                del self.data["keywords"][group_name]
            if group_name in self.data["rankings"]:
                del self.data["rankings"][group_name]
            self.save_data()
            return True
        return False
    
    def add_keyword(self, group_name: str, keyword: str, skip_save: bool = False):
        """그룹에 키워드 추가"""
        if group_name not in self.data["groups"]:
            self.add_group(group_name, skip_save=skip_save)
        
        if group_name not in self.data["keywords"]:
            self.data["keywords"][group_name] = []
        
        if keyword not in self.data["keywords"][group_name]:
            self.data["keywords"][group_name].append(keyword)
            if not skip_save:
                self.save_data()
            return True
        return False
    
    def delete_keyword(self, group_name: str, keyword: str):
        """그룹에서 키워드 삭제"""
        if group_name in self.data["keywords"]:
            if keyword in self.data["keywords"][group_name]:
                self.data["keywords"][group_name].remove(keyword)
                # 관련 순위 데이터도 삭제
                if group_name in self.data["rankings"] and keyword in self.data["rankings"][group_name]:
                    del self.data["rankings"][group_name][keyword]
                self.save_data()
                return True
        return False
    
    def save_ranking(self, group_name: str, keyword: str, place_id: str, place_name: str, rank: int, link: str = "", date: str = None, skip_save: bool = False):
        """날짜별 순위 저장"""
        if group_name not in self.data["rankings"]:
            self.data["rankings"][group_name] = {}
        
        if keyword not in self.data["rankings"][group_name]:
            self.data["rankings"][group_name][keyword] = {}
        
        if place_id not in self.data["rankings"][group_name][keyword]:
            self.data["rankings"][group_name][keyword][place_id] = {
                "place_name": place_name,
                "rankings": []
            }
        
        # 날짜가 지정되지 않으면 오늘 날짜 사용
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        rankings = self.data["rankings"][group_name][keyword][place_id]["rankings"]
        existing_ranking = next((r for r in rankings if r["date"] == date), None)
        
        if existing_ranking:
            # 이미 해당 날짜 순위가 있으면 업데이트
            existing_ranking["rank"] = rank
            if link:
                existing_ranking["link"] = link
            existing_ranking["search_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            # 새로운 순위 추가
            rankings.append({
                "date": date,
                "rank": rank,
                "link": link,
                "search_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            # 날짜순으로 정렬
            rankings.sort(key=lambda x: x["date"])
        
        if not skip_save:
            self.save_data()
    
    def import_from_excel(self, file_path: str):
        """엑셀 파일에서 키워드와 플레이스명만 불러오기 (A열: 플레이스명, B열: 그룹, C열: 키워드)

        - 순위 데이터(H열 이후)는 무시
        - A/B/C 열만 사용해서 그룹과 키워드만 추가
        """
        if not PANDAS_AVAILABLE:
            raise Exception("pandas가 설치되지 않았습니다. 'pip install pandas openpyxl'을 실행하세요.")

        # 엑셀 파일 읽기 (헤더 없이)
        df = pd.read_excel(file_path, header=None)

        imported_count = 0

        # 데이터 행 처리
        for idx, row in df.iterrows():
            # 최소 3열(A,B,C)이 없으면 스킵
            if len(row) < 3:
                continue

            place_raw = row.iloc[0]
            group_raw = row.iloc[1]
            keyword_raw = row.iloc[2]

            place_name = str(place_raw).strip() if pd.notna(place_raw) else ""
            group_name = str(group_raw).strip() if pd.notna(group_raw) else ""
            keyword = str(keyword_raw).strip() if pd.notna(keyword_raw) else ""

            # 필수 값이 비어있으면 스킵
            if not place_name or not group_name or not keyword:
                continue

            # 헤더 행 스킵 (첫 행이 "플레이스명 / 그룹 / 키워드" 등인 경우)
            if place_name in ["플레이스명", "Place Name", "매장명", "상호명"] \
               or group_name in ["그룹", "Group", "그룹명"] \
               or keyword in ["키워드", "Keyword", "검색어"]:
                continue

            # 그룹 추가 (없으면 생성)
            if group_name not in self.data["groups"]:
                self.add_group(group_name, skip_save=True)

            # 키워드 리스트 초기화
            if group_name not in self.data["keywords"]:
                self.data["keywords"][group_name] = []

            # 키워드 추가
            if keyword not in self.data["keywords"][group_name]:
                self.data["keywords"][group_name].append(keyword)
                imported_count += 1

            # D열에 있는 URL에서 place_id 추출 (예: https://m.place.naver.com/restaurant/1905233616)
            place_id = None
            if len(row) > 3:
                d_raw = row.iloc[3]
                if pd.notna(d_raw):
                    url = str(d_raw).strip()
                    place_id = extract_place_id(url)

            # D열이 없거나 URL에서 추출 실패 시, 플레이스명에서 괄호 속 숫자 형태로 시도 (예: "모토이시 안산고잔점(1905233616)")
            if not place_id and place_name:
                m = re.search(r"\((\d+)\)", place_name)
                if m:
                    place_id = m.group(1)

            # place_id를 알 수 있으면 rankings 구조에 기본 정보만 채워둔다 (순위는 나중에 검색 시 저장)
            if place_id:
                if group_name not in self.data["rankings"]:
                    self.data["rankings"][group_name] = {}
                if keyword not in self.data["rankings"][group_name]:
                    self.data["rankings"][group_name][keyword] = {}
                if place_id not in self.data["rankings"][group_name][keyword]:
                    self.data["rankings"][group_name][keyword][place_id] = {
                        "place_name": place_name,
                        "rankings": []  # 아직 순위 이력은 없음
                    }

        # 모든 데이터 처리 후 한 번만 저장
        if imported_count > 0:
            self.save_data()

        return {
            "success": True,
            "imported": imported_count,
            "errors": []
        }
    
    def _parse_date_from_header(self, date_header: str):
        """헤더에서 날짜 파싱 (예: "01-05" -> "2026-01-05")"""
        try:
            # "01-05" 형식
            if re.match(r'^\d{2}-\d{2}$', date_header):
                month, day = date_header.split('-')
                # 현재 연도 사용
                current_year = datetime.now().year
                # 날짜가 미래면 작년으로 간주
                try:
                    test_date = datetime(current_year, int(month), int(day))
                    if test_date > datetime.now():
                        current_year = current_year - 1
                except:
                    pass
                return f"{current_year}-{month}-{day}"
            # "2024-01-05" 형식
            elif re.match(r'^\d{4}-\d{2}-\d{2}$', date_header):
                return date_header
            # "01/05" 형식
            elif re.match(r'^\d{2}/\d{2}$', date_header):
                month, day = date_header.split('/')
                current_year = datetime.now().year
                try:
                    test_date = datetime(current_year, int(month), int(day))
                    if test_date > datetime.now():
                        current_year = current_year - 1
                except:
                    pass
                return f"{current_year}-{month.zfill(2)}-{day.zfill(2)}"
        except:
            pass
        return None
    
    def get_rankings_history(self, group_name: str, keyword: str, place_id: str):
        """순위 이력 조회"""
        if (group_name in self.data["rankings"] and 
            keyword in self.data["rankings"][group_name] and
            place_id in self.data["rankings"][group_name][keyword]):
            return self.data["rankings"][group_name][keyword][place_id]
        return None
    
    def get_all_rankings_for_group(self, group_name: str):
        """그룹의 모든 순위 데이터 조회"""
        if group_name not in self.data["rankings"]:
            return []
        
        # 날짜 미리 계산 (반복 계산 방지 - 성능 최적화)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        one_month_ago_str = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        
        results = []
        for keyword, places in self.data["rankings"][group_name].items():
            for place_id, place_data in places.items():
                place_name = place_data["place_name"]
                rankings = place_data["rankings"]
                
                # 최신 순위 정보 (효율적으로 찾기)
                today_rank = None
                yesterday_rank = None
                first_rank = rankings[0] if rankings else None
                
                # 정렬된 리스트에서 효율적으로 찾기
                for r in rankings:
                    if r["date"] == today:
                        today_rank = r
                    elif r["date"] == yesterday:
                        yesterday_rank = r
                    elif r["date"] > today:  # 오늘 이후 데이터는 없음
                        break
                
                # 최근 1개월 데이터 (30일) - 문자열 비교로 최적화
                monthly_rankings = [r for r in rankings if r.get("date", "") >= one_month_ago_str]
                
                results.append({
                    "keyword": keyword,
                    "place_id": place_id,
                    "place_name": place_name,
                    "today_rank": today_rank["rank"] if today_rank else None,
                    "yesterday_rank": yesterday_rank["rank"] if yesterday_rank else None,
                    "first_rank": first_rank["rank"] if first_rank else None,
                    "all_rankings": rankings,
                    "monthly_rankings": monthly_rankings
                })
        
        return results
    
    def get_keyword_card_data(self, group_name: str, keyword: str):
        """특정 키워드의 카드 데이터 조회 (캐싱 최적화)"""
        # 캐시 키 생성
        cache_key = f"card_data:{group_name}:{keyword}"
        current_time = time.time()
        
        # 캐시 확인
        if cache_key in self._cache:
            cached_data, cached_time = self._cache[cache_key]
            if current_time - cached_time < self._cache_timeout:
                return cached_data
            else:
                # 캐시 만료
                del self._cache[cache_key]
        
        if group_name not in self.data["rankings"] or keyword not in self.data["rankings"][group_name]:
            return None
        
        places = self.data["rankings"][group_name][keyword]
        if not places:
            return None
        
        # 첫 번째 place_id 사용 (일반적으로 하나의 매장)
        place_id = list(places.keys())[0]
        place_data = places[place_id]
        rankings = place_data["rankings"]
        
        # 키워드 모바일 월간 검색수
        mobile_volume = (
            self.data.get("keyword_mobile_volume", {})
                .get(group_name, {})
                .get(keyword)
        )
        
        # 최신 순위 정보
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # 최적화: 날짜 필터링을 먼저 수행하여 조회 최소화
        today_rank = None
        yesterday_rank = None
        first_rank = rankings[0] if rankings else None
        
        # 날짜순 정렬된 리스트에서 효율적으로 찾기 (이미 정렬되어 있음)
        for r in rankings:
            if r["date"] == today:
                today_rank = r
            elif r["date"] == yesterday:
                yesterday_rank = r
            elif r["date"] > today:  # 오늘 이후 데이터는 없음 (정렬된 상태에서)
                break
        
        # 최근 1개월 데이터 (30일) - 문자열 비교로 최적화
        one_month_ago_str = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        monthly_rankings = [r for r in rankings if r.get("date", "") >= one_month_ago_str]
        
        result = {
            "group_name": group_name,
            "keyword": keyword,
            "place_id": place_id,
            "place_name": place_data["place_name"],
            "today_rank": today_rank["rank"] if today_rank else None,
            "yesterday_rank": yesterday_rank["rank"] if yesterday_rank else None,
            "first_rank": first_rank["rank"] if first_rank else None,
            "monthly_rankings": monthly_rankings,
            "mobile_monthly_search": mobile_volume
        }
        
        # 캐시 저장
        self._cache[cache_key] = (result, current_time)
        
        # 캐시 크기 제한 (메모리 관리)
        if len(self._cache) > 100:
            # 가장 오래된 캐시 제거
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]
        
        return result
    
    def get_csv_data(self, group_name: str = None):
        """결과를 CSV 형식의 문자열로 반환"""
        # last_results가 있으면 사용
        if self.last_results:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["keyword", "rank", "link"])
            writer.writeheader()
            writer.writerows(self.last_results)
            return output.getvalue()
        
        # 그룹명이 제공되면 그룹의 모든 순위 데이터를 CSV로 변환
        if group_name and group_name in self.data["rankings"]:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["keyword", "place_id", "place_name", "date", "rank", "link", "search_time"])
            writer.writeheader()
            
            for keyword, places in self.data["rankings"][group_name].items():
                for place_id, place_data in places.items():
                    for ranking in place_data["rankings"]:
                        writer.writerow({
                            "keyword": keyword,
                            "place_id": place_id,
                            "place_name": place_data["place_name"],
                            "date": ranking["date"],
                            "rank": ranking["rank"],
                            "link": ranking.get("link", ""),
                            "search_time": ranking.get("search_time", "")
                        })
            
            return output.getvalue()
        
        return None
    
    def clear_results(self):
        """결과 초기화"""
        self.last_results = []
        self.search_status = {"status": "ready", "message": "준비", "progress": 0, "total": 0}
    
    def __del__(self):
        """종료 시 브라우저 정리"""
        try:
            if self.playwright:
                if self.browser:
                    if not self.is_connected_browser:
                        try:
                            self.browser.close()
                        except:
                            pass
                    else:
                        try:
                            self.browser.close()
                        except:
                            pass
                self.playwright.stop()
        except:
            pass

    # -------------------- 네이버 지도 플레이스 정보 조회 --------------------
    def get_place_info(self, keyword: str, place_name: str | None = None, place_id: str | None = None):
        """
        네이버 플레이스 검색 페이지에서 매장 정보 추출 (saveCount, 리뷰 수 등)
        네이버저장.py의 extract_place_info 로직을 참고하여 구현

        Args:
        keyword: 카드의 키워드 (예: '전주맛집') - 보조용
        place_name: 플레이스명 (예: '모토이시 분당야탑점') - 실제 검색어로 사용
            place_id: 플레이스 ID (선택사항, 정확한 매칭에 사용)

        Returns:
            dict: {
                "placeId": str,
                "placeName": str,
                "saveCount": int,
                "visitorReviewCount": int,
                "blogReviewCount": int,
                "search_url": str
            } 또는 None (실패 시)
        """
        import sys
        print(f"[get_place_info] 메서드 호출됨: keyword={keyword}, place_name={place_name}, place_id={place_id}", file=sys.stderr, flush=True)
        try:
            # 실제 검색어는 업체명 우선, 없으면 키워드 사용
            search_query = place_name or keyword
            print(f"[get_place_info] 검색어 결정: search_query={search_query}", file=sys.stderr, flush=True)
            if not search_query:
                print(f"[get_place_info] 검색어가 없어서 None 반환", file=sys.stderr, flush=True)
                return None

            # 네이버 플레이스 검색 URL (음식점 리스트 페이지 - 저장 수 확인용)
            # 브라우저에서 저장 수가 보이는 주소:
            #   view-source:https://pcmap.place.naver.com/restaurant/list?query={매장명}
            # 여기서는 view-source 대신 원본 HTML을 직접 로드
            search_url = f"https://pcmap.place.naver.com/restaurant/list?query={quote(search_query)}"
            print(f"[get_place_info] 검색 URL: {search_url}", file=sys.stderr, flush=True)
            
            place_id_found = None
            found_place_name = None
            save_count = None
            visitor_review_count = None
            blog_review_count = None
            
            collected_responses = []
            
            # 기존 브라우저에 연결 시도 (재사용으로 메모리 절약)
            temp_playwright = None
            browser = None
            context = None
            page = None
            use_existing = False
            try:
                # 먼저 기존 Chrome 브라우저에 연결 시도 (메모리 절약)
                try:
                    temp_playwright = sync_playwright().start()
                    browser = temp_playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    contexts = browser.contexts
                    if contexts:
                        context = contexts[0]
                        pages = context.pages
                        if pages:
                            page = pages[0]
                        else:
                            page = context.new_page()
                    else:
                        context = browser.new_context()
                        page = context.new_page()
                    use_existing = True
                    print(f"[get_place_info] 기존 Chrome 브라우저 재사용", file=sys.stderr, flush=True)
                except Exception:
                    # 연결 실패 시 새 브라우저 생성
                    if temp_playwright is None:
                        temp_playwright = sync_playwright().start()
                    browser = temp_playwright.chromium.launch(
                        headless=True,
                        args=[
                            '--disable-blink-features=AutomationControlled',
                            '--disable-dev-shm-usage',
                            '--no-sandbox',
                            '--disable-images',  # 이미지 로드 비활성화 (메모리 절약)
                            '--disable-javascript-harmony-shipping',
                            '--disable-background-networking',  # 백그라운드 네트워크 비활성화
                        ]
                    )
                    context = browser.new_context(
                        viewport={'width': 1920, 'height': 1080},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        locale='ko-KR',
                        timezone_id='Asia/Seoul',
                        extra_http_headers={
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'Accept-Language': 'ko-KR,ko;q=0.9',
                            'Accept-Encoding': 'gzip, deflate, br',
                            'Connection': 'keep-alive',
                        }
                    )
                    page = context.new_page()
                    print(f"[get_place_info] 새 브라우저 생성", file=sys.stderr, flush=True)
                
                # 자동화 탐지 스크립트 제거
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                """)
                
                # 불필요한 리소스 차단 (이미지, 폰트, 미디어 등) - 메모리 및 속도 최적화
                def route_handler(route):
                    resource_type = route.request.resource_type
                    if resource_type in ['image', 'font', 'media', 'stylesheet', 'websocket']:
                        route.abort()
                    else:
                        route.continue_()
                
                page.route('**/*', route_handler)
                
                # 네트워크 응답 가로채기
                print(f"[get_place_info] 네트워크 응답 리스너 설정 중...", file=sys.stderr, flush=True)
                def on_response(response):
                    try:
                        content_type = response.headers.get("content-type", "")
                        if "application/json" in content_type or "text/json" in content_type:
                            url = response.url
                            if any(keyword in url.lower() for keyword in ["restaurant", "place", "list", "api"]):
                                try:
                                    data = response.json()
                                    collected_responses.append((url, data))
                                except:
                                    pass
                    except:
                        pass
                
                page.on("response", on_response)
                print(f"[get_place_info] 네트워크 응답 리스너 설정 완료", file=sys.stderr, flush=True)
                
                # 검색 결과 페이지 접속 (domcontentloaded로 빠르게 시작)
                print(f"[get_place_info] 페이지 접속 중: {search_url}", file=sys.stderr, flush=True)
                page.goto(search_url, wait_until="domcontentloaded", timeout=10000)
                # 최소한의 대기 (네트워크 응답 수집을 위해)
                page.wait_for_timeout(1500)  # 3초 → 1.5초로 단축 (메모리 및 속도 최적화)
                print(f"[get_place_info] 페이지 접속 완료", file=sys.stderr, flush=True)
                
                # 페이지 소스 가져오기
                page_source = page.content()
                print(f"[플레이스 정보] 검색어 '{search_query}'로 검색 결과 페이지 접속 완료", file=sys.stderr, flush=True)
                print(f"[플레이스 정보] 수집된 네트워크 응답 수: {len(collected_responses)}", file=sys.stderr, flush=True)
                print(f"[플레이스 정보] 페이지 소스 길이: {len(page_source)} 문자", file=sys.stderr, flush=True)
                
                # 네트워크 응답에서 찾기 (네이버저장.py 방식)
                for url, data in collected_responses:
                    if isinstance(data, dict):
                        # place_id가 제공된 경우에는 place_id로만 매칭 (정확도 우선)
                        if place_id:
                            def find_by_id(obj, target_id):
                                if isinstance(obj, dict):
                                    if 'id' in obj and str(obj.get('id', '')) == target_id:
                                        return obj
                                    for value in obj.values():
                                        result = find_by_id(value, target_id)
                                        if result:
                                            return result
                                elif isinstance(obj, list):
                                    for item in obj:
                                        if isinstance(item, dict) and 'id' in item:
                                            if str(item.get('id', '')) == target_id:
                                                return item
                                    # 리스트에서 찾지 못했으면 None 반환 (첫 번째 항목 사용하지 않음)
                                return None
                            place_data = find_by_id(data, place_id)
                        else:
                            # place_id가 없으면 이름으로 검색
                            def find_place_data(obj, target_name):
                                if isinstance(obj, dict):
                                    # name 필드가 검색어와 일치하는지 확인
                                    if 'name' in obj and target_name in str(obj.get('name', '')):
                                        return obj
                                    for value in obj.values():
                                        result = find_place_data(value, target_name)
                                        if result:
                                            return result
                                elif isinstance(obj, list):
                                    # 리스트의 첫 번째 항목 또는 매칭되는 항목 찾기
                                    for item in obj:
                                        if isinstance(item, dict) and 'name' in item:
                                            if target_name in str(item.get('name', '')):
                                                return item
                                    # 매칭되지 않으면 첫 번째 항목 사용
                                    if obj and isinstance(obj[0], dict):
                                        return obj[0]
                                return None
                            
                            # place_name이 있으면 그것을 우선 사용, 없으면 search_query 사용
                            search_target = place_name if place_name else search_query
                            place_data = find_place_data(data, search_target)
                        if place_data:
                            if not place_id_found and 'id' in place_data:
                                place_id_found = str(place_data['id'])
                            if not found_place_name and 'name' in place_data:
                                found_place_name = place_data['name']
                            if save_count is None and 'saveCount' in place_data:
                                save_str = str(place_data['saveCount']).replace(",", "").replace("+", "")
                                save_match = re.search(r'\d+', save_str)
                                if save_match:
                                    save_count = int(save_match.group())
                            if visitor_review_count is None and 'visitorReviewCount' in place_data:
                                visitor_review_count = int(str(place_data['visitorReviewCount']).replace(",", ""))
                            if blog_review_count is None and 'blogCafeReviewCount' in place_data:
                                blog_review_count = int(str(place_data['blogCafeReviewCount']).replace(",", ""))
                            print(f"[플레이스 정보] 네트워크 응답에서 매장 정보 발견", file=sys.stderr, flush=True)
                        break

                # 페이지 소스에서 검색 결과 찾기 (네이버저장.py 방식)
                
                # 방법 1: RestaurantListSummary 패턴으로 찾기
                summary_pattern = r'RestaurantListSummary:(\d+):\s*(\{[^}]+\})'
                summary_matches = list(re.finditer(summary_pattern, page_source, re.DOTALL))
                
                # place_id가 제공된 경우 정확히 일치하는 것만 찾기
                if place_id:
                    for match in summary_matches:
                        found_id = match.group(1)
                        if found_id == place_id:  # 정확히 일치하는 경우만 사용
                            json_str = match.group(2)
                            try:
                                data = json.loads(json_str)
                                if not place_id_found:
                                    place_id_found = found_id
                                if not found_place_name and 'name' in data:
                                    found_place_name = data['name']
                                if save_count is None and 'saveCount' in data:
                                    save_str = str(data['saveCount']).replace(",", "").replace("+", "")
                                    save_match = re.search(r'\d+', save_str)
                                    if save_match:
                                        save_count = int(save_match.group())
                                if visitor_review_count is None and 'visitorReviewCount' in data:
                                    visitor_review_count = int(str(data['visitorReviewCount']).replace(",", ""))
                                if blog_review_count is None and 'blogCafeReviewCount' in data:
                                    blog_review_count = int(str(data['blogCafeReviewCount']).replace(",", ""))
                                print(f"[플레이스 정보] RestaurantListSummary에서 정보 추출 성공 (place_id 일치)", file=sys.stderr, flush=True)
                                break  # 찾았으면 중단
                            except:
                                continue
                else:
                    # place_id가 없으면 첫 번째 결과 사용
                    if summary_matches:
                        match = summary_matches[0]
                        found_id = match.group(1)
                        json_str = match.group(2)
                        try:
                            data = json.loads(json_str)
                            if not place_id_found:
                                place_id_found = found_id
                            if not found_place_name and 'name' in data:
                                found_place_name = data['name']
                            if save_count is None and 'saveCount' in data:
                                save_str = str(data['saveCount']).replace(",", "").replace("+", "")
                                save_match = re.search(r'\d+', save_str)
                                if save_match:
                                    save_count = int(save_match.group())
                            if visitor_review_count is None and 'visitorReviewCount' in data:
                                visitor_review_count = int(str(data['visitorReviewCount']).replace(",", ""))
                            if blog_review_count is None and 'blogCafeReviewCount' in data:
                                blog_review_count = int(str(data['blogCafeReviewCount']).replace(",", ""))
                            print(f"[플레이스 정보] RestaurantListSummary에서 정보 추출 성공", file=sys.stderr, flush=True)
                        except:
                            pass
                
                # 방법 2: "id":"숫자" 패턴으로 JSON 객체 찾기 (네이버저장.py와 동일)
                if not all([place_id_found, save_count is not None, visitor_review_count is not None, blog_review_count is not None]):
                    print(f"[플레이스 정보] 방법 2 시도: place_id={place_id_found}, save_count={save_count}, visitor={visitor_review_count}, blog={blog_review_count}", file=sys.stderr, flush=True)
                    id_pattern = r'"id"\s*:\s*"(\d+)"'
                    id_matches = list(re.finditer(id_pattern, page_source))
                    
                    # place_id가 제공된 경우 정확히 일치하는 것만 찾기
                    target_id = place_id if place_id else None
                    
                    for id_match in id_matches:
                        found_id = id_match.group(1)
                        
                        # place_id가 제공된 경우 정확히 일치하는 것만 처리
                        if target_id and found_id != target_id:
                            continue
                        
                        start_pos = id_match.start()
                        
                        # 앞뒤로 JSON 객체의 시작과 끝 찾기
                        obj_start = page_source.rfind('{', 0, start_pos)
                        if obj_start != -1:
                            brace_count = 0
                            obj_end = start_pos
                            for i in range(obj_start, len(page_source)):
                                if page_source[i] == '{':
                                    brace_count += 1
                                elif page_source[i] == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        obj_end = i + 1
                                        break
                            
                            if obj_end > obj_start:
                                json_str = page_source[obj_start:obj_end]
                                try:
                                    data = json.loads(json_str)
                                    data_name = data.get('name', '')
                                    data_id = str(data.get('id', ''))
                                    
                                    # place_id나 place_name으로 매칭 확인
                                    match_ok = False
                                    if place_id and data_id == place_id:
                                        match_ok = True
                                    elif place_name and place_name in data_name:
                                        match_ok = True
                                    elif not place_id and not place_name:
                                        match_ok = True
                                    
                                    if match_ok:
                                        if not place_id_found:
                                            place_id_found = str(data.get('id', found_id))
                                        if not found_place_name and 'name' in data:
                                            found_place_name = data['name']
                                        if save_count is None and 'saveCount' in data:
                                            save_str = str(data['saveCount']).replace(",", "").replace("+", "")
                                            save_match = re.search(r'\d+', save_str)
                                            if save_match:
                                                save_count = int(save_match.group())
                                        if visitor_review_count is None and 'visitorReviewCount' in data:
                                            visitor_review_count = int(str(data['visitorReviewCount']).replace(",", ""))
                                        if blog_review_count is None and 'blogCafeReviewCount' in data:
                                            blog_review_count = int(str(data['blogCafeReviewCount']).replace(",", ""))
                                        print(f"[플레이스 정보] JSON 객체에서 정보 추출 성공", file=sys.stderr, flush=True)
                                        if place_id:  # place_id로 찾았으면 중단
                                            break
                                except:
                                    continue
                
                # 방법 3: 정규식으로 직접 필드 추출 (네이버저장.py와 동일)
                if not all([place_id_found, save_count is not None, visitor_review_count is not None, blog_review_count is not None]):
                    print(f"[플레이스 정보] 방법 3 시도: place_id={place_id_found}, save_count={save_count}, visitor={visitor_review_count}, blog={blog_review_count}", file=sys.stderr, flush=True)
                    if save_count is None:
                        save_match = re.search(r'"saveCount"\s*:\s*"([\d,]+)\+?"', page_source)
                        if save_match:
                            save_count = int(save_match.group(1).replace(",", ""))
                    
                    if visitor_review_count is None:
                        visitor_match = re.search(r'"visitorReviewCount"\s*:\s*"([\d,]+)"', page_source)
                        if visitor_match:
                            visitor_review_count = int(visitor_match.group(1).replace(",", ""))
                    
                    if blog_review_count is None:
                        blog_match = re.search(r'"blogCafeReviewCount"\s*:\s*"([\d,]+)"', page_source)
                        if blog_match:
                            blog_review_count = int(blog_match.group(1).replace(",", ""))
                    
                    if not place_id_found:
                        id_match = re.search(r'"id"\s*:\s*"(\d+)"', page_source)
                        if id_match:
                            place_id_found = id_match.group(1)
                    
                    if not found_place_name:
                        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', page_source)
                        if name_match:
                            found_place_name = name_match.group(1)
                
                # 브라우저 정리 (기존 브라우저 재사용한 경우 닫지 않음)
                if not use_existing:
                    # 새로 만든 브라우저만 정리
                    try:
                        if page:
                            page.close()
                        if context:
                            context.close()
                        if browser:
                            browser.close()
                        if temp_playwright:
                            temp_playwright.stop()
                    except:
                        pass
                else:
                    # 기존 브라우저 재사용한 경우, 페이지만 닫지 않고 메모리 정리
                    try:
                        # 페이지 상태 초기화 (메모리 정리)
                        if page:
                            page.evaluate("() => { window.location.href = 'about:blank'; }")
                            page.wait_for_timeout(100)
                    except:
                        pass
                
            except Exception as e:
                print(f"[get_place_info] 예외 발생: {type(e).__name__}: {str(e)}", file=sys.stderr, flush=True)
                import traceback
                print(f"[get_place_info] 전체 스택 트레이스:", file=sys.stderr, flush=True)
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                # 브라우저 정리 (기존 브라우저 재사용한 경우 닫지 않음)
                try:
                    if not use_existing:
                        if page:
                            page.close()
                        if context:
                            context.close()
                        if browser:
                            browser.close()
                        if temp_playwright:
                            temp_playwright.stop()
                except Exception as cleanup_error:
                    print(f"[get_place_info] 브라우저 정리 중 오류: {cleanup_error}", file=sys.stderr, flush=True)
                return None
            
            # place_id가 제공되었는데 찾지 못했거나 다른 place_id가 찾아진 경우 확인
            if place_id:
                if not place_id_found:
                    print(f"[플레이스 정보] 경고: 요청한 place_id({place_id})를 찾을 수 없습니다.", file=sys.stderr, flush=True)
                    return None  # place_id가 제공되었는데 찾지 못했으면 None 반환
                elif place_id_found != place_id:
                    print(f"[플레이스 정보] 경고: 요청한 place_id({place_id})와 다른 place_id({place_id_found})가 찾아졌습니다.", file=sys.stderr, flush=True)
                    return None  # 다른 place_id가 찾아졌으면 None 반환
            
            # 결과 반환
            result = {
                "placeId": place_id_found or place_id,  # place_id가 제공되었으면 그것을 사용
                "placeName": found_place_name,
                "saveCount": save_count,
                "visitorReviewCount": visitor_review_count,
                "blogReviewCount": blog_review_count,
                "search_url": search_url
            }
            
            print(f"[플레이스 정보] 최종 결과: placeId={result['placeId']}, placeName={found_place_name}, saveCount={save_count}, visitor={visitor_review_count}, blog={blog_review_count}", file=sys.stderr, flush=True)
            
            # 최소한 하나의 정보라도 있으면 반환
            if any([result['placeId'], found_place_name, save_count is not None, 
                   visitor_review_count is not None, blog_review_count is not None]):
                # 세부정보를 날짜별로 저장
                if result['placeId']:
                    self.save_place_details(result['placeId'], save_count, visitor_review_count, blog_review_count)
                return result
            
            print(f"[플레이스 정보] 정보를 찾을 수 없습니다.", file=sys.stderr, flush=True)
            return None

        except Exception as e:
            print(f"[플레이스 정보 조회 오류] {str(e)}", file=sys.stderr, flush=True)
            import traceback
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            return None
    
    def save_place_details(self, place_id: str, save_count: int | None, visitor_review_count: int | None, blog_review_count: int | None, skip_save: bool = False):
        """
        플레이스 세부정보를 날짜별로 저장
        
        Args:
            place_id: 플레이스 ID
            save_count: 저장 수
            visitor_review_count: 방문자 리뷰 수
            blog_review_count: 블로그 리뷰 수
            skip_save: True면 메모리만 갱신하고 파일 저장은 나중에 수행
        """
        if not place_id:
            return
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        # place_details 구조 초기화
        if "place_details" not in self.data:
            self.data["place_details"] = {}
        
        if place_id not in self.data["place_details"]:
            self.data["place_details"][place_id] = {"details": []}
        
        details_list = self.data["place_details"][place_id]["details"]
        
        # 오늘 날짜의 기존 데이터 찾기
        existing_detail = next((d for d in details_list if d["date"] == today), None)
        
        if existing_detail:
            # 이미 해당 날짜 데이터가 있으면 업데이트
            existing_detail["saveCount"] = save_count
            existing_detail["visitorReviewCount"] = visitor_review_count
            existing_detail["blogReviewCount"] = blog_review_count
            existing_detail["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            # 새로운 데이터 추가
            details_list.append({
                "date": today,
                "saveCount": save_count,
                "visitorReviewCount": visitor_review_count,
                "blogReviewCount": blog_review_count,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            # 날짜순으로 정렬
            details_list.sort(key=lambda x: x["date"])
        
        if not skip_save:
            self.save_data()
    
    def update_keyword_mobile_volumes(self, group_name: str | None = None):
        """
        네이버 검색광고 API를 사용하여 키워드별 모바일 월간 검색수를 갱신.
        - group_name이 지정되면 해당 그룹만, 없으면 모든 그룹 대상
        """
        if "keyword_mobile_volume" not in self.data:
            self.data["keyword_mobile_volume"] = {}
        
        target_groups = []
        if group_name:
            if group_name in self.data["groups"]:
                target_groups = [group_name]
            else:
                return 0
        else:
            target_groups = list(self.data.get("groups", []))
        
        updated_count = 0
        
        for grp in target_groups:
            keywords = self.data.get("keywords", {}).get(grp, [])
            if not keywords:
                continue
            
            if grp not in self.data["keyword_mobile_volume"]:
                self.data["keyword_mobile_volume"][grp] = {}
            
            for kw in keywords:
                try:
                    vol = get_mobile_monthly(kw)
                    self.data["keyword_mobile_volume"][grp][kw] = vol
                    updated_count += 1
                    # 캐시 무효화 (갱신된 키워드의 카드 데이터 캐시 삭제)
                    cache_key = f"card_data:{grp}:{kw}"
                    if cache_key in self._cache:
                        del self._cache[cache_key]
                    # API 과도 호출 방지
                    time.sleep(0.1)
                except Exception as e:
                    print(f"[키워드 모바일 조회수 업데이트 오류] group={grp}, keyword={kw}, error={e}")
        
        if updated_count > 0:
            self.save_data()
        
        return updated_count

    def update_single_keyword_mobile_volume(self, group_name: str, keyword: str) -> int:
        """
        특정 그룹의 단일 키워드에 대해 모바일 월간 검색수를 갱신.
        """
        if not group_name or not keyword:
            raise ValueError("group_name과 keyword가 필요합니다.")

        if group_name not in self.data.get("keywords", {}) or \
           keyword not in self.data["keywords"].get(group_name, []):
            raise ValueError("그룹 또는 키워드를 찾을 수 없습니다.")

        if "keyword_mobile_volume" not in self.data:
            self.data["keyword_mobile_volume"] = {}
        if group_name not in self.data["keyword_mobile_volume"]:
            self.data["keyword_mobile_volume"][group_name] = {}

        vol = get_mobile_monthly(keyword)
        self.data["keyword_mobile_volume"][group_name][keyword] = vol
        self.save_data()
        
        # 캐시 무효화 (갱신된 키워드의 카드 데이터 캐시 삭제)
        cache_key = f"card_data:{group_name}:{keyword}"
        if cache_key in self._cache:
            del self._cache[cache_key]
        
        return vol
    
    def get_place_save_count(self, keyword: str, place_name: str | None = None):
        """
        네이버 지도 검색 페이지에서 특정 키워드로 검색 후,
        프레임 소스 내 JSON에서 saveCount 값을 추출.
        (하위 호환성을 위해 유지, 내부적으로 get_place_info 사용)

        keyword: 카드의 키워드 (예: '전주맛집') - 보조용
        place_name: 플레이스명 (예: '모토이시 분당야탑점') - 실제 검색어로 사용
        """
        try:
            info = self.get_place_info(keyword, place_name)
            if info:
                save_count = info.get("saveCount")
                # 원본 스니펫은 더 이상 제공하지 않음 (get_place_info에서 처리)
                return str(save_count) if save_count is not None else None, None, info.get("search_url")
            return None, None, None
        except Exception as e:
            print(f"[saveCount 조회 오류] {str(e)}")
            return None, None, None


# Flask 앱 초기화
app = Flask(__name__)
# 템플릿 자동 리로드 활성화 (개발 모드)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # 캐시 비활성화
search_app = NaverPlaceRankSearch()


@app.route("/")
def index():
    """메인 페이지"""
    print("[메인 페이지] index.html 렌더링")
    return render_template("index.html")


@app.route("/details", methods=["GET"])
def place_details():
    """플레이스 세부정보 (saveCount, 리뷰 수 등) 페이지"""
    import sys
    import logging
    
    # 강제로 즉시 출력되도록 flush
    sys.stderr.flush()
    sys.stdout.flush()
    
    print("=" * 50, file=sys.stderr, flush=True)
    print("[세부정보 페이지] 라우트 호출됨!", file=sys.stderr, flush=True)
    print("=" * 50, file=sys.stderr, flush=True)
    
    keyword = request.args.get("keyword", "").strip()
    place_name = request.args.get("place_name", "").strip()
    place_id = request.args.get("place_id", "").strip()

    print(f"[세부정보 페이지] 요청 받음: keyword={keyword}, place_name={place_name}, place_id={place_id}", file=sys.stderr, flush=True)

    if not keyword and not place_name:
        return "키워드 또는 플레이스명이 필요합니다.", 400

    # get_place_info를 사용하여 모든 정보 추출
    print(f"[세부정보 페이지] get_place_info 호출 시작...", file=sys.stderr, flush=True)
    place_info = search_app.get_place_info(keyword, place_name or None, place_id or None)
    print(f"[세부정보 페이지] get_place_info 결과: {place_info}", file=sys.stderr, flush=True)

    # 하위 호환성을 위해 기존 방식도 지원
    if not place_info:
        print(f"[세부정보 페이지] place_info가 None이므로 기존 방식 사용", file=sys.stderr, flush=True)
        save_count, raw_snippet, map_url = search_app.get_place_save_count(keyword, place_name or None)
        place_info = {
            "placeId": place_id or None,
            "placeName": place_name or "-",
            "saveCount": save_count,
            "visitorReviewCount": None,
            "blogReviewCount": None,
            "search_url": map_url
        }
        print(f"[세부정보 페이지] 기존 방식 결과: {place_info}", file=sys.stderr, flush=True)

    # 숫자 포맷팅 (천 단위 구분)
    def format_number(num):
        if num is None:
            return None
        try:
            return f"{int(num):,}"
        except:
            return str(num)

    # 순위 데이터 조회 (키워드와 place_id로)
    rankings_data = []
    if keyword and place_info.get("placeId"):
        # 모든 그룹에서 해당 키워드와 place_id로 순위 찾기
        for group_name, group_keywords in search_app.data.get("rankings", {}).items():
            if keyword in group_keywords:
                places = group_keywords[keyword]
                if place_info.get("placeId") in places:
                    place_data = places[place_info.get("placeId")]
                    rankings_data = place_data.get("rankings", [])
                    break
    
    template_data = {
        "keyword": keyword or place_name,
        "place_name": place_info.get("placeName") or place_name or "-",
        "place_id": place_info.get("placeId"),
        "save_count": format_number(place_info.get("saveCount")),
        "visitor_review_count": format_number(place_info.get("visitorReviewCount")),
        "blog_review_count": format_number(place_info.get("blogReviewCount")),
        "search_url": place_info.get("search_url"),
        "has_rankings": len(rankings_data) > 0,
    }
    
    print(f"[세부정보 페이지] 템플릿에 전달할 데이터: {template_data}", file=sys.stderr, flush=True)
    
    # 템플릿 파일 경로 확인 및 디버깅
    import os
    template_path = os.path.join(app.template_folder or 'templates', 'details.html')
    print(f"[세부정보 페이지] 템플릿 파일 경로: {template_path}", file=sys.stderr, flush=True)
    print(f"[세부정보 페이지] 템플릿 파일 존재: {os.path.exists(template_path)}", file=sys.stderr, flush=True)
    if os.path.exists(template_path):
        import time
        mtime = os.path.getmtime(template_path)
        print(f"[세부정보 페이지] 템플릿 파일 수정 시간: {time.ctime(mtime)}", file=sys.stderr, flush=True)
        # 템플릿 캐시 강제 무효화
        if hasattr(app.jinja_env, 'cache'):
            app.jinja_env.cache.clear()
            print(f"[세부정보 페이지] Jinja2 캐시 클리어 완료", file=sys.stderr, flush=True)
    
    # 템플릿 렌더링 (기본 방식 사용 - 더 빠름)
    try:
        result = render_template("details.html", **template_data)
        print(f"[세부정보 페이지] 템플릿 렌더링 성공", file=sys.stderr, flush=True)
        return result
    except Exception as e:
        print(f"[세부정보 페이지] 템플릿 렌더링 오류: {str(e)}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise

@app.route("/api/place_details_history", methods=["GET"])
def api_place_details_history():
    """날짜별 세부정보 조회 API"""
    place_id = request.args.get("place_id", "").strip()
    keyword = request.args.get("keyword", "").strip()
    
    if not place_id:
        return jsonify({"success": False, "error": "place_id가 필요합니다."}), 400
    
    # 날짜별 세부정보 조회
    place_details = search_app.data.get("place_details", {}).get(place_id, {})
    details_list = place_details.get("details", [])
    
    # 순위 데이터 조회 (키워드가 있는 경우) - 최적화
    rankings_data = []
    if keyword:
        rankings_dict = search_app.data.get("rankings", {})
        one_month_ago = datetime.now() - timedelta(days=30)
        one_month_ago_str = one_month_ago.strftime("%Y-%m-%d")
        
        # 빠른 검색을 위해 중첩 루프 최소화
        for group_keywords in rankings_dict.values():
            if keyword in group_keywords:
                places = group_keywords[keyword]
                if place_id in places:
                    place_data = places[place_id]
                    all_rankings = place_data.get("rankings", [])
                    # 최근 1개월 데이터만 필터링 (문자열 비교로 빠르게)
                    rankings_data = [r for r in all_rankings if r.get("date", "") >= one_month_ago_str]
                    break
    
    return jsonify({
        "success": True,
        "place_id": place_id,
        "details": details_list,
        "rankings": rankings_data
    })

@app.route("/api/check_ranks", methods=["POST"])
def api_check_ranks():
    """순위 확인 API"""
    try:
        data = request.json
        place_id = data.get("place_id", "").strip()
        keywords = data.get("keywords", "").strip()
        max_rank = int(data.get("max_rank", "300") or "300")
        group_name = data.get("group_name", "").strip()

        # 새로운 검색이 시작됨을 즉시 상태에 반영 (이전 completed 상태 때문에
        # 프론트에서 완료로 오인하지 않도록 하기 위함)
        search_app.search_status = {
            "status": "searching",
            "message": "검색 시작 중...",
            "progress": 0,
            "total": 0
        }
        
        # 재검색 시 이전 결과 초기화 (중요: 이전 결과가 표시되지 않도록)
        search_app.last_results = []
        
        # 별도 스레드에서 실행 (스레드 안전성을 위해 독립적인 브라우저 생성)
        def run_search():
            try:
                # 스레드 내부에서 독립적인 브라우저 인스턴스 생성
                thread_local_browser = create_thread_local_browser()
                if thread_local_browser:
                    results = search_app.check_ranks(place_id, keywords, max_rank, thread_local_browser, group_name)
                    # 검색 완료 후 스레드 로컬 브라우저 정리
                    cleanup_thread_local_browser(thread_local_browser)
                else:
                    # 브라우저 생성 실패 시 기존 방식 사용
                    results = search_app.check_ranks(place_id, keywords, max_rank, None, group_name)
            except Exception as e:
                print(f"[오류] {str(e)}")
                import traceback
                traceback.print_exc()
                # 오류 발생 시에도 상태 업데이트
                search_app.search_status = {
                    "status": "error",
                    "message": f"오류: {str(e)}",
                    "progress": 0,
                    "total": 0
                }
        
        thread = Thread(target=run_search)
        thread.daemon = True
        thread.start()
        
        return jsonify({"success": True, "message": "검색이 시작되었습니다."})
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


def create_thread_local_browser():
    """스레드 로컬 브라우저 인스턴스 생성"""
    try:
        playwright = sync_playwright().start()
        
        # 기존 Chrome 브라우저에 연결 시도
        try:
            browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                pages = context.pages
                if pages:
                    page = pages[0]
                else:
                    page = context.new_page()
            else:
                context = browser.new_context()
                page = context.new_page()
            
            is_connected_browser = True
            print("[스레드 로컬] 기존 Chrome 브라우저에 연결되었습니다.")
            
        except Exception as e:
            # 연결 실패 시 새 브라우저 실행
            print(f"[스레드 로컬] 기존 Chrome 연결 실패: {e}. 새 브라우저를 실행합니다.")
            headless_mode = os.environ.get('PLAYWRIGHT_HEADLESS', 'False').lower() == 'true'
            launch_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
            if not headless_mode:
                launch_args.extend([
                    '--incognito',
                    '--start-minimized',
                    '--window-position=-2000,0'
                ])
                
            browser = playwright.chromium.launch(
                headless=headless_mode,
                args=launch_args
            )
            context = browser.new_context(
                viewport={"width": 1200, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ko-KR",
                timezone_id="Asia/Seoul"
            )
            page = context.new_page()
            
            # 자동화 감지 스크립트 제거
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            is_connected_browser = False
            print("[스레드 로컬] 새 브라우저가 실행되었습니다.")
        
        return {
            'playwright': playwright,
            'browser': browser,
            'page': page,
            'is_connected_browser': is_connected_browser
        }
        
    except Exception as e:
        print(f"[스레드 로컬 브라우저 생성 실패] {str(e)}")
        return None


def cleanup_thread_local_browser(thread_local_browser):
    """스레드 로컬 브라우저 정리"""
    try:
        if thread_local_browser:
            # 연결된 브라우저는 닫지 않음 (기존 Chrome)
            if not thread_local_browser.get('is_connected_browser', False):
                if thread_local_browser.get('browser'):
                    try:
                        thread_local_browser['browser'].close()
                    except:
                        pass
            
            # Playwright는 정리하지 않음 (재사용 가능)
            # 필요시 정리: thread_local_browser.get('playwright').stop()
    except Exception as e:
        print(f"[스레드 로컬 브라우저 정리 오류] {str(e)}")


@app.route("/api/status", methods=["GET"])
def api_status():
    """검색 상태 조회"""
    return jsonify(search_app.search_status)


@app.route("/api/results", methods=["GET"])
def api_results():
    """검색 결과 조회"""
    return jsonify({"results": search_app.last_results})


@app.route("/api/download_csv", methods=["GET"])
def api_download_csv():
    """CSV 다운로드"""
    group_name = request.args.get('group_name', '').strip()
    
    csv_data = search_app.get_csv_data(group_name if group_name else None)
    if not csv_data:
        return jsonify({"error": "저장할 결과가 없습니다."}), 400
    
    output = io.BytesIO()
    output.write(csv_data.encode('utf-8-sig'))
    output.seek(0)
    
    filename = f'naver_place_rank_{group_name}.csv' if group_name else 'naver_place_rank_results.csv'
    
    return send_file(
        output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=filename
    )


@app.route("/api/clear", methods=["POST"])
def api_clear():
    """결과 초기화"""
    search_app.clear_results()
    return jsonify({"success": True})


@app.route("/api/groups", methods=["GET"])
def api_get_groups():
    """그룹 목록 조회"""
    return jsonify({"groups": search_app.data["groups"]})


@app.route("/api/groups", methods=["POST"])
def api_add_group():
    """그룹 추가"""
    try:
        data = request.json
        group_name = data.get("group_name", "").strip()
        if not group_name:
            return jsonify({"success": False, "error": "그룹명을 입력해주세요."}), 400
        
        if search_app.add_group(group_name):
            return jsonify({"success": True, "message": "그룹이 추가되었습니다."})
        else:
            return jsonify({"success": False, "error": "이미 존재하는 그룹입니다."}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/groups/<group_name>", methods=["DELETE"])
def api_delete_group(group_name):
    """그룹 삭제"""
    try:
        if search_app.delete_group(group_name):
            return jsonify({"success": True, "message": "그룹이 삭제되었습니다."})
        else:
            return jsonify({"success": False, "error": "그룹을 찾을 수 없습니다."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/keywords/<group_name>", methods=["GET"])
def api_get_keywords(group_name):
    """그룹의 키워드 목록 조회"""
    keywords = search_app.data["keywords"].get(group_name, [])
    return jsonify({"keywords": keywords})


@app.route("/api/keywords", methods=["POST"])
def api_add_keyword():
    """그룹에 키워드 추가"""
    try:
        data = request.json
        group_name = data.get("group_name", "").strip()
        keyword = data.get("keyword", "").strip()
        
        if not group_name:
            return jsonify({"success": False, "error": "그룹명을 입력해주세요."}), 400
        if not keyword:
            return jsonify({"success": False, "error": "키워드를 입력해주세요."}), 400
        
        if search_app.add_keyword(group_name, keyword):
            return jsonify({"success": True, "message": "키워드가 추가되었습니다."})
        else:
            return jsonify({"success": False, "error": "이미 존재하는 키워드입니다."}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/keywords", methods=["DELETE"])
def api_delete_keyword():
    """그룹에서 키워드 삭제"""
    try:
        data = request.json
        group_name = data.get("group_name", "").strip()
        keyword = data.get("keyword", "").strip()
        
        if not group_name or not keyword:
            return jsonify({"success": False, "error": "그룹명과 키워드를 입력해주세요."}), 400
        
        if search_app.delete_keyword(group_name, keyword):
            return jsonify({"success": True, "message": "키워드가 삭제되었습니다."})
        else:
            return jsonify({"success": False, "error": "키워드를 찾을 수 없습니다."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/rankings/<group_name>", methods=["GET"])
def api_get_rankings(group_name):
    """그룹의 모든 순위 데이터 조회"""
    try:
        rankings = search_app.get_all_rankings_for_group(group_name)
        return jsonify({"rankings": rankings})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/keyword-card/<group_name>/<keyword>", methods=["GET"])
def api_get_keyword_card(group_name, keyword):
    """특정 키워드의 카드 데이터 조회"""
    try:
        card_data = search_app.get_keyword_card_data(group_name, keyword)
        if card_data:
            return jsonify({"success": True, "data": card_data})
        else:
            return jsonify({"success": False, "error": "데이터를 찾을 수 없습니다."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/update_keyword_volumes", methods=["POST"])
def api_update_keyword_volumes():
    """키워드별 모바일 월간 검색수 갱신 API"""
    try:
        data = request.json or {}
        group_name = (data.get("group_name") or "").strip()
        group_name = group_name or None
        
        updated = search_app.update_keyword_mobile_volumes(group_name)
        
        return jsonify({
            "success": True,
            "updated": updated,
            "group_name": group_name
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/update_keyword_volume_for_card", methods=["POST"])
def api_update_keyword_volume_for_card():
    """단일 카드(그룹+키워드)의 모바일 월간 검색수 갱신 API"""
    try:
        data = request.json or {}
        group_name = (data.get("group_name") or "").strip()
        keyword = (data.get("keyword") or "").strip()

        if not group_name or not keyword:
            return jsonify({"success": False, "error": "group_name과 keyword가 필요합니다."}), 400

        vol = search_app.update_single_keyword_mobile_volume(group_name, keyword)

        return jsonify({
            "success": True,
            "group_name": group_name,
            "keyword": keyword,
            "mobile_monthly_search": vol
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/place_info", methods=["GET"])
def api_place_info():
    """플레이스 정보 조회 API (saveCount, 리뷰 수 등)"""
    try:
        keyword = request.args.get("keyword", "").strip()
        place_name = request.args.get("place_name", "").strip()
        place_id = request.args.get("place_id", "").strip()

        if not keyword and not place_name:
            return jsonify({"success": False, "error": "키워드 또는 플레이스명이 필요합니다."}), 400

        # get_place_info를 사용하여 모든 정보 추출
        place_info = search_app.get_place_info(keyword, place_name or None, place_id or None)

        if place_info:
            return jsonify({
                "success": True,
                "data": place_info
            })
        else:
            return jsonify({
                "success": False,
                "error": "플레이스 정보를 찾을 수 없습니다."
            }), 404

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/import_excel", methods=["POST"])
def api_import_excel():
    """엑셀 파일 불러오기 (다중 파일 지원)"""
    try:
        import tempfile
        import traceback
        
        # 다중 파일 지원: 'files'로 변경 (단일 파일 'file'도 지원)
        files = request.files.getlist('files')
        if not files:
            # 단일 파일도 지원 (하위 호환성)
            single_file = request.files.get('file')
            if single_file and single_file.filename:
                files = [single_file]
        
        if not files or len(files) == 0:
            return jsonify({"success": False, "error": "파일이 없습니다."}), 400
        
        # 빈 파일명 필터링
        files = [f for f in files if f.filename and f.filename.strip()]
        if len(files) == 0:
            return jsonify({"success": False, "error": "파일을 선택해주세요."}), 400
        
        # 전체 결과 집계
        total_imported = 0
        total_skipped = 0
        all_errors = []
        file_results = []
        tmp_paths = []
        
        # 각 파일 처리
        for file_idx, file in enumerate(files):
            if not file.filename.endswith(('.xlsx', '.xls')):
                all_errors.append(f"{file.filename}: 엑셀 파일(.xlsx, .xls)이 아닙니다.")
                continue
            
            tmp_path = None
            try:
                # 파일 확장자에 맞게 임시 파일 생성
                suffix = '.xlsx' if file.filename.endswith('.xlsx') else '.xls'
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                    file.save(tmp_file.name)
                    tmp_path = tmp_file.name
                    tmp_paths.append(tmp_path)
                
                # 엑셀 파일 불러오기
                result = search_app.import_from_excel(tmp_path)
                
                if result.get('success'):
                    imported = result.get('imported', 0)
                    skipped = result.get('skipped', 0)
                    errors = result.get('errors', [])
                    
                    total_imported += imported
                    total_skipped += skipped
                    
                    if errors:
                        # 파일명과 함께 오류 메시지 추가
                        for error in errors:
                            all_errors.append(f"{file.filename}: {error}")
                    
                    # 파일별 결과 저장
                    file_results.append({
                        "filename": file.filename,
                        "imported": imported,
                        "skipped": skipped,
                        "errors_count": len(errors)
                    })
                else:
                    error_msg = result.get('error', '알 수 없는 오류')
                    all_errors.append(f"{file.filename}: {error_msg}")
                    file_results.append({
                        "filename": file.filename,
                        "imported": 0,
                        "skipped": 0,
                        "errors_count": 1
                    })
                    
            except Exception as e:
                error_msg = str(e)
                # 너무 긴 상세 정보는 간단히 표시
                if len(error_msg) > 500:
                    error_msg = error_msg[:500] + "..."
                all_errors.append(f"{file.filename}: {error_msg}")
                file_results.append({
                    "filename": file.filename,
                    "imported": 0,
                    "skipped": 0,
                    "errors_count": 1
                })
            finally:
                # 각 파일 처리 후 임시 파일 삭제 (즉시 정리)
                if tmp_path is not None:
                    try:
                        if os.path.exists(tmp_path):
                            os.unlink(tmp_path)
                    except Exception:
                        pass
        
        # 추가로 남은 임시 파일 정리 (안전장치)
        for tmp_path in tmp_paths:
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
        
        # 결과 반환
        return jsonify({
            "success": True,
            "processed_files": len(files),
            "imported": total_imported,
            "skipped": total_skipped,
            "errors": all_errors,
            "file_results": file_results
        })
        
    except Exception as e:
        return jsonify({"success": False, "error": f"파일 업로드 오류: {str(e)}"}), 400

def run_auto_schedule_search():
    """매일 새벽 2시, 오후 2시 자동 순위 검색 태스크"""
    import sys
    print("=" * 60, file=sys.stderr, flush=True)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 자동 순위 검색 스케줄 태스크 시작", file=sys.stderr, flush=True)
    print("=" * 60, file=sys.stderr, flush=True)
    
    # 최신 데이터 로드
    search_app.load_data()
    
    groups = search_app.data.get("groups", [])
    if not groups:
        print("[스케줄러] 등록된 그룹이 없어 자동 검색을 건너뜁니다.", file=sys.stderr, flush=True)
        return
        
    for group_name in groups:
        keywords = search_app.data.get("keywords", {}).get(group_name, [])
        rankings = search_app.data.get("rankings", {}).get(group_name, {})
        
        if not keywords or not rankings:
            continue
            
        for keyword in keywords:
            keyword_places = rankings.get(keyword, {})
            if not keyword_places:
                continue
            
            for place_id, place_data in keyword_places.items():
                place_name = place_data.get("place_name", "")
                print(f"[스케줄러] 그룹: {group_name} | 키워드: {keyword} | 매장명: {place_name} (placeId: {place_id}) 자동 순위 검색 시작", file=sys.stderr, flush=True)
                
                search_app.search_status = {
                    "status": "searching",
                    "message": f"[자동검색] {group_name} - {keyword} 진행 중",
                    "progress": 0,
                    "total": 1
                }
                
                # 스레드 로컬 브라우저 생성 후 검색 실행
                thread_local_browser = create_thread_local_browser()
                try:
                    if thread_local_browser:
                        search_app.check_ranks(place_id, keyword, 300, thread_local_browser, group_name)
                        cleanup_thread_local_browser(thread_local_browser)
                    else:
                        search_app.check_ranks(place_id, keyword, 300, None, group_name)
                except Exception as e:
                    print(f"[스케줄러 오류] {group_name} - {keyword} (place_id: {place_id}) 검색 실패: {e}", file=sys.stderr, flush=True)
                
                # 네이버 요청 과밀 차단 방지를 위한 검색 간 텀 설정
                time.sleep(2.0)
                
    search_app.search_status = {
        "status": "ready",
        "message": f"자동 스케줄 완료 (최근 구동: {datetime.now().strftime('%m-%d %H:%M')})",
        "progress": 0,
        "total": 0
    }
    print("=" * 60, file=sys.stderr, flush=True)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 자동 순위 검색 스케줄 태스크 완료", file=sys.stderr, flush=True)
    print("=" * 60, file=sys.stderr, flush=True)


def main():
    """웹 서버 실행"""
    import sys
    import logging
    import os
    
    # 로깅 설정 - 모든 로그를 콘솔에 출력
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stderr)
        ]
    )
    
    # Flask 로거도 활성화
    app.logger.setLevel(logging.DEBUG)
    
    # Railway, Render 등 클라우드 플랫폼의 PORT 환경변수 사용
    port = int(os.environ.get('PORT', 5000))
    
    print("=" * 50, file=sys.stderr, flush=True)
    print("네이버 플레이스 순위 검색 웹앱", file=sys.stderr, flush=True)
    print("=" * 50, file=sys.stderr, flush=True)
    print(f"웹 브라우저에서 http://127.0.0.1:{port} 으로 접속하세요.", file=sys.stderr, flush=True)
    print("=" * 50, file=sys.stderr, flush=True)
    
    # 템플릿 자동 리로드 강제 활성화
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    # Jinja2 캐시 완전 비활성화
    app.jinja_env.cache_size = 0
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    
    print("템플릿 자동 리로드: 활성화", file=sys.stderr, flush=True)
    print("Jinja2 캐시: 비활성화 (cache_size=0)", file=sys.stderr, flush=True)
    
    # 백그라운드 자동 검색 스케줄러 등록 및 가동
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_auto_schedule_search, 'cron', hour=2, minute=0, id='auto_search_0200')
    scheduler.add_job(run_auto_schedule_search, 'cron', hour=14, minute=0, id='auto_search_1400')
    scheduler.start()
    print("[스케줄러] 백그라운드 자동 검색 스케줄러 가동 성공! (02:00, 14:00 실행)", file=sys.stderr, flush=True)
    
    # 클라우드 환경에서는 debug=False (gunicorn 사용 시)
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # debug=True로 변경하여 자동 리로드 및 상세 로그 활성화
    # 템플릿 파일 변경 감지
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=False, extra_files=['templates/details.html', 'templates/index.html'])


if __name__ == "__main__":
    main()
