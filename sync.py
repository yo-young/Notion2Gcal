import os
import sys
import json
import time
from datetime import datetime, timedelta
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# 1. 환경 변수 로드
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DB_ID = os.environ.get("NOTION_DB_ID")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID")
# GitHub Secrets에 저장된 Service Account JSON 문자열을 파싱합니다.
GOOGLE_SERVICE_ACCOUNT_STR = os.environ.get("GOOGLE_SERVICE_ACCOUNT")

if not all([NOTION_TOKEN, NOTION_DB_ID, GOOGLE_CALENDAR_ID, GOOGLE_SERVICE_ACCOUNT_STR]):
    print("❌ Error: 필수 환경 변수가 누락되었습니다.")
    sys.exit(1)

# 2. 구글 캘린더 API 클라이언트 설정
SCOPES = ['https://www.googleapis.com/auth/calendar']
try:
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_STR)
    creds = service_account.Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    calendar_service = build('calendar', 'v3', credentials=creds)
except Exception as e:
    print(f"❌ 구글 인증 실패: {e}")
    sys.exit(1)

# 노션 API 헤더 설정
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def get_recent_notion_pages():
    """최근 15분 내에 수정된 노션 페이지 목록을 가져옵니다."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    
    # 15분 전 시간 계산 (ISO 8601 형식)
    time_limit = (datetime.utcnow() - timedelta(minutes=16)).isoformat() + "Z"
    
    # Filter: Last Edited Time이 15분 전 이후인 것만 필터링
    payload = {
        "filter": {
            "timestamp": "last_edited_time",
            "last_edited_time": {
                "after": time_limit
            }
        }
    }
    
    response = requests.post(url, json=payload, headers=NOTION_HEADERS)
    if response.status_code != 200:
        print(f"❌ 노션 DB 조회 실패: {response.text}")
        return []
    
    return response.json().get("results", [])

def update_notion_page_google_id(page_id, google_event_id):
    """노션 페이지의 Google_Event_ID 속성을 업데이트합니다."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {
        "properties": {
            "Google_Event_ID": {
                "rich_text": [
                    {
                        "text": {
                            "content": google_event_id
                        }
                    }
                ]
            }
        }
    }
    requests.patch(url, json=payload, headers=NOTION_HEADERS)

def sync_to_google_calendar():
    pages = get_recent_notion_pages()
    print(f"🔄 동기화 대상 노션 항목 개수: {len(pages)}개")
    
    for page in pages:
        properties = page.get("properties", {})
        
        # 1. 노션 데이터 파싱 (속성 이름은 본인의 DB 구조에 맞춰야 합니다)
        # 제목(Name) 파ing
        title_obj = properties.get("이름", {}).get("title", [])
        title = title_obj[0].get("text", {}).get("content", "제목 없음") if title_obj else "제목 없음"
        
        # 날짜(Date) 파싱
        date_obj = properties.get("날짜", {}).get("date", {})
        if not date_obj:
            continue  # 날짜가 없는 일정은 싱크 건너뜀
            
        start_date = date_obj.get("start")
        end_date = date_obj.get("end") or start_date  # 종료일이 없으면 시작일과 동일하게 설정
        
        # Google_Event_ID 확인
        gcal_id_obj = properties.get("Google_Event_ID", {}).get("rich_text", [])
        google_event_id = gcal_id_obj[0].get("text", {}).get("content", "").strip() if gcal_id_obj else ""
        
        # 구글 캘린더 이벤트 포맷 생성
        # 시간 포함 여부에 따라 date 또는 dateTime으로 매핑
        time_key = "dateTime" if "T" in start_date else "date"
        
        event_body = {
            'summary': title,
            'start': {time_key: start_date},
            'end': {time_key: end_date},
        }
        
        # 타임존 유효성 보완 (시간 정보가 있고 타임존 정보가 없을 시 기본 KST 설정)
        if time_key == "dateTime" and "+" not in start_date and "Z" not in start_date.upper():
            event_body['start']['timeZone'] = 'Asia/Seoul'
            event_body['end']['timeZone'] = 'Asia/Seoul'

        try:
            if not google_event_id:
                # [CASE 1] 새로 생성된 일정 -> 구글에 Insert
                print(f"➕ 새 일정 추가 중: {title}")
                event = calendar_service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
                new_google_id = event.get('id')
                
                # 노션에 발급받은 Google_Event_ID 기록
                update_notion_page_google_id(page.get("id"), new_google_id)
                print(f"✅ 구글 일정 매핑 성공 (ID: {new_google_id})")
            else:
                # [CASE 2] 기존에 존재하던 일정 -> 구글 업데이트(Update)
                print(f"✏️ 기존 일정 수정 중: {title} (ID: {google_event_id})")
                calendar_service.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=google_event_id, body=event_body).execute()
                print("✅ 구글 일정 수정 완료")
                
        except Exception as e:
            print(f"❌ 일정 처리 중 에러 발생 ({title}): {e}")
            
        # 노션 API Rate Limit(초당 3회) 안 걸리도록 안전하게 딜레이 부여
        time.sleep(0.5)

if __name__ == "__main__":
    print(f"🚀 동기화 스크립트 시작 시간: {datetime.now().isoformat()}")
    sync_to_google_calendar()
    print("🏁 동기화 프로세스 종료")
