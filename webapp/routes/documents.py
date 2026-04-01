"""
Documents API routes — PDF upload, download, view, delete
"""
from fastapi import APIRouter, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
import os
from urllib.parse import quote

from config import DOCUMENT_DIR
from auth import require_auth, require_authenticated_user

router = APIRouter(prefix="/api/documents", tags=["documents"])

# Max upload size: 50 MB
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024


def _safe_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal"""
    return os.path.basename(filename)


@router.get("")
async def list_documents(request: Request):
    """List all uploaded PDF documents"""
    require_authenticated_user(request)
    documents = []
    if os.path.exists(DOCUMENT_DIR):
        for f in os.listdir(DOCUMENT_DIR):
            if f.lower().endswith(".pdf"):
                path = os.path.join(DOCUMENT_DIR, f)
                stat = os.stat(path)
                documents.append({
                    "filename": f,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
    documents.sort(key=lambda x: x["modified"], reverse=True)
    return {"documents": documents}


@router.post("/upload")
async def upload_document(request: Request, file: UploadFile = File(...)):
    """Upload a PDF document"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    safe_name = _safe_filename(file.filename)
    dest_path = os.path.join(DOCUMENT_DIR, safe_name)

    total_size = 0
    first_chunk = b""
    with open(dest_path, "wb") as f:
        while chunk := await file.read(UPLOAD_CHUNK_SIZE):
            if not first_chunk:
                first_chunk = chunk[:8]
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE:
                f.close()
                os.remove(dest_path)
                raise HTTPException(status_code=400, detail="File too large (max 50 MB)")
            f.write(chunk)

    if not first_chunk.startswith(b"%PDF"):
        os.remove(dest_path)
        raise HTTPException(status_code=400, detail="Invalid PDF file")

    await file.close()

    return {
        "success": True,
        "filename": safe_name,
        "size": total_size,
        "message": f"Uploaded {safe_name}",
    }


@router.get("/view/{filename}")
async def view_document(request: Request, filename: str):
    """Serve PDF for in-browser viewing"""
    require_authenticated_user(request)
    safe_name = _safe_filename(filename)
    file_path = os.path.join(DOCUMENT_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Document not found")
    encoded_name = quote(safe_name)
    return FileResponse(
        file_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}"},
    )


@router.get("/{filename}")
async def download_document(request: Request, filename: str):
    """Download a PDF document"""
    require_authenticated_user(request)
    safe_name = _safe_filename(filename)
    file_path = os.path.join(DOCUMENT_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Document not found")
    encoded_name = quote(safe_name)
    return FileResponse(
        file_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@router.delete("/{filename}")
async def delete_document(filename: str, request: Request):
    """Delete a PDF document"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    safe_name = _safe_filename(filename)
    file_path = os.path.join(DOCUMENT_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Document not found")

    os.remove(file_path)
    return {"success": True, "message": f"Deleted {safe_name}"}
