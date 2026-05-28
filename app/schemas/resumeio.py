from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Extension(str, Enum):
    jpeg = "jpeg"
    png = "png"
    webp = "webp"


class BrowserRenderRequest(BaseModel):
    preview_url: str = Field(..., min_length=8)
    filename: str = Field(default="resume.pdf", pattern=r"^[\w.\- ]+\.pdf$")
    wait_selector: Optional[str] = Field(default=None, max_length=200)
    timeout_ms: int = Field(default=45000, ge=5000, le=120000)
    max_pages: int = Field(default=20, ge=1, le=50)
