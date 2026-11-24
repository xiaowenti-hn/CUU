from pydantic import BaseModel, ConfigDict, Field


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    repo_path: str = ""
    branch: str = ""
    git_remote: str = "origin"
    yunxiao_entry: str = ""
    yunxiao_organization_id: str = ""
    yunxiao_edition: str = "center"
    yunxiao_browser_app: str = ""
    product_browser: str = ""
    base_url: str = ""
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_databases: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ProjectView(ProjectRecord):
    token_configured: bool = False
    current_branch: str | None = None
    current_commit: str | None = None
    git_error: str | None = None


class FieldError(BaseModel):
    code: str
    message: str


class ValidationResult(BaseModel):
    ok: bool
    missing: list[str]
    errors: list[FieldError]
    dirty: bool
    token_configured: bool
    ocu_available: bool
