import os
import sys
import time
from datetime import datetime, timezone, timedelta
import feedparser
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
import requests

CHANNEL_ID = "UCYeL__ckkLfpnZzM7iBVktA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False
    }
    requests.post(url, json=payload)

def summarize_with_gemini(client, prompt):
    # 503 과부하 에러 방지를 위한 2단계 모델 예비책
    models_to_try = ['gemini-2.0-flash', 'gemini-1.5-flash']
    
    for model_name in models_to_try:
        # 최대 3회 재시도
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response.text:
                    return response.text
            except Exception as e:
                print(f"[{model_name}] 시도 {attempt + 1} 실패: {e}")
                time.sleep(3)  # 과부하 해소를 위해 3초 대기 후 재시도
                
    return "서버 사용량 폭주로 인해 일시적으로 요약을 생성하지 못했습니다. 링크를 통해 확인해 주세요."

def main():
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("영상을 찾을 수 없습니다.")
        return

    now = datetime.now(timezone.utc)
    # 최근 24시간 동안 업로드된 영상 확인
    time_threshold = now - timedelta(hours=24)

    client = genai.Client(api_key=GEMINI_API_KEY)
    processed_count = 0

    for entry in feed.entries:
        published_dt = datetime.fromisoformat(entry.published)
        
        if published_dt >= time_threshold:
            video_id = entry.yt_videoid
            video_title = entry.title
            video_url = entry.link
            
            # 자막 가져오기
            transcript_text = ""
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['ko', 'en'])
                transcript_text = " ".join([item['text'] for item in transcript_list])
            except Exception:
                # 자막이 없는 경우 영상 설명란 사용
                transcript_text = entry.get('summary', '자막 없음')

            # 제미나이 요약 요청
            prompt = f"""
다음은 유튜브 영상 '{video_title}'의 내용(자막)입니다.
영상의 핵심 내용과 중요한 포인트를 한국어로 읽기 쉽게 3~5줄 내외의 글머리 기호(- )로 요약해 주세요.

[내용]
{transcript_text[:7000]}
"""
            summary = summarize_with_gemini(client, prompt)

            # 텔레그램 메시지 구성 및 전송
            message = f"📢 [새 영상 알림 및 핵심 요약]\n\n🎬 제목: {video_title}\n🔗 링크: {video_url}\n\n📝 요약:\n{summary}"
            send_telegram(message)
            processed_count += 1

    print(f"총 {processed_count}개의 새로운 영상을 처리했습니다.")

if __name__ == "__main__":
    main()
