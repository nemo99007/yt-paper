"""Claude structured summarization.

Current Claude models reject a forced `tool_choice` (400), so instead of a forced
tool call this uses Structured Outputs: `messages.parse(output_format=<Pydantic>)`
constrains decoding to the JSON schema and returns a validated model instance.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

import anthropic

from . import config
from .schemas import LLMExtractionResult, ParsedDocument

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """당신은 반도체 검증·에뮬레이션·컴퓨터 아키텍처 분야의 시니어 엔지니어입니다.
주어진 기술 문서(논문, 프리프린트, 서적 일부, 블로그)를 읽고 엔지니어 독자를 위한 구조화된 요약을 작성합니다.

규칙:
- extracted_title, authors_or_venue는 원문 표기를 그대로 사용합니다.
- one_line_summary, key_insights, detailed_summary는 한국어로 씁니다. 기술 용어는 영문 그대로 둬도 됩니다.
- tags는 3~5개, key_insights는 3~5개로 씁니다.
- detailed_summary는 마크다운이며 다음 세 섹션을 이 순서로 반드시 포함합니다:
  ### 배경 및 문제 정의
  ### 핵심 아키텍처 및 방법론
  ### 실험 결과 및 결론
- 구조나 흐름을 설명할 때 도움이 되면 ```mermaid 코드 블록을, 수식은 $...$ 를 사용합니다.
- 문서에 없는 수치나 사실을 지어내지 않습니다. 본문이 잘려 있으면 있는 내용 안에서만 요약합니다."""

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def _clip(items: list[str], n: int) -> list[str]:
    seen: list[str] = []
    for x in (s.strip() for s in items):
        if x and x not in seen:
            seen.append(x)
    return seen[:n]


def backend() -> str:
    """'api' (ANTHROPIC_API_KEY) or 'cli' (Claude Code `claude -p`, uses the logged-in subscription)."""
    b = config.LLM_BACKEND
    if b == "auto":
        return "api" if os.getenv("ANTHROPIC_API_KEY") else ("cli" if shutil.which("claude") else "api")
    return b


def _user_prompt(doc: ParsedDocument, title_hint: str | None) -> str:
    hints = []
    if title_hint or doc.title_hint:
        hints.append(f"제목 힌트: {title_hint or doc.title_hint}")
    if doc.authors_hint:
        hints.append(f"저자/출처 힌트: {doc.authors_hint}")
    hints.append(f"자료 유형: {doc.source_type}")
    if doc.truncated:
        hints.append("주의: 본문이 길어 뒷부분이 잘렸습니다.")
    return "\n".join(hints) + f"\n\n<document>\n{doc.text}\n</document>\n\n위 문서를 스키마에 맞게 요약하세요."


def _extract_cli(user: str) -> LLMExtractionResult:
    exe = shutil.which("claude")
    if exe is None:
        raise LLMError("claude CLI를 찾을 수 없습니다. Claude Code를 설치하거나 ANTHROPIC_API_KEY를 설정하세요.")
    cmd = [exe, "-p", "--output-format", "json",
           "--json-schema", json.dumps(LLMExtractionResult.model_json_schema(), ensure_ascii=False),
           "--system-prompt", SYSTEM_PROMPT, "--tools", "", "--no-session-persistence",
           "--setting-sources", "", "--strict-mcp-config"]
    if config.CLI_MODEL:
        cmd += ["--model", config.CLI_MODEL]
    log.info("Claude Code 요약 요청: model=%s, 본문 %d자", config.CLI_MODEL or "(기본)", len(user))
    try:
        with tempfile.TemporaryDirectory() as cwd:  # 프로젝트 CLAUDE.md 등을 읽지 않도록 빈 폴더에서 실행
            proc = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=cwd,
                                  timeout=config.CLI_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude CLI 응답 시간 초과({config.CLI_TIMEOUT}s)") from e
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude CLI 출력 해석 실패 (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}") from e
    if data.get("is_error") or data.get("structured_output") is None:
        raise LLMError(f"claude CLI 오류: {data.get('subtype')} {str(data.get('result'))[:300]}")
    try:
        result = LLMExtractionResult.model_validate(data["structured_output"])
    except ValueError as e:
        raise LLMError(f"스키마에 맞지 않는 응답: {e}") from e
    log.info("요약 완료(claude CLI): %.1fs", (data.get("duration_ms") or 0) / 1000)
    return result


def extract(doc: ParsedDocument, title_hint: str | None = None) -> LLMExtractionResult:
    user = _user_prompt(doc, title_hint)
    if backend() == "cli":
        result = _extract_cli(user)
        result.tags = _clip(result.tags, 5)
        result.key_insights = _clip(result.key_insights, 5)
        return result

    kwargs: dict = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": config.LLM_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
        "output_format": LLMExtractionResult,
    }
    if config.LLM_EFFORT:
        kwargs["output_config"] = {"effort": config.LLM_EFFORT}
    if config.ANTHROPIC_FALLBACKS == "default":
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"

    client = anthropic.Anthropic(max_retries=4)
    log.info("Claude 요약 요청: model=%s, 본문 %d자", config.ANTHROPIC_MODEL, len(doc.text))
    try:
        resp = client.beta.messages.parse(**kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("API 키가 없거나 잘못되었습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.") from e
    except anthropic.PermissionDeniedError as e:
        raise LLMError(f"권한 없음: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise LLMError(f"모델을 찾을 수 없습니다: {config.ANTHROPIC_MODEL} ({e.message})") from e
    except anthropic.BadRequestError as e:
        raise LLMError(f"잘못된 요청: {e.message}") from e
    except anthropic.RateLimitError as e:
        raise LLMError("요청 한도 초과(429). 잠시 후 다시 실행하세요.") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"API 오류 {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"네트워크 오류: {e}") from e

    if resp.stop_reason == "refusal":
        detail = getattr(resp, "stop_details", None)
        raise LLMError(f"모델이 요청을 거절했습니다: {getattr(detail, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise LLMError("응답이 max_tokens에서 잘렸습니다. LLM_MAX_TOKENS를 늘리세요.")
    result = resp.parsed_output
    if result is None:
        raise LLMError(f"구조화 응답을 받지 못했습니다 (stop_reason={resp.stop_reason})")

    usage = resp.usage
    log.info("요약 완료: served_by=%s, input=%s, output=%s tokens", resp.model, usage.input_tokens, usage.output_tokens)
    result.tags = _clip(result.tags, 5)
    result.key_insights = _clip(result.key_insights, 5)
    return result
