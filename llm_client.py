"""
llm_client.py — LLM integration via OpenAI-compatible API.

Supports: NEBIUS_API_KEY (Nebius Token Factory) or OPENAI_API_KEY (OpenAI).
Sends repo context with a structured prompt and parses JSON response.
"""

import os
import json
import logging
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

NEBIUS_BASE_URL = "https://api.studio.nebius.com/v1/"
NEBIUS_MODEL = "meta-llama/Meta-Llama-3.1-70B-Instruct"
OPENAI_MODEL = "gpt-4.1-mini"


def _get_client_and_model() -> tuple[AsyncOpenAI, str]:
    nebius_key = os.environ.get("NEBIUS_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if nebius_key:
        logger.info("Using Nebius Token Factory LLM provider")
        return AsyncOpenAI(api_key=nebius_key, base_url=NEBIUS_BASE_URL), NEBIUS_MODEL

    if openai_key:
        logger.info("Using OpenAI LLM provider")
        return AsyncOpenAI(), OPENAI_MODEL  # auto-reads env vars

    raise EnvironmentError("No LLM API key found. Set NEBIUS_API_KEY or OPENAI_API_KEY.")


SYSTEM_PROMPT = """\
You are an expert software engineer who analyzes GitHub repositories. \
Given the contents of a repository (directory tree, key files, and config files), \
produce a structured analysis in JSON format.

Your response MUST be a valid JSON object with exactly these three fields:

{
  "summary": "<string>",
  "technologies": ["<string>", ...],
  "structure": "<string>"
}

Guidelines for each field:

**summary**: Write a clear, concise description (2-5 sentences) of what the project \
does, its purpose, and its main features. Use Markdown bold for the project name. \
Be specific — mention the domain, key functionality, and target audience if apparent.

**technologies**: List the main programming languages, frameworks, libraries, \
databases, and tools used in the project. Include only significant technologies \
(not trivial utilities). Order by importance. Typically 3-10 items.

**structure**: Describe the project's directory layout in 2-4 sentences. Mention \
the main source directories, where tests live, configuration files, and any notable \
organizational patterns (monorepo, standard package layout, etc.).

IMPORTANT:
- Return ONLY the JSON object, no markdown code fences, no extra text.
- Be factual — only mention technologies and features you can confirm from the files.
- If you are unsure about something, omit it rather than guess.
"""

USER_PROMPT_TEMPLATE = """\
Analyze the following GitHub repository and provide a structured summary.

{repo_context}

Remember: respond with ONLY a valid JSON object containing "summary", \
"technologies", and "structure" fields.
"""


async def generate_summary(repo_context: str) -> dict[str, Any]:
    client, model = _get_client_and_model()
    user_message = USER_PROMPT_TEMPLATE.format(repo_context=repo_context)
    logger.info("Sending %d chars to LLM (model=%s)", len(user_message), model)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
        max_tokens=2000,
    )

    raw_text = response.choices[0].message.content.strip()
    logger.info("LLM response length: %d chars", len(raw_text))
    return _parse_llm_response(raw_text)


def _parse_llm_response(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else len(text)
        text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM JSON: %s", exc)
        import re
        match = re.search(r"\{[\s\S]*\}", raw_text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                raise ValueError(f"LLM returned invalid JSON: {raw_text[:200]}") from exc
        else:
            raise ValueError(f"LLM returned invalid JSON: {raw_text[:200]}") from exc

    summary = data.get("summary", "")
    technologies = data.get("technologies", [])
    structure = data.get("structure", "")
    if not isinstance(summary, str):
        summary = str(summary)
    if not isinstance(structure, str):
        structure = str(structure)
    if not isinstance(technologies, list):
        technologies = [str(technologies)]
    else:
        technologies = [str(t) for t in technologies]
    if not summary:
        raise ValueError("LLM returned an empty summary")
    return {"summary": summary, "technologies": technologies, "structure": structure}
