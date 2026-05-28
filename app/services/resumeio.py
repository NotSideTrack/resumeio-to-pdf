import io
import json
from dataclasses import dataclass
from datetime import datetime

import requests
from fastapi import HTTPException
from PIL import Image
from pypdf import PdfReader, PdfWriter

from app.schemas.resumeio import Extension


@dataclass
class ResumeioDownloader:
    """
    Class to download a resume from resume.io and convert it to a PDF.

    Parameters
    ----------
    rendering_token : str
        Rendering Token of the resume to download.
    extension : str, optional
        Image extension to download, by default "jpeg".
    image_size : int, optional
        Size of the images to download, by default 2000.
    max_pages : int, optional
        Maximum number of page images to probe, by default 20.
    """

    rendering_token: str
    extension: Extension = Extension.jpeg
    image_size: int = 2000
    max_pages: int = 20
    METADATA_URL: str = "https://ssr.resume.tools/meta/{rendering_token}?cache={cache_date}"
    IMAGES_URL: str = (
        "https://ssr.resume.tools/to-image/{rendering_token}-{page_id}.{extension}?cache={cache_date}&size={image_size}"
    )

    def __post_init__(self) -> None:
        """Set the cache date to the current time."""
        self.cache_date = datetime.utcnow().isoformat()[:-4] + "Z"
        self.pages_merged = 0
        self.page_probe_stop_reason = ""

    def generate_pdf(self) -> bytes:
        """
        Generate a PDF from the resume.io resume.

        Returns
        -------
        bytes
            PDF representation of the resume.
        """
        self.__get_resume_metadata()
        images = self.__download_images()
        self.pages_merged = len(images)
        pdf = PdfWriter()

        for i, image in enumerate(images):
            page = self.__image_to_pdf_page(image)
            pdf.add_page(page)

            if i >= len(self.metadata):
                continue

            metadata_w, metadata_h = self.metadata[i].get("viewport").values()
            page_scale = max(page.mediabox.height / metadata_h, page.mediabox.width / metadata_w)

            for metadata_link in self.metadata[i].get("links") or []:
                link_url = metadata_link["url"]
                x = metadata_link["x"] * page_scale
                y = metadata_link["y"] * page_scale
                w = metadata_link["w"] * page_scale
                h = metadata_link["h"] * page_scale

                annotation = {
                    "/Type": "/Annot",
                    "/Subtype": "/Link",
                    "/Rect": [x, y, x + w, y + h],
                    "/Border": [0, 0, 0],
                    "/A": {"/S": "/URI", "/URI": link_url},
                }
                pdf.add_annotation(page_number=i, annotation=annotation)

        with io.BytesIO() as file:
            pdf.write(file)
            return file.getvalue()

    def __get_resume_metadata(self) -> None:
        """Download the metadata for the resume."""
        response = self.__get(
            self.METADATA_URL.format(rendering_token=self.rendering_token, cache_date=self.cache_date),
        )
        content: dict[str, list] = json.loads(response.text)
        self.metadata = content.get("pages") or []

    def __download_images(self) -> list[io.BytesIO]:
        """Download the images for the resume.

        Probes pages sequentially (starting at 1) until the server returns a
        non-200 response, so all pages are captured even when the metadata
        endpoint under-reports the page count.

        Returns
        -------
        list[io.BytesIO]
            List of image files.
        """
        images = []
        first_page_size = None
        for page_id in range(1, self.max_pages + 1):
            image_url = self.IMAGES_URL.format(
                rendering_token=self.rendering_token,
                page_id=page_id,
                extension=self.extension,
                cache_date=self.cache_date,
                image_size=self.image_size,
            )
            response = self.__get(image_url, raise_on_error=False)
            if response.status_code != 200:
                self.page_probe_stop_reason = f"page {page_id}: HTTP {response.status_code}"
                break

            content_type = response.headers.get("content-type", "unknown")
            if not self.__is_image_response(response):
                self.page_probe_stop_reason = f"page {page_id}: non-image response ({content_type})"
                break

            page_size = self.__image_size(response.content)
            if first_page_size is None:
                first_page_size = page_size
            elif page_size != first_page_size:
                self.page_probe_stop_reason = f"page {page_id}: placeholder image size {page_size[0]}x{page_size[1]}"
                break

            images.append(io.BytesIO(response.content))

        if not images:
            raise HTTPException(
                status_code=404,
                detail=f"Unable to download any pages (rendering token: {self.rendering_token})",
            )

        if not self.page_probe_stop_reason:
            self.page_probe_stop_reason = f"reached max_pages={self.max_pages}"

        return images

    def __is_image_response(self, response: requests.Response) -> bool:
        """Return whether the response body can be opened as an image."""
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            return True

        try:
            Image.open(io.BytesIO(response.content)).verify()
        except Exception:
            return False

        return True

    def __image_size(self, content: bytes) -> tuple[int, int]:
        """Return image dimensions for a response body."""
        with Image.open(io.BytesIO(content)) as image:
            return image.size

    def __image_to_pdf_page(self, image_file: io.BytesIO):
        """Convert one downloaded page image into one PDF page."""
        image_file.seek(0)
        with Image.open(image_file) as image:
            if image.mode != "RGB":
                image = image.convert("RGB")

            page_pdf = io.BytesIO()
            image.save(page_pdf, format="PDF", resolution=72.0)

        page_pdf.seek(0)
        return PdfReader(page_pdf).pages[0]

    def __get(self, url: str, raise_on_error: bool = True) -> requests.Response:
        """Get a response from a URL.

        Parameters
        ----------
        url : str
            URL to get.
        raise_on_error : bool, optional
            Whether to raise an HTTPException on non-200 responses, by default True.

        Returns
        -------
        requests.Response
            Response object.

        Raises
        ------
        HTTPException
            If the response status code is not 200 and raise_on_error is True.
        """
        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36",
            },
        )
        if raise_on_error and response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Unable to download resume (rendering token: {self.rendering_token})",
            )
        return response
