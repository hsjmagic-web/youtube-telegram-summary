import os
import re
import json
import time
from datetime import datetime, timedelta
import zoneinfo

import holidays
import feedparser
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from google.genai import types

# ==========================================
# [기본 설정]
# ==========================================
CHANNEL_ID = "UCYeL__ckkLfpnZzM7iBVktA"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

PREFERRED_MODEL = "gemini-3.8-flash"

# 수집 시간대 (한국 시간 기준)
START_DAYS_AGO = 0
START_HOUR = 0
END_HOUR = 8

# 수동 실행 시 테스트할 영상 개수
MANUAL_TEST_COUNT = 2

# 자막을 제미나이에게 넘길 최대 글자 수
MAX_SCRIPT_CHARS = 12000

KST = zoneinfo.ZoneInfo("Asia/Seoul")


# ==========================================
# 텔레그램 전송
# ==========================================
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True
                },
                timeout=15,
            )
            if r.status_code != 200:
                print(f"텔레그램 전송 실패: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"텔레그램 전송 오류: {e}")
        time.sleep(1)


# ==========================================
# 평일/공휴일 확인
# ==========================================
def is_korean_trading_day():
    today = datetime.now(KST).date()
    if today.weekday() >= 5:
        print(f"[{today}] 주말이므로 작업을 건너뜁니다.")
        return False
    kr_holidays = holidays.SouthKorea(years=today.year)
    if today in kr_holidays:
        print(f"[{today}] 공휴일({kr_holidays.get(today)})이므로 작업을 건너뜁니다.")
        return False
    return True


# ==========================================
# 사용할 모델 자동 선택
# ==========================================
def pick_model(client):
    try:
        names = []
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                names.append(m.name.replace("models/", ""))
    except Exception as e:
        print(f"모델 목록 조회 실패({e}) -> 기본 모델 사용")
        return PREFERRED_MODEL

    if PREFERRED_MODEL in names:
        return PREFERRED_MODEL

    exclude = ("lite", "image", "tts", "live", "audio", "embedding", "preview", "exp")
    cands = [n for n in names if n.startswith("gemini-") and "flash" in n and not any(x in n for x in exclude)]
    if not cands:
        cands = [n for n in names if "flash" in n]

    def version(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0

    cands.sort(key=version, reverse=True)
    print(f"사용 가능한 flash 모델: {cands}")
    return cands[0] if cands else PREFERRED_MODEL


# ==========================================
# 개별 영상 분석
# ==========================================
EXTRACT_PROMPT = """당신은 주식 유튜브 영상에서 매매 정보를 정확히 뽑아내는 전문 비서입니다.
영상 제목: {title}

{script_block}

아래 JSON 형식으로만 답하세요. 마크다운 코드블록이나 다른 설명은 절대 쓰지 마세요.
{{
  "author": "영상 진행자(분석가) 이름. 제목 끝이나 인사말에서 찾고, 모르면 '기타'",
  "stocks": [
    {{
      "name": "종목명",
      "buy": "매수 추천 금액 또는 매수 구간",
      "stop": "손절 기준 금액",
      "sell": "매도 기준 및 비율"
    }}
  ]
}}

규칙:
- 영상에서 구체적으로 추천하거나 매매 기준을 제시한 종목만 넣으세요.
- 금액이나 기준을 말하지 않았으면 절대 지어내지 말고 "언급 없음"이라고 쓰세요.
- 추천 종목이 하나도 없으면 "stocks": [] 로 두세요.
"""


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def call_gemini(client, model, contents, use_video=False):
    last_error = ""
    if use_video:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        )
    else:
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        )
    for attempt in range(1, 4):
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            if resp and resp.text:
                return parse_json(resp.text), ""
            last_error = "빈 응답"
        except Exception as e:
            last_error = str(e)
            print(f"  시도 {attempt}/3 실패: {last_error[:200]}")
        if attempt < 3:
            time.sleep(10 * attempt)  # 10초, 20초 대기
    return None, last_error


def get_video_transcript(video_id):
    """최신 youtube-transcript-api 방식. 실패하면 None과 실패 사유를 돌려줌."""
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=['ko', 'en'])
        raw_text = " ".join(s.text for s in fetched)
        if raw_text.strip():
            return raw_text[:MAX_SCRIPT_CHARS], ""
        return None, "자막 내용 없음"
    except Exception as e:
        return None, f"{type(e).__name__}"


