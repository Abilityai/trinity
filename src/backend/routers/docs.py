"""
Documentation router - serves process documentation files.

Provides endpoints for:
- Listing available documentation
- Serving markdown content
- Documentation index
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import json
import logging

from models import User
from dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/docs", tags=["Documentation"])

# Documentation directory - relative to trinity root
# In Docker, /app/config/process-docs
# In development, ../../../config/process-docs
DOCS_PATHS = [
    Path("/app/config/process-docs"),
    Path(__file__).parent.parent.parent.parent / "config" / "process-docs",
]


def get_docs_dir() -> Path:
    """Find the documentation directory."""
    for path in DOCS_PATHS:
        if path.exists():
            return path
    return None


@router.get("/index")
async def get_docs_index(current_user: User = Depends(get_current_user)):
    """Get documentation index/navigation structure."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        raise HTTPException(status_code=404, detail="Documentation directory not found")

    index_path = docs_dir / "index.json"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Documentation index not found")

    try:
        with open(index_path, "r") as f:
            return json.load(f)
    except Exception as e:
        # #4284: an OS/JSON error names the server-side path; log it, do not return it.
        logger.exception("Failed to read documentation index")
        raise HTTPException(status_code=500, detail=f"Failed to read index ({e.__class__.__name__} — details in backend logs)")


@router.get("/content/{slug:path}")
async def get_doc_content(
    slug: str,
    current_user: User = Depends(get_current_user),
):
    """Get documentation content by slug (supports .md and .json files)."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        raise HTTPException(status_code=404, detail="Documentation directory not found")

    # Security: Prevent path traversal
    if ".." in slug or slug.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid document path")

    # Check if slug already has an extension
    doc_path = None
    is_json = False

    if slug.endswith('.json'):
        # JSON file requested directly
        doc_path = docs_dir / slug
        is_json = True
    elif slug.endswith('.md'):
        # Markdown file with extension
        doc_path = docs_dir / slug
    else:
        # Try with .md extension first (default)
        doc_path = docs_dir / f"{slug}.md"
        if not doc_path.exists():
            # Try .json extension
            json_path = docs_dir / f"{slug}.json"
            if json_path.exists():
                doc_path = json_path
                is_json = True

    if not doc_path or not doc_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    # Verify the path is still within docs_dir (security)
    try:
        doc_path.resolve().relative_to(docs_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid document path")

    try:
        content = doc_path.read_text(encoding="utf-8")
        if is_json:
            # Return JSON content directly
            return JSONResponse(content=json.loads(content))
        return JSONResponse(content={"content": content, "slug": slug})
    except Exception as e:
        logger.exception("Failed to read document %s", slug)
        raise HTTPException(status_code=500, detail=f"Failed to read document ({e.__class__.__name__} — details in backend logs)")


@router.get("/list")
async def list_docs(current_user: User = Depends(get_current_user)):
    """List all available documentation files."""
    docs_dir = get_docs_dir()
    if not docs_dir:
        raise HTTPException(status_code=404, detail="Documentation directory not found")

    docs = []
    for md_file in docs_dir.rglob("*.md"):
        relative_path = md_file.relative_to(docs_dir)
        slug = str(relative_path).replace(".md", "")
        docs.append({
            "slug": slug,
            "path": str(relative_path),
            "title": md_file.stem.replace("-", " ").title()
        })

    return {"documents": docs}
