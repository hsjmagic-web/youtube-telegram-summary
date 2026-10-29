import os
import sys
import time
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

START_DAYS_AGO = 0
START_HOUR = 0
END_HOUR = 8

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

def clean_and_shrink_text(text, max_chars=1800):
    lines = text.split()
    joined = " ".join(lines)
    return joined[:max_chars]

def summarize_with_gemini(client, prompt):
    # 구글 안내 기준 신규 키 지원 모델 지정
    model_name = 'gemini-3.8-flash'
    last_error = ""
    
    for attempt in range(1, 4):
        try:
            print(f"[{model_name}] 요약 요청 시도 ({attempt}/3)...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            last_error = str(e)
            print(f"시도 {attempt} 실패 상세: {last_error}")
            time.sleep(attempt * 5)
                
    return f"요약 생성 실패. 상세 사유: {last_error[:300]}"

def main():
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not is_korean_trading_day():
        return

    kst = zoneinfo.ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)

    start_time = (now_kst - timedelta(days=START_DAYS_AGO)).replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
    end_time = now_kst.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)

    if is_manual and end_time < start_time:
        end_time = now_kst

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("채널에서 영상을 찾을 수 없습니다.")
        return

    # 수동 테스트 시 최근 영상 최대 2개만 집중 추출
    entries = feed.entries[:2] if is_manual else feed.entries

    target_videos = []
    for entry in entries:
        published_utc = datetime.fromisoformat(entry.published)
        published_kst = published_utc.astimezone(kst)

        if is_manual or (start_time <= published_kst <= end_time):
            video_id = entry.yt_videoid
            video_title = entry.title
            video_desc = entry.get('summary', '')

            transcript_text = ""
            try:
                t_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ko', 'en'])
                raw_text = " ".join([item['text'] for item in t_list])
                transcript_text = clean_and_shrink_text(raw_text, max_chars=1800)
            except Exception:
                transcript_text = clean_and_shrink_text(video_desc, max_chars=800)

            target_videos.append({
                "id": video_id,
                "title": video_title,
                "time": published_kst,
                "content": transcript_text
            })

    if not target_videos:
        print("수집 대상 영상이 없습니다.")
        return

    target_videos.sort(key=lambda x: x["time"])

    combined_script = ""
    for idx, v in enumerate(target_videos, 1):
        combined_script += f"\n[영상 {idx}]\n"
        combined_script += f"제목: {v['title']}\n"
        combined_script += f"업로드: {v['time'].strftime('%Y-%m-%d %H:%M')}\n"
        combined_script += f"내용(요약 자막): {v['content']}\n"

    prompt = f"""
당신은 주식 매매 추천 데이터를 추출하는 비서입니다.
아래 {len(target_videos)}개의 유튜브 영상 자막을 분석하여 다음 양식대로 정리하세요.

[규칙]
1. 작성자(예: 광수 등)별로 그룹화하세요. (파악 불가 시 '공통/기타')
2. 각 작성자가 언급한 종목별로 다음 4가지 정보를 추출하세요:
   - 종목명
   - 매수 추천 금액 (또는 매수 구간)
   - 손절 기준 금액
   - 매도 기준 및 비율
3. 중복 종목 최신화:
   - 같은 작성자가 여러 영상에서 동일 종목을 언급했다면, 시간상 가장 최신 영상의 가격/기준으로 작성하세요.
4. 구체적인 금액이나 기준이 언급되지 않았다면 지어내지 말고 '언급 없음'으로 표기하세요.
5. 텔레그램 메시지이므로 보기 쉽게 깔끔한 글머리 기호(📌, •)로 작성하세요.

[데이터]
{combined_script}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    summary_result = summarize_with_gemini(client, prompt)

    report_date = now_kst.strftime('%Y-%m-%d')
    final_message = f"📢 [추천 종목 브리핑]\n📅 기준: {report_date}\n\n{summary_result}"

    if len(final_message) > 4000:
        for chunk in [final_message[i:i+3900] for i in range(0, len(final_message), 3900)]:
            send_telegram(chunk)
            time.sleep(1)
    else:
        send_telegram(final_message)

    print(f"총 {len(target_videos)}개 영상 분석 및 전송 완료.")

if __name__ == "__main__":
    main()