def analyze_video(client, model, video):
    print(f"[분석 시작] {video['title'][:40]}")

    # 1단계: 자막으로 분석
    script, t_err = get_video_transcript(video["id"])
    if script:
        print("  자막 확보 성공")
        prompt = EXTRACT_PROMPT.format(
            title=video["title"], script_block=f"[영상 대본]\n{script}")
        data, err = call_gemini(client, model, prompt)
        if data is not None:
            return {"video": video, "data": data, "error": ""}
    else:
        print(f"  자막 가져오기 실패({t_err}) -> 영상 직접 분석으로 전환")

    # 2단계: 영상 주소를 제미나이에게 직접 전달
    prompt = EXTRACT_PROMPT.format(
        title=video["title"], script_block="[첨부된 영상을 직접 보고 분석하세요]")
    contents = types.Content(parts=[
        types.Part(file_data=types.FileData(file_uri=video["url"])),
        types.Part(text=prompt),
    ])
    data, err = call_gemini(client, model, contents, use_video=True)
    if data is not None:
        data["note"] = "영상 직접 분석"
        return {"video": video, "data": data, "error": ""}
    print("  영상 직접 분석 실패 -> 설명란으로 분석")

    # 3단계: 설명란으로 분석
    desc = video["desc"].replace("\n", " ")[:1500]
    prompt = EXTRACT_PROMPT.format(
        title=video["title"], script_block=f"[영상 설명란]\n{desc}")
    data, err2 = call_gemini(client, model, prompt)
    if data is not None:
        data["note"] = "설명란 기반(자막·영상 분석 실패)"
        return {"video": video, "data": data, "error": ""}

    return {"video": video, "data": None, "error": err2 or err}


# ==========================================
# 결과를 작성자별로 합치기 (최신 종목 덮어쓰기)
# ==========================================
def build_report(results, report_date, model):
    results.sort(key=lambda r: r["video"]["time"])  # 오래된 순 -> 최신 순
    authors = {}
    failures = []

    for r in results:
        if r["data"] is None:
            failures.append(f"• {r['video']['title'][:40]}\n  사유: {r['error'][:150]}")
            continue

        author = (r["data"].get("author") or "기타").strip()
        bucket = authors.setdefault(author, {})

        for s in r["data"].get("stocks", []) or []:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            # 같은 작성자가 동일 종목을 재언급하면 최신 영상 내용으로 덮어씀
            bucket[name] = {
                **s,
                "time": r["video"]["time"].strftime("%H:%M"),
                "note": r["data"].get("note", "")
            }

    lines = [f"📢 [추천 종목 종합 브리핑]", f"📅 기준일: {report_date}", ""]
    if not any(authors.values()):
        lines.append("분석한 영상에서 구체적인 추천 종목이 나오지 않았습니다.")

    for author, stocks in authors.items():
        if not stocks:
            continue
        lines += ["====================", f"👤 분석가: {author}", "===================="]
        for name, s in stocks.items():
            lines.append(f"📌 {name} (영상 {s['time']})")
            lines.append(f"• 매수 추천가: {s.get('buy') or '언급 없음'}")
            lines.append(f"• 손절 기준가: {s.get('stop') or '언급 없음'}")
            lines.append(f"• 매도 기준·비율: {s.get('sell') or '언급 없음'}")
            if s.get("note"):
                lines.append(f"• 참고: {s['note']}")
            lines.append("")

    if failures:
        lines += ["⚠️ 분석 실패 영상", *failures]
    lines.append(f"\n(분석 영상 {len(results)}개 · 모델: {model})")
    return "\n".join(lines)


# ==========================================
# 메인 루프
# ==========================================
def main():
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not is_korean_trading_day():
        return

    now_kst = datetime.now(KST)
    start_time = (now_kst - timedelta(days=START_DAYS_AGO)).replace(
        hour=START_HOUR, minute=0, second=0, microsecond=0)
    end_time = now_kst.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("채널에서 영상을 찾을 수 없습니다.")
        return

    entries = feed.entries[:MANUAL_TEST_COUNT] if is_manual else feed.entries
    videos = []
    for e in entries:
        published_kst = datetime.fromisoformat(e.published).astimezone(KST)
        if is_manual or (start_time <= published_kst <= end_time):
            videos.append({
                "id": e.yt_videoid,
                "title": e.title,
                "url": f"https://www.youtube.com/watch?v={e.yt_videoid}",
                "time": published_kst,
                "desc": e.get("summary", ""),
            })

    if not videos:
        print(f"{start_time:%m-%d %H:%M} ~ {end_time:%H:%M} 사이 업로드된 영상이 없습니다.")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    model = pick_model(client)
    print(f"선택된 모델: {model} / 분석 대상 {len(videos)}개")

    # 무료 키 429 한도 초과 방지를 위해 순차 호출 + 2초 텀
    results = []
    for v in videos:
        res = analyze_video(client, model, v)
        results.append(res)
        time.sleep(2)

    report = build_report(results, now_kst.strftime("%Y-%m-%d"), model)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
