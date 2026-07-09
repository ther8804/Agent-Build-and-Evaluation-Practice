# 신규 정보보호 논문 리뷰·요약 에이전트

arXiv(cs.CR)에서 무료 공개된 신규 정보보호 논문을 주기적으로 수집해, 실무 관점의
한국어 요약과 주간 브리핑 문서(`paper_brief_YYYYMMDD.docx` / `.md`)를 자동 생성한다.

- **arXiv 공개 API 직접 호출** — 외부 MCP 없이 동작 (스펙의 대안 구현 방식)
- **OpenAI 호환 API** — `.env` 의 `OPENAI_API_KEY` 로 연결 (OpenRouter 도 지원)
- **요약 아카이브** — JSONL 누적 저장, 중복 요약 방지, 이후 검색 가능
- **Agent-Build-and-Evaluation-Practice 레포 연동** — deep agent 스킬(`paper-review`) 동봉

## 1. 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # OPENAI_API_KEY 입력
```

`.env`:

```dotenv
OPENAI_API_KEY=sk-...        # 필수
OPENAI_BASE_URL=             # 선택. OpenRouter면 https://openrouter.ai/api/v1
OPENAI_MODEL=                # 선택. 기본 gpt-4o-mini
```

## 2. 사용법

```bash
# 설치·파이프라인 검증 (네트워크·API 키 불필요, 픽스처로 전체 흐름 테스트)
python -m paper_review selftest

# 수집·요약·브리핑 1회 실행 (주 1회 실행 권장)
python -m paper_review run

# 과거 요약 아카이브 검색
python -m paper_review search "malware analysis"
```

실행 결과:

```
output/paper_brief_YYYYMMDD.docx   # 주간 브리핑 (Word)
output/paper_brief_YYYYMMDD.md     # 동일 내용 Markdown
output/pdf/                        # 다운로드한 원문 PDF
archive/summaries.jsonl            # 요약 아카이브 (누적)
```

수집 키워드·카테고리·주기는 `paper_review_config.json` 에서 조정한다:

```json
{
  "keywords": ["threat detection", "malware analysis", "LLM security"],
  "categories": ["cs.CR"],
  "days_back": 7,
  "max_summaries": 10,
  "abstract_fallback": true
}
```

### 주 1회 자동 실행 (cron 예시)

```cron
# 매주 월요일 08:00
0 8 * * 1 cd /path/to/project && python -m paper_review run >> logs/paper_review.log 2>&1
```

## 3. Agent-Build-and-Evaluation-Practice 레포에 붙이기

1. `paper_review/`, `paper_review_config.json`, `requirements.txt` 를 레포 루트에 복사
   (또는 `uv add requests pypdf python-docx openai` 로 의존성 추가)
2. `workspace_seed/skills/paper-review/` 를 레포의 `workspace_seed/skills/` 아래에 복사
   → 다음 실행 시 `workspace/skills/paper-review` 로 자동 동기화되고, deep agent 가
   "이번 주 보안 논문 브리핑 만들어줘" 같은 요청에 이 스킬을 읽어 `execute` 도구로
   `python -m paper_review run` 을 실행한다.
3. 레포의 `.env` 는 이미 `OPENAI_API_KEY` 를 쓰므로 그대로 재사용된다. 레포 기본값이
   OpenRouter 키라면 `.env` 에 `OPENAI_BASE_URL=https://openrouter.ai/api/v1` 과
   `OPENAI_MODEL=anthropic/claude-sonnet-5` 처럼 모델명을 함께 지정하면 된다.
4. (선택) Google Drive MCP 커넥터가 연결돼 있으면, **사용자 승인 후** 에이전트가
   생성된 브리핑 파일을 Drive 에 업로드해 팀과 공유할 수 있다. 파이프라인 자체에는
   어떤 발송·공유 기능도 없다.

## 4. 구조

```
paper_review/
├── __main__.py      # CLI: run / search / selftest
├── config.py        # 설정 로드 (paper_review_config.json + .env)
├── arxiv_client.py  # arXiv API 검색, Atom 파싱, 메타데이터 검증, 3초 지연
├── pdf_extract.py   # PDF 다운로드(arxiv.org 한정) + 본문 추출(pypdf)
├── summarizer.py    # LLM 요약 (인젝션 방어, JSON 고정 출력, 복사 검사)
├── archive.py       # JSONL 아카이브 (중복 방지, 검색)
├── briefing.py      # 브리핑 렌더링 (.md + .docx, 관심도 순위)
├── pipeline.py      # 오케스트레이션
└── fixtures/        # selftest 용 샘플 데이터

workspace_seed/skills/paper-review/   # deep agent 스킬 (SKILL.md + 규칙 + 템플릿)
```

## 5. '반드시 지켜야 하는 규칙' 구현 위치

| 규칙 | 구현 |
|---|---|
| 원문에 없는 수치·결과 날조 금지 | `summarizer.SYSTEM_PROMPT` (날조 금지·불확실 시 생략), temperature 0.2 |
| 본문 지시문 미이행 (인젝션 방지) | `<PAPER_DATA>` 구분자 + 시스템 프롬프트 명시, 요약 호출에 **도구 미부여**, 출력은 고정 키 JSON 만 수용, 헤더는 API 메타데이터로 렌더링, `detect_injection()` 이 의심 문구 감지 시 브리핑에 `[인젝션 의심]` 경고 |
| 원문 문단 복사 금지 | 프롬프트 재작성 지시 + `find_verbatim_overlap()` 12단어 연속 일치 사후 검사 → `[복사 의심]` 경고 |
| 무료 공개 논문만 | `pdf_extract._check_host()` — arxiv.org 외 도메인 다운로드 거부 |
| 자동 발송·공유 금지 | 파이프라인에 발송 기능 없음. 문서 하단 "검토·승인 후 공유" 고지 자동 삽입. 스킬 규칙에도 승인 후 공유 명시 |
| 메타데이터 일치 확인 | `arxiv_client.verify_metadata()` + 헤더는 항상 API 응답에서 렌더링 (LLM 이 덮어쓸 수 없음) |
| 중복 요약 방지 | `archive.Archive.contains()` — base_id 기준 dedupe |
| [추출 실패] 분리 / '초록 기반 요약' 명시 | `pipeline.run()` 의 fallback 분기 + 브리핑의 `[추출 실패]` 섹션·`요약 근거` 라벨 |
| 주장 vs 사실 구분 | 프롬프트: "저자들은 ~라고 주장한다" 표기 강제 |
| 템플릿 순서 / arXiv 링크·ID / 에이전트 의견 라벨 / 파일명 규칙 | `briefing.py` 렌더러가 구조적으로 강제 (`paper_brief_YYYYMMDD.docx`) |

## 6. 보안 유의사항

- 논문 본문·PoC 코드는 신뢰할 수 없는 외부 입력으로 취급된다. 요약 LLM 호출에는
  도구가 붙지 않으므로 본문 속 지시문이 실행될 경로가 없다.
- 이 파이프라인은 논문에 포함된 PoC 코드를 **실행하지 않는다**. 실행이 필요하면
  격리 환경에서 사람 승인 후 별도로 진행할 것.
- 브리핑 공유(메일·Slack·Drive)는 사람이 내용을 검토·승인한 뒤 수동(또는 에이전트에
  명시적 지시)으로만 수행한다.
- `[인젝션 의심]` / `[복사 의심]` 경고가 붙은 요약은 반드시 사람이 확인한다.
