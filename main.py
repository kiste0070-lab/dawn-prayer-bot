# ============================================================
# Dawn Prayer Bot - 새벽예배 말씀 정리 및 텔레그램 발송 봇
# 
# - 유튜브 '새벽예배' 재생목록의 최신 영상의 한국어 자동 자막 추출
# - Gemini API(gemma-4-26b-it)로 큐티(QT) 형식(제목/말씀/해설/적용/기도) 정리
# - 텔레그램으로 발송
# - GitHub Actions로 매일 KST 08:00 (UTC 23:00) 실행
# ============================================================
import os
import re
import sys
import json
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError
from telegram import Bot
from telegram.error import TelegramError

# ============================================================
# 로깅 설정
# ============================================================
log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.FileHandler("dawn_prayer_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ============================================================
# 상수
# ============================================================
PLAYLIST_URL = (
    "https://www.youtube.com/playlist?list=PLHEvWqh20QZJdZlsooU4gnoe8EAnqt_yF"
)
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
KST = timezone(timedelta(hours=9))

# 자막 정리용 정규식 캐시
TIMECODE_PATTERN = re.compile(
    r"\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*$",
    re.MULTILINE,
)
VTT_TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
# 비언어 메타(음악, 박수, 기침 등)는 자막에서 제거 대상
NON_KOREAN_AUDIO_PATTERN = re.compile(
    r"\[("
    r"음악|박수|웃음|박수와\s*웃음|박수박수|박수소리|박수\w*|"
    r"노래|기침|목을\s*가다듬음|발걸음|발소리|박수소리|환호|박수\s*및\s*웃음"
    r")\]"
)


# ============================================================
# Pydantic 모델
# ============================================================
class Settings(BaseModel):
    """환경 변수 기반 설정"""

    telegram_token: str = Field(..., min_length=10)
    gemini_api_key: str = Field(..., min_length=10)
    chat_id: int = Field(..., gt=0, description="텔레그램 채팅 ID (양의 정수)")
    model_primary: str = "gemma-4-26b-a4b-it"
    model_secondary: str = "gemini-2.5-flash"
    playlist_url: str = PLAYLIST_URL

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        try:
            chat_id_str = os.getenv("CHAT_ID", "0")
            chat_id = int(chat_id_str)
        except (ValueError, TypeError):
            chat_id = 0
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            chat_id=chat_id,
            model_primary=os.getenv("GEMINI_MODEL_PRIMARY", "gemma-4-26b-a4b-it"),
            model_secondary=os.getenv("GEMINI_MODEL_SECONDARY", "gemini-2.5-flash"),
            playlist_url=os.getenv("PLAYLIST_URL", PLAYLIST_URL),
        )


class VideoInfo(BaseModel):
    """유튜브 비디오 메타데이터"""

    video_id: str
    title: str
    url: str
    upload_date: str
    duration: int


