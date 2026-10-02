"""Pydantic request/response models."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import config

Status = Literal[
    "suggested", "applied", "oa", "interviewing", "offer", "rejected", "not_interested"
]

# Accepts YYYY-MM-DD or empty. Kept loose on purpose -- a tracker should not
# reject you for a half-typed date.
DATE_FIELDS = ("date_found", "date_applied", "deadline")


class ApplicationBase(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role_title: str = Field(min_length=1, max_length=300)
    status: Status = config.DEFAULT_STATUS
    date_found: Optional[str] = None
    date_applied: Optional[str] = None
    deadline: Optional[str] = None
    # Raw from the clipboard; sanitized server-side before storage.
    jd_html: str = ""
    jd_text_plain: str = ""
    source_url: Optional[str] = None
    notes: str = ""
    tags: str = ""
    # Canonical term, e.g. "Summer 2027". None when the posting doesn't say.
    season: Optional[str] = None

    @field_validator("season")
    @classmethod
    def _canon_season(cls, v: Optional[str]) -> Optional[str]:
        from . import seasons

        return seasons.normalize(v) if v else None

    @field_validator("source_url")
    @classmethod
    def _check_url_scheme(cls, v: Optional[str]) -> Optional[str]:
        """Block javascript:/data: in the one URL we render as a link."""
        if not v:
            return None
        v = v.strip()
        if not v:
            return None
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("source_url must start with http:// or https://")
        return v

    @field_validator(*DATE_FIELDS)
    @classmethod
    def _empty_to_none(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() or None if isinstance(v, str) else v


class ApplicationCreate(ApplicationBase):
    # Client-generated, stable for one filled-in form. Re-POSTing the same key
    # returns the row created the first time instead of inserting again, so an
    # impatient double-click cannot produce duplicates.
    idempotency_key: Optional[str] = Field(default=None, max_length=64)


class ApplicationUpdate(BaseModel):
    """All fields optional -- PATCH semantics."""

    company: Optional[str] = Field(default=None, min_length=1, max_length=200)
    role_title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    status: Optional[Status] = None
    date_found: Optional[str] = None
    date_applied: Optional[str] = None
    deadline: Optional[str] = None
    jd_html: Optional[str] = None
    jd_text_plain: Optional[str] = None
    source_url: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    season: Optional[str] = None

    _check_url_scheme = field_validator("source_url")(
        ApplicationBase._check_url_scheme.__func__
    )
    _canon_season = field_validator("season")(ApplicationBase._canon_season.__func__)


class Application(ApplicationBase):
    id: int
    external_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: str
    updated_at: str


class FetchResult(BaseModel):
    ok: bool
    fetched: int = 0
    matched: int = 0
    inserted: int = 0
    # Imported straight to not_interested because of AUTO_NOT_INTERESTED_SEASONS.
    auto_archived: int = 0
    skipped_existing: int = 0
    error: Optional[str] = None
    ran_at: Optional[str] = None
