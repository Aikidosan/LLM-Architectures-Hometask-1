"""
GitHub Repository Summarizer API

A FastAPI service that accepts a GitHub repository URL and returns a structured
summary of the project: what it does, what technologies are used, and how it's
structured. Uses an LLM (via OpenAI-compatible API) to generate the summary.
"""

import os
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from repo_fetcher import fetch_repo_contents
from llm_client import generate_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GitHub Repository Summarizer",
    description="Accepts a GitHub repository URL and returns a human-readable summary.",
    version="1.0.0",
)


class SummarizeRequest(BaseModel):
    github_url: str = Field(
        ...,
        description="URL of a public GitHub repository",
        examples=["https://github.com/psf/requests"],
    )


class SummarizeResponse(BaseModel):
    summary: str = Field(..., description="Human-readable description of the project")
    technologies: list[str] = Field(
        ..., description="List of main technologies, languages, and frameworks"
    )
    structure: str = Field(..., description="Brief description of the project structure")


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str


@app.post(
    "/summarize",
    response_model=SummarizeResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def summarize(request: SummarizeRequest):
    """Accept a GitHub repo URL, fetch contents, and return an LLM-generated summary."""
    import re

    github_url = request.github_url.strip()

    if not github_url:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "github_url is required."},
        )

    pattern = r"^https?://github\.com/[\w.\-]+/[\w.\-]+(/?|\.git)?$"
    if not re.match(pattern, github_url):
        raise HTTPException(
            status_code=400,
            detail={
                "status": "error",
                "message": "Invalid GitHub URL. Expected format: https://github.com/<owner>/<repo>",
            },
        )

    try:
        repo_context = await fetch_repo_contents(github_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"status": "error", "message": str(exc)})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"status": "error", "message": str(exc)})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"status": "error", "message": str(exc)})
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail={"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("Unexpected error fetching repo contents")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "message": f"Failed to fetch repository: {exc}"},
        )

    if not repo_context.strip():
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "message": "Repository appears to be empty or contains no readable files."},
        )

    try:
        result = await generate_summary(repo_context)
    except EnvironmentError as exc:
        raise HTTPException(status_code=500, detail={"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("LLM summarization failed")
        raise HTTPException(
            status_code=502,
            detail={"status": "error", "message": f"LLM summarization failed: {exc}"},
        )

    return SummarizeResponse(**result)