class QTIDocument(BaseModel):
    """큐티(QT) 형식의 정리된 말씀 문서"""

    title: str = Field(..., description="큐티 제목")
    scripture: str = Field(default="", description="말씀 본문 (성경 구절)")
    explanation: str = Field(..., description="해설/원문 해석")
    application: str = Field(..., description="적용/오늘의 묵상")
    prayer: str = Field(..., description="마무리 기도문")

    @property
    def has_scripture(self) -> bool:
        return bool(self.scripture and self.scripture.strip())

    def to_markdown(self, video_info: VideoInfo, generated_at: str) -> str:
        scripture_section = (
            f"## 📖 말씀 본문\n\n{self.scripture}\n\n"
            if self.has_scripture
            else "## 📖 말씀 본문\n\n_(영상에서 별도로 인용된 본문이 확인되지 않아, 해설/적용 위주로 정리했습니다.)_\n\n"
        )
        return (
            f"# 🙏 {self.title}\n\n"
            f"**📺 영상**: [{video_info.title}]({video_info.url})\n"
            f"**🕒 생성 시각**: {generated_at}\n\n"
            f"{scripture_section}"
            f"## 💡 해설\n\n{self.explanation}\n\n"
            f"## 🌱 적용 (오늘의 묵상)\n\n{self.application}\n\n"
            f"## 🙏 마무리 기도\n\n{self.prayer}\n"
        )

    def to_telegram_message(self, video_info: VideoInfo) -> str:
        """텔레그램용 한 메시지 (마크다운, 4096자 제한 고려)"""
        scripture_block = (
            f"📖 *말씀 본문*\n{self.scripture}\n\n"
            if self.has_scripture
            else "📖 *말씀 본문*\n_(본문 인용이 확인되지 않아, 해설/적용 위주로 정리했습니다.)_\n\n"
        )
        msg = (
            f"🙏 *{self.title}*\n\n"
            f"📺 _{video_info.title}_\n"
            f"🔗 {video_info.url}\n\n"
            f"{scripture_block}"
            f"💡 *해설*\n{self.explanation}\n\n"
            f"🌱 *적용 (오늘의 묵상)*\n{self.application}\n\n"
            f"🙏 *마무리 기도*\n{self.prayer}"
        )
        return msg


# ============================================================
# 1) 유튜브 재생목록 / 자막 추출
# ============================================================
def get_playlist_entries(playlist_url: str, n: int = 3) -> list[VideoInfo]:
    """재생목록에서 최신 n개 비디오의 메타데이터를 가져온다."""
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "playlist_items": f"1-{n}",
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        entries = info.get("entries") or []
        result: list[VideoInfo] = []
        for e in entries:
            if not e or not e.get("id"):
                continue
            result.append(
                VideoInfo(
                    video_id=e["id"],
                    title=e.get("title") or "untitled",
                    url=f"https://www.youtube.com/watch?v={e['id']}",
                    upload_date=e.get("upload_date") or "",
                    duration=int(e.get("duration") or 0),
                )
            )
        if not result:
            raise RuntimeError("재생목록에서 비디오를 찾을 수 없습니다.")
        return result


def download_korean_subtitle(video_id: str) -> Path:
    """지정 비디오의 한국어 자동 자막(VTT)을 임시 폴더에 저장하고 경로 반환."""
    tmp_dir = BASE_DIR / "tmp_subs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(tmp_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": False,
        "subtitleslangs": ["ko", "ko-orig"],
        "subtitlesformat": "vtt",
        "outtmpl": outtmpl,
        "js_runtimes": {"node": {}},
        "quiet": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # 다운로드된 vtt 파일 찾기
    candidates = sorted(tmp_dir.glob(f"{video_id}*.vtt"))
    if not candidates:
        raise RuntimeError(f"한국어 자막을 찾을 수 없습니다: {video_id}")
    return candidates[0]


def parse_vtt_to_text(vtt_path: Path) -> str:
    """VTT 파일을 파싱해 중복 제거한 한국어 텍스트만 추출."""
    raw = vtt_path.read_text(encoding="utf-8")
    # 타임코드 라인 제거
    raw = TIMECODE_PATTERN.sub("", raw)
    # WEBVTT 헤더, NOTE, STYLE, KIND/LANG 메타 제거
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("WEBVTT") or s.startswith("NOTE") or s.startswith("STYLE"):
            continue
        if s.startswith("Kind:") or s.startswith("Language:"):
            continue
        # HTML 태그 제거
        s = VTT_TAG_PATTERN.sub("", s)
        s = WHITESPACE_PATTERN.sub(" ", s).strip()
        if not s:
            continue
        lines.append(s)

    # 중복 라인 제거 (자동 자막은 같은 라인이 2번 나오는 경우가 많음)
    deduped: list[str] = []
    prev: str | None = None
    for s in lines:
        if s != prev:
            deduped.append(s)
        prev = s

    # [음악] / [박수] 같은 비언어 메타만으로 구성된 짧은 토큰은 제거
    cleaned: list[str] = []
    for s in deduped:
        stripped = NON_KOREAN_AUDIO_PATTERN.sub("", s).strip()
        # 한글이 하나라도 포함된 경우만 유지
        if any("\uac00" <= ch <= "\ud7a3" for ch in stripped):
            cleaned.append(stripped)
    return "\n".join(cleaned)


