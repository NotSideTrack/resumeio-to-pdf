from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.schemas.resumeio import BrowserRenderRequest, Extension
from app.services.browser import ResumeioBrowserRenderer
from app.services.resumeio import ResumeioDownloader

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.post("/download/browser")
def download_browser_resume(payload: BrowserRenderRequest):
    """
    Render an authenticated Resume.io preview page in a browser and return it as a PDF.

    Requires RESUMEIO_COOKIE to be set in the server environment.
    """
    renderer = ResumeioBrowserRenderer(
        preview_url=payload.preview_url,
        filename=payload.filename,
        wait_selector=payload.wait_selector,
        timeout_ms=payload.timeout_ms,
        max_pages=payload.max_pages,
    )
    pdf = renderer.generate_pdf()
    return Response(
        pdf,
        headers={
            "Content-Disposition": f'inline; filename="{payload.filename}"',
            "X-Resumeio-Render-Path": "browser",
            "X-Resumeio-Browser-Pages": str(renderer.page_count),
            "X-Resumeio-Browser-Status": renderer.render_status,
        },
        media_type="application/pdf",
    )


@router.post("/download/{rendering_token}")
def download_resume(
    rendering_token: Annotated[str, Path(min_length=24, max_length=24, pattern="^[a-zA-Z0-9]{24}$")],
    image_size: Annotated[int, Query(gt=0, le=2000)] = 2000,
    max_pages: Annotated[int, Query(gt=0, le=50)] = 20,
    extension: Annotated[Extension, Query(...)] = Extension.jpeg,
):
    """
    Download a resume from resume.io and return it as a PDF.

    Parameters
    ----------
    rendering_token : str
        Rendering Token of the resume to download.
    image_size : int, optional
        Size of the images to download, by default 2000.
    max_pages : int, optional
        Maximum number of pages to probe, by default 20.
    extension : str, optional
        Image extension to download, by default "jpg".

    Returns
    -------
    fastapi.responses.Response
        A PDF representation of the resume with appropriate headers for inline display.
    """
    resumeio = ResumeioDownloader(
        rendering_token=rendering_token,
        image_size=image_size,
        extension=extension,
        max_pages=max_pages,
    )
    pdf = resumeio.generate_pdf()
    return Response(
        pdf,
        headers={
            "Content-Disposition": f'inline; filename="{rendering_token}.pdf"',
            "X-Resumeio-Pages-Merged": str(resumeio.pages_merged),
            "X-Resumeio-Page-Probe-Stop": resumeio.page_probe_stop_reason,
        },
        media_type="application/pdf",
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request):
    """
    Render the main index page.

    Parameters
    ----------
    request : fastapi.Request
        The request instance.

    Returns
    -------
    fastapi.templating.Jinja2Templates.TemplateResponse
        Rendered template of the main index page.
    """
    return templates.TemplateResponse("index.html", {"request": request})
