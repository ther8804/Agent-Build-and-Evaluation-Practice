"""신규 정보보호 논문 리뷰·요약 에이전트 (arXiv cs.CR).

arXiv 공개 API를 직접 호출해(외부 MCP 불필요) 신규 정보보호 논문을 수집하고,
OpenAI 호환 API(OPENAI_API_KEY)로 실무 관점의 한국어 요약과 주간 브리핑 문서를
생성한다.

파이프라인: 검색 → 중복 확인(아카이브) → PDF 다운로드·본문 추출 → LLM 요약
→ 메타데이터 검증 → 아카이브 적재 → 주간 브리핑(paper_brief_YYYYMMDD.docx/.md)
"""

__version__ = "0.1.0"
