# Dawn Prayer Bot

> 유튜브 '새벽예배' 재생목록의 최신 영상을 자동으로 가져와 한국어 자막을 추출하고,
> Gemini API로 큐티(QT) 형식(제목/말씀/해설/적용/기도)으로 정리한 뒤 텔레그램으로 발송하는 봇.

## 동작 흐름

1. GitHub Actions가 매일 **KST 07:00~09:00 사이** (UTC 22:00, 실제 지연 있음) 에 워크플로우 실행
2. 재생목록에서 최신 1~3개 영상의 한국어 자동 자막 추출 (3-tier fallback):
   1) `youtube-transcript-api` (GitHub Actions에서도 안정적인 경우가 많음)
   2) `yt-dlp` — 다양한 player client (mediaconnect, tv_embedded, ios_creator, android) 순차 시도
   3) Invidious 공개 인스턴스 체인 (third-party 프록시, 마지막 폴백)
3. `google-genai` (`gemma-4-26b-a4b-it` 우선, `gemini-2.5-flash` 폴백) 로 큐티 형식 정리
4. `python-telegram-bot` 으로 텔레그램 메시지 발송 (Markdown 파싱 실패 시 plain text 자동 폴백)
5. 결과를 `outputs/YYYY-MM-DD_<videoId>.md` 로 저장 후 git push
6. 이미 처리된 video_id 는 자동 스킵 (outputs/ 스캔 기반)

## 디렉토리 구조

```
dawn-prayer-bot/
├── main.py                       # 메인 로직 (Pydantic 기반, 3-tier 자막 추출)
├── requirements.txt              # 의존성
├── .env.example                  # 환경변수 템플릿
├── .github/workflows/
│   └── daily_dawn.yml            # 매일 UTC 22:00 실행 (KST 07:00)
└── outputs/                      # 큐티 결과 (git 커밋됨)
```

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env  # 실제 토큰/키 입력
python main.py           # 실제 텔레그램 발송
python main.py --dry-run # 큐티 생성 + 파일 저장만 (텔레그램 생략)
```

## GitHub Secrets 설정

레포지토리 Settings → Secrets and variables → Actions 에 아래 항목 등록:

| Secret 이름 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 텔레그램 봇 토큰 (tsc-bot과 동일 값 사용 가능) |
| `GEMINI_API_KEY` | ✅ | Google Gemini API 키 (tsc-bot과 동일 값 사용 가능) |
| `CHAT_ID` | ✅ | 텔레그램 채팅 ID (양의 정수) |
| `GEMINI_MODEL_PRIMARY` | 선택 | 기본값 `gemma-4-26b-a4b-it` (정확한 모델명) |
| `GEMINI_MODEL_SECONDARY` | 선택 | 기본값 `gemini-2.5-flash` |
| `PLAYLIST_URL` | 선택 | 기본값 = 새벽예배 재생목록 |
| `YOUTUBE_COOKIES` | 선택 (권장) | YouTube cookies.txt 내용 (Netscape 형식). 봇 차단 우회용 |

### YouTube 봇 차단 우회: `YOUTUBE_COOKIES` 시크릿 (권장)

GitHub Actions의 IP는 YouTube 봇 차단 목록에 자주 등록되어 있습니다.  
이 경우 `youtube-transcript-api`와 `yt-dlp` 모두 작동하지 않습니다.

해결책: 브라우저에서 YouTube cookies.txt 를 추출해 시크릿으로 등록합니다.

1. 브라우저(Chrome 등) 로 YouTube에 로그인
2. 확장 [`Get cookies.txt LOCALLY`](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) 설치
3. YouTube 도메인(`youtube.com`, `.youtube.com`) 에서 cookies.txt 추출
4. GitHub 레포 → Settings → Secrets and variables → Actions → New repository secret
   - Name: `YOUTUBE_COOKIES`
   - Value: cookies.txt 내용 붙여넣기
5. (선택) `expiry` 값이 큰 숫자(예: 13자리 이상) 인지 확인. 너무 작으면 yt-dlp 가 거부할 수 있음

시크릿이 등록되면 워크플로우가 자동으로 `/tmp/youtube/cookies.txt` 에 파일을 쓰고
`yt-dlp` 가 `cookiefile` 옵션으로 인증된 세션으로 자막을 받습니다.

## 모델

- 1순위: `gemma-4-26b-a4b-it` (요청하신 "gemma4 26b it" 모델, **a4b 접미사 포함**)
- 2순위(폴백): `gemini-2.5-flash`

> ⚠️ "gemma-4-26b-it" (a4b 접미사 없음) 은 존재하지 않는 모델입니다. 절대 입력하지 마세요.

## 라이선스

MIT