def fetch_subtitle_text(video_id: str) -> str:
    """비디오 ID로 한국어 자막을 추출해 깨끗한 텍스트로 반환."""
    vtt_path = download_korean_subtitle(video_id)
    return parse_vtt_to_text(vtt_path)


# ============================================================
# 2) Gemini API 로 큐티(QT) 형식 정리
# ============================================================
QTI_PROMPT_TEMPLATE = """너는 신학적으로 깊이 있는 새벽예배 큐티(QT) 정리 도우미야.
아래는 한국 유튜브 '새벽예배' 라이브/영상에서 자동 추출한 한국어 자막이야.
자막은 자동 인식이라 오타·중복이 있을 수 있어. 의미 단위로 다듬어 다음 형식의 큐티로 정리해 줘.

[출력 형식 - 반드시 이 5개 섹션만, 각 섹션은 ## 헤더로 시작]
## 제목
(한 줄, 30자 이내, 오늘 다룬 말씀의 핵심을 담은 제목)

## 말씀 본문
(매우 중요!) 자막에서 성경 본문을 반드시 추출해. 보통 'O편 말씀 보겠습니다', '성경 봉독하겠습니다', '말씀을 읽겠습니다' 같은 도입 직후에 본문이 통째로 인용됨. 찬양 가사([노래], [음악] 등)나 기도, 광고/안내 멘트는 제외하고 순수 성경 본문만 골라내.
- 본문은 한국어 개역개정 스타일로 2~6절 정도 인용 (예: '시편 127:1-3 여호와께서 집을 세우지 아니하시면...')
- 자막에 여러 차례 등장하면 가장 완전한 본문 1회만 인용
- 정말 본문으로 판단되는 구절이 자막에 전혀 없을 때만 빈 문자열로 두고 절대 다른 내용으로 채우지 마

## 해설
(자막의 설교/해설 내용을 5~8줄로 요약. 핵심 메시지와 신학적 의미 포함)

## 적용
(오늘 하루 실천할 수 있는 적용/묵상 포인트를 3~5개의 짧은 항목으로, 불릿 포인트)

## 기도
(2~4줄 정도의 마무리 기도문. '주님' 또는 '하나님' 호칭 사용, 따뜻하고 진솔하게)

규칙:
1. 다른 인사말, 부연 설명, 메타 코멘트 일절 없이 위 5개 섹션만 출력.
2. 자막에 실제로 있는 내용만 다듬어. 없는 내용을 지어내지 마.
3. 영상은 한국어 설교/찬양/말씀 중심이므로 신중하고 경건한 톤으로 작성.
4. 한국어로 작성.
5. '## 말씀 본문' 섹션이 비어 있을 경우에도 다른 4개 섹션은 반드시 채워.

[자막]
{transcript}
"""


def _extract_qti_sections(raw: str) -> QTIDocument:
    """모델 출력 텍스트에서 5개 섹션을 파싱해 QTIDocument로 변환."""
    section_keys = {
        "제목": "title",
        "말씀 본문": "scripture",
        "해설": "explanation",
        "적용": "application",
        "기도": "prayer",
    }
    parsed: dict[str, str] = {v: "" for v in section_keys.values()}

    # ## 헤더 기준으로 분리
    pattern = re.compile(
        r"^##\s*(제목|말씀\s*본문|해설|적용|기도)\s*$", re.MULTILINE
    )
    matches = list(pattern.finditer(raw))
    for i, m in enumerate(matches):
        key_ko = m.group(1).replace(" ", "")
        field = section_keys.get(key_ko)
        if not field:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        parsed[field] = raw[start:end].strip()

    # 누락된 섹션이 있으면 빈 문자열로라도 채워 Validation 통과
    for v in section_keys.values():
        parsed.setdefault(v, "")
    return QTIDocument(**parsed)


