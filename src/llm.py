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

SYSTEM_PROMPT = """You are a senior engineer in semiconductor verification, emulation and computer architecture.
Read the given technical document (conference paper, preprint, book excerpt or blog post) and write a
structured summary for engineers.

Rules:
- Write the English fields in English and the *_ko fields as faithful Korean translations of them
  (same structure, same numbers; keep technical terms such as UVM, UPF, emulator in English).
- The readers are domain experts: be precise and dense, no introductory filler.
- extracted_title and authors_or_venue keep the original spelling; include affiliations when given.
- tags: 3-5 short keywords (1-3 words each, common abbreviations preferred, e.g. "UVM", "Emulation", "UPF").
- This is a digest, not a retelling: keep only what an expert needs to judge the paper and decide
  whether to read it. Skip background the reader already knows, examples, and step-by-step detail.
- key_insights: 3-5 concrete takeaways, one sentence each, with the paper's own numbers where available.
- detailed_summary is Markdown, about 200-300 words in total, with these three sections in this order,
  each 2-4 short bullets or a short paragraph:
  ### Background and Problem
  ### Architecture and Methodology
  ### Results and Conclusions
- Use a ```mermaid block when a flow or structure is easier to show as a diagram, and $...$ for math.
- Never invent numbers or facts. If the text is truncated, summarize only what is present."""

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMAuthError(LLMError):
    """인증 실패: 다음 자료도 똑같이 실패하므로 일괄 작업을 멈춰야 한다."""


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


def _user_prompt(doc: ParsedDocument, title_hint: str | None, figure_captions: list[str] | None) -> str:
    hints = []
    if title_hint or doc.title_hint:
        hints.append(f"제목 힌트: {title_hint or doc.title_hint}")
    if doc.authors_hint:
        hints.append(f"저자/출처 힌트: {doc.authors_hint}")
    hints.append(f"자료 유형: {doc.source_type}")
    if doc.truncated:
        hints.append("주의: 본문이 길어 뒷부분이 잘렸습니다.")
    if figure_captions:
        hints.append("Figure captions (pick representative_figure from these numbers):\n"
                     + "\n".join(f"- {c[:200]}" for c in figure_captions))
    else:
        hints.append("No figure captions were found; set representative_figure to null.")
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
        if "authenticat" in str(data.get("result")).lower():
            raise LLMAuthError("Claude Code 로그인이 만료되었습니다. 터미널에서 claude 실행 후 /login 하세요.")
        raise LLMError(f"claude CLI 오류: {data.get('subtype')} {str(data.get('result'))[:300]}")
    try:
        result = LLMExtractionResult.model_validate(data["structured_output"])
    except ValueError as e:
        raise LLMError(f"스키마에 맞지 않는 응답: {e}") from e
    log.info("요약 완료(claude CLI): %.1fs", (data.get("duration_ms") or 0) / 1000)
    return result


def extract(doc: ParsedDocument, title_hint: str | None = None,
            figure_captions: list[str] | None = None) -> LLMExtractionResult:
    user = _user_prompt(doc, title_hint, figure_captions)
    if backend() == "cli":
        result = _extract_cli(user)
        result.tags = _clip(result.tags, 5)
        result.key_insights = _clip(result.key_insights, 5)
        result.key_insights_ko = _clip(result.key_insights_ko, 5)
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
        raise LLMAuthError("API 키가 없거나 잘못되었습니다. .env의 ANTHROPIC_API_KEY를 확인하세요.") from e
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
    result.key_insights_ko = _clip(result.key_insights_ko, 5)
    return result
