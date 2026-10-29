import os
import sys
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

            # 제미나이 요약 요청 (최신 모델 gemini-3.8-flash 적용)
            prompt = f"""
다음은 유튜브 영상 '{video_title}'의 자막/내용입니다.
영상의 핵심 내용과 중요한 포인트를 한국어로 읽기 쉽게 3~5줄 내외의 글머리 기호로 요약해 주세요.

[내용]
{transcript_text[:7000]}
"""
            try:
                response = client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=prompt
                )
                summary = response.text
            except Exception as e:
                summary = f"요약 생성 중 오류 발생: {e}"

            # 텔레그램 메시지 구성 및 전송
            message = f"📢 [새 영상 알림 및 핵심 요약]\n\n🎬 제목: {video_title}\n🔗 링크: {video_url}\n\n📝 요약:\n{summary}"
            send_telegram(message)
            processed_count += 1

    print(f"총 {processed_count}개의 새로운 영상을 요약하여 전송했습니다.")

if __name__ == "__main__":
    main()