def generate_qti(transcript: str, settings: Settings) -> QTIDocument:
    """Gemini API 호출하여 큐티 형식 문서를 생성한다. 폴백 모델 포함."""
    if not transcript.strip():
        raise RuntimeError("자막이 비어 있어 큐티를 생성할 수 없습니다.")

    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = QTI_PROMPT_TEMPLATE.format(transcript=transcript[:18000])
    models: list[str] = [settings.model_primary, settings.model_secondary]
    last_error: Exception | None = None

    for model_id in models:
        logger.info(f"큐티 생성 시도 - 모델: {model_id}")
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model_id, contents=prompt
                )
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("모델 응답이 비어 있습니다.")
                qti = _extract_qti_sections(text)
                logger.info(f"큐티 생성 성공 - 모델: {model_id}, 사용 시도: {attempt + 1}")
                return qti
            except Exception as e:  # noqa: BLE001
                last_error = e
                msg = str(e)
                logger.warning(
                    f"[{model_id}] 큐티 생성 실패 (시도 {attempt + 1}/3): {msg[:200]}"
                )
                transient = any(
                    s in msg for s in ("503", "UNAVAILABLE", "500", "INTERNAL", "429")
                )
                if transient and attempt < 2:
                    time.sleep(60)
                    continue
                # 비일시적이거나 재시도 소진 -> 다음 모델로
                break
        logger.info(f"모델 {model_id} 실패, 다음 모델로 폴백합니다.")

    raise RuntimeError(
        f"모든 모델에서 큐티 생성 실패. 마지막 에러: {last_error}"
    )


