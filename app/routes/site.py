"""Health, the favicon, and the door frames (served and deleted, owner-only like every page)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response

from app import pages
from app.deliveries import frame_name
from app.ring.api import RingError
from app.routes import common

router = APIRouter()


@router.get("/healthz")
def healthz():
    try:
        ring, err = common.source.get(), None
    except RingError as exc:
        ring, err = None, exc
    return {"ok": err is None, "ring": ring.name if ring else None,
            "ring_error": err.status.value if err else None, "classifier": common.classifier.name,
            "token_pasted": bool(common.source.pasted_token())}


@router.get("/frames/{name}")
def frame(request: Request, name: str):
    if frame_name(name.removesuffix(".jpg")) != name:
        raise HTTPException(404)
    path = common.frames.path(name.removesuffix(".jpg"))
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/favicon.ico")
def favicon():  # for clients that ask for it anyway; the pages carry the icon inline
    return Response(pages.FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@router.post("/frames/{name}/delete")
async def frame_delete(request: Request, name: str):
    """Delete a door frame now (and, for a live view, every frame kept from that session). A form gets
    sent back to the page it came from; a script gets {"deleted": n}."""
    form = await request.form() if "form" in request.headers.get("content-type", "") else None
    gone = await run_in_threadpool(_delete_frame, name)
    if form is not None:
        back = common._safe_next(str(form.get("back", "/")))
        return RedirectResponse(back + ("&" if "?" in back else "?") + f"notice={quote('Frame deleted.')}",
                                status_code=303)
    return {"deleted": gone}


def _delete_frame(name: str) -> int:
    stem = name.removesuffix(".jpg")
    if frame_name(stem) != name:
        raise HTTPException(404)
    gone = 0
    for d in (common.DATA / "frames", common.DATA / "live-frames"):
        for p in [d / name, *(d.glob(f"{stem}-f*.jpg") if stem.startswith("live-") else [])]:
            if p.is_file():
                p.unlink()
                gone += 1
    return gone
