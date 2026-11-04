# mcp: none — UI-only: the Workspace Build page shares the company profile between people's guides
"""Workspace Build: the company profile, kept once per instance (core-agent AC-14).

Each person who can build has their own guide. The first one's research is kept here
so a colleague's guide starts from it. People who can build read and write it with
their own session; an agent key never does (`require_person`), and Workspace-only
accounts are refused by the role floor before they get here.
"""

from fastapi import APIRouter, Depends, HTTPException

from dependencies import require_person, require_role
from models import User, WorkspaceCompany, WorkspaceCompanyResponse
from services import workspace_company

router = APIRouter(prefix="/api/workspace-build", tags=["workspace-build"])


@router.get("/company", response_model=WorkspaceCompanyResponse, dependencies=[Depends(require_role("creator"))])
async def get_company(current_user: User = Depends(require_person)):
    """The company profile, or 404 when nobody has kept one yet."""
    company = workspace_company.load_company()
    if not company:
        raise HTTPException(status_code=404, detail="No company profile yet")
    return company


@router.put("/company", response_model=WorkspaceCompanyResponse, dependencies=[Depends(require_role("creator"))])
async def put_company(body: WorkspaceCompany, current_user: User = Depends(require_person)):
    """Keep the company profile, as this person."""
    return workspace_company.save_company(body.model_dump(), current_user.username)