# ============================================================
# 3) 텔레그램 발송
# ============================================================
async def _send_with_fallback(bot: Bot, chat_id: int, text: str) -> None:
    """단일 메시지 발송. Markdown 파싱 실패 시 plain text로 재시도."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning(
            f"Markdown 파싱 실패, plain text로 재시도: {str(e)[:150]}"
        )
        # Markdown 특수문자(_*`[) 이스케이프 후 재시도
        escaped = re.sub(r"([_*`\[\]])", r"\\\1", text)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=escaped,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
        except TelegramError as e2:
            logger.warning(
                f"MarkdownV2도 실패, parse_mode 없이 최종 재시도: {str(e2)[:150]}"
            )
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                disable_web_page_preview=True,
            )


async def send_to_telegram(message: str, settings: Settings) -> None:
    bot = Bot(token=settings.telegram_token)
    chunk_size = 4000
    if len(message) <= chunk_size:
        await _send_with_fallback(bot, settings.chat_id, message)
        return

    parts: list[str] = []
    cur = ""
    for line in message.split("\n"):
        if len(cur) + len(line) + 1 > chunk_size:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)

    for idx, part in enumerate(parts):
        await _send_with_fallback(
            bot, settings.chat_id, f"(part {idx + 1}/{len(parts)})\n{part}"
        )
        await asyncio.sleep(0.5)


# ============================================================
# 4) 결과 저장
# ============================================================
def save_outputs(
    qti: QTIDocument,
    video_info: VideoInfo,
    transcript: str,
    generated_at: str,
) -> Path:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out_path = OUTPUT_DIR / f"{today}_{video_info.video_id}.md"
    body = qti.to_markdown(video_info, generated_at)
    body += "\n---\n\n## 📜 자동 추출 자막 (원본)\n\n```\n" + transcript[:8000] + "\n```\n"
    out_path.write_text(body, encoding="utf-8")
    logger.info(f"결과 파일 저장: {out_path}")
    return out_path


# ============================================================
# 5) 메인
# ============================================================
def get_already_processed_video_ids() -> set[str]:
    """outputs/ 폴더에 이미 저장된 video_id들을 스캔 (중복 처리 방지)."""
    processed: set[str] = set()
    if not OUTPUT_DIR.exists():
        return processed
    for f in OUTPUT_DIR.glob("*_*.md"):
        # 파일명 형식: YYYY-MM-DD_<videoId>.md
        stem = f.stem
        parts = stem.split("_", 1)
        if len(parts) == 2 and len(parts[0]) == 10 and parts[0][4] == "-" and parts[0][7] == "-":
            processed.add(parts[1])
    return processed


async def run(settings: Settings, dry_run: bool = False) -> int:
    generated_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    logger.info("=== Dawn Prayer Bot 시작 ===")

    # 1) 재생목록에서 최신 비디오 n개 가져오기
    logger.info(f"재생목록 조회: {settings.playlist_url}")
    candidates = get_playlist_entries(settings.playlist_url, n=3)
    logger.info(f"후보 영상 {len(candidates)}개: {[c.video_id for c in candidates]}")

    # 1-1) 이미 처리된 영상 제외
    already_done = get_already_processed_video_ids()
    new_candidates = [c for c in candidates if c.video_id not in already_done]
    if not new_candidates:
        logger.info(
            f"후보 영상 {len(candidates)}개가 모두 이미 처리됨. "
            f"새 영상이 없으므로 종료합니다."
        )
        return 0
    if len(new_candidates) < len(candidates):
        logger.info(
            f"이미 처리된 {len(candidates) - len(new_candidates)}개 제외 → "
            f"신규 후보 {len(new_candidates)}개: "
            f"{[c.video_id for c in new_candidates]}"
        )
    candidates = new_candidates

    # 2) 자막 추출 (최신부터 차례로 시도, 라이브/오류 영상은 폴백)
    transcript = ""
    video_info: VideoInfo | None = None
    for idx, cand in enumerate(candidates):
        try:
            transcript = fetch_subtitle_text(cand.video_id)
            video_info = cand
            logger.info(
                f"자막 추출 성공(후보 {idx + 1}/{len(candidates)}): {cand.title}"
            )
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"자막 추출 실패(후보 {idx + 1}, {cand.video_id}): {str(e)[:200]}"
            )
            continue
    if not video_info or not transcript:
        raise RuntimeError("모든 후보 영상에서 자막 추출에 실패했습니다.")
    logger.info(f"자막 길이: {len(transcript)}자")

    # 3) 큐티 생성
    qti = generate_qti(transcript, settings)
    logger.info(f"큐티 생성 완료: {qti.title}")

    # 4) 결과 저장
    save_outputs(qti, video_info, transcript, generated_at)

    # 5) 텔레그램 발송 (dry-run이면 미리보기만 출력하고 스킵)
    msg = qti.to_telegram_message(video_info)
    if dry_run:
        logger.info("[DRY-RUN] 텔레그램 발송 생략, 콘솔 미리보기만 출력")
        _print_preview(msg)
    else:
        await send_to_telegram(msg, settings)
        logger.info("텔레그램 발송 완료")

    logger.info("=== Dawn Prayer Bot 정상 종료 ===")
    return 0


def _print_preview(message: str) -> None:
    """dry-run 모드에서 콘솔에 큐티 메시지 미리보기 출력 (인코딩 안전)."""
    print("\n" + "=" * 60)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    preview = message[:2000]
    try:
        print(preview)
    except UnicodeEncodeError:
        print(preview.encode("ascii", "replace").decode("ascii"))
    print("=" * 60)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    try:
        settings = Settings.from_env()
    except ValidationError as e:
        logger.error(f"설정 오류: {e}")
        return 2

    if dry_run:
        logger.info("=== DRY-RUN 모드: 큐티 생성/저장만 수행 (텔레그램 발송 생략) ===")

    try:
        return asyncio.run(run(settings, dry_run=dry_run))
    except TelegramError as e:
        logger.error(f"텔레그램 발송 오류: {e}")
        return 3
    except Exception as e:  # noqa: BLE001
        logger.exception(f"실행 중 오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
