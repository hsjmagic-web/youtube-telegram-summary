import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta
import zoneinfo
import holidays
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
import requests

CHANNEL_ID = "UCYeL__ckkLfpnZzM7iBVktA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# ==========================================
# [설정] 수집 대상 시간대 (한국 시간 KST 기준)
# 기본값: 당일 00:00 ~ 당일 08:00
# 추후 전일 22:00부터 수집하고 싶으실 때는 아래 START_DAYS_AGO = 1, START_HOUR = 22 로 변경하시면 됩니다.
# ==========================================
START_DAYS_AGO = 0   # 0이면 당일, 1이면 전일
START_HOUR = 0       # 시작 시 (00시)
END_HOUR = 8         # 종료 시 (08시)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")

def is_korean_trading_day():
    kst = zoneinfo.ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()

    if today.weekday() >= 5:
        print(f"[{today}] 주말이므로 작업을 종료합니다.")
        return False

    kr_holidays = holidays.SouthKorea(years=today.year)
    if today in kr_holidays:
        print(f"[{today}] 공휴일({kr_holidays.get(today)})이므로 작업을 종료합니다.")
        return False

    return True

def summarize_with_gemini(client, prompt):
    models_to_try = ['gemini-1.5-flash', 'gemini-1.5-pro']
    
    for model_name in models_to_try:
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                print(f"[{model_name}] 시도 {attempt} 실패: {e}")
                time.sleep(attempt * 3)
                
    return "요약 생성 실패: 제미나이 서버 응답 지연"

def main():
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not is_korean_trading_day():
        return

    kst = zoneinfo.ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)

    # 수집 기준 시간 계산 (KST 기준)
    # 시작 시간: 당일 00:00:00 (START_DAYS_AGO 설정 반영)
    start_time = (now_kst - timedelta(days=START_DAYS_AGO)).replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
    # 종료 시간: 당일 08:00:00
    end_time = now_kst.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)

    # 수동 테스트 시 안전장치: 현재 시각이 8시 이전이면 현재 시각까지 검사
    if is_manual and end_time < start_time:
        end_time = now_kst

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("채널에서 영상을 찾을 수 없습니다.")
        return

    # 대상 시간대 영상 필터링
    target_videos = []
    for entry in feed.entries:
        # 유튜브 RSS의 published_dt는 UTC 기준이므로 KST로 변환
        published_utc = datetime.fromisoformat(entry.published)
        published_kst = published_utc.astimezone(kst)

        if start_time <= published_kst <= end_time or is_manual:
            video_id = entry.yt_videoid
            video_title = entry.title
            video_url = entry.link
            video_desc = entry.get('summary', '')

            # 자막 추출
            transcript_text = ""
            try:
                t_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ko', 'en'])
                transcript_text = " ".join([item['text'] for item in t_list])
            except Exception:
                transcript_text = video_desc

            target_videos.append({
                "id": video_id,
                "title": video_title,
                "url": video_url,
                "time": published_kst,
                "content": transcript_text
            })

    if not target_videos:
        print(f"지정된 시간대({start_time.strftime('%H:%M')} ~ {end_time.strftime('%H:%M')})에 업로드된 영상이 없습니다.")
        return

    # 영상을 시간 순(오래된 순 -> 최신 순)으로 정렬 (최신 정보 덮어쓰기 보장 목적)
    target_videos.sort(key=lambda x: x["time"])

    # 제미나이에 넘길 통합 텍스트 구성
    combined_script = ""
    for idx, v in enumerate(target_videos, 1):
        combined_script += f"\n--- [영상 {idx}] ---\n"
        combined_script += f"제목: {v['title']}\n"
        combined_script += f"업로드시간: {v['time'].strftime('%Y-%m-%d %H:%M')}\n"
        combined_script += f"내용(자막): {v['content'][:4000]}\n"

    # 정밀 타깃 추출 프롬프트
    prompt = f"""
당신은 주식 매매 추천 정보를 정확히 구조화하는 전문 비서입니다.
아래에 수집된 {len(target_videos)}개의 유튜브 영상 정보를 분석하여 다음 규칙에 따라 정리해 주세요.

[정리 규칙]
1. 작성자(분석가/전문가)별로 그룹화하세요.
   - 작성자는 영상 제목 끝이나 자막 인사말(예: "광수", "마스터" 등)에서 식별하세요. 파악이 어려우면 "기타/공통"으로 표기하세요.
2. 각 작성자가 추천하거나 핵심 분석한 종목들을 추출하세요.
   - 반드시 추출할 항목:
     • 종목명
     • 매수 추천 금액 (또는 매수 유효 구간)
     • 손절 기준 금액
     • 매도 기준 및 비율 (예: 1차 목표가 및 비중, 추세 매도선 등)
3. 중복 종목 최신화 규칙 (매우 중요):
   - 같은 작성자가 여러 영상에서 동일한 종목을 언급한 경우, 시간순으로 가장 마지막(최신) 영상에서 제시한 매수가, 손절가, 매도 기준을 채택해 덮어쓰기하세요.
4. 만약 영상에서 특정 가격(매수가/손절가 등)을 구체적으로 언급하지 않았다면 임의로 지어내지 말고 "언급 없음"으로 표기하세요.
5. 텔레그램으로 전송될 메시지이므로 가독성 좋게 글머리 기호(•, 📌)를 사용하여 깔끔한 텍스트로 출력하세요.

[수집된 영상 데이터]
{combined_script}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    summary_result = summarize_with_gemini(client, prompt)

    # 텔레그램 최종 메시지 구성
    report_date = now_kst.strftime('%Y-%m-%d')
    final_message = f"📢 [당일 추천 종목 종합 브리핑]\n📅 기준일: {report_date} (00:00~08:00 업로드분)\n\n{summary_result}"

    # 텔레그램 메시지 길이 제한(4096자) 대응: 길 경우 분할 전송
    if len(final_message) > 4000:
        for chunk in [final_message[i:i+3900] for i in range(0, len(final_message), 3900)]:
            send_telegram(chunk)
            time.sleep(1)
    else:
        send_telegram(final_message)

    print(f"총 {len(target_videos)}개 영상 취합 및 텔레그램 발송 완료.")

if __name__ == "__main__":
    main()
