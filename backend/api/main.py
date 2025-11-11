from __future__ import annotations

import os
import asyncio
import json
import tempfile
import shutil
import time
import io
import requests
import threading
import logging
from typing import AsyncGenerator, Dict, Any, List, Optional
import uuid
from pathlib import Path
from datetime import datetime
import aiofiles

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends, BackgroundTasks
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRouter
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
from backend.core.database import init_database, upsert_job, get_job, list_jobs
from backend.core.file_versions import file_version_manager
from backend.core.strategy_feedback import strategy_feedback_manager, StrategyFeedback

# Import REAL backend components
from backend.settings import Settings
from backend.utils import (
    derive_history_profile, 
    read_text_from_file, 
    write_text_to_file,  # ⭐ REAL FILE WRITING
    load_heuristics,
    extract_drive_file_id,
    download_drive_file,
    get_google_credentials,
    create_google_doc,
    make_style_skeleton_from_docx,
    write_docx_with_skeleton,
    make_style_sequence_from_docx
)
# Style functions are already available in backend.utils

def validate_file_content(content: bytes, file_type: str) -> bool:
    """Validate file content matches declared type using magic bytes"""
    if not content:
        return False
    
    # Magic bytes for different file types
    magic_signatures = {
        'txt': [],  # Text files have no specific magic bytes
        'md': [],   # Markdown files have no specific magic bytes
        'pdf': [b'%PDF'],
        'docx': [b'PK\x03\x04'],  # ZIP-based format
        'doc': [b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'],  # OLE2 format
    }
    
    if file_type not in magic_signatures:
        return False
    
    signatures = magic_signatures[file_type]
    
    # If no specific signature, do basic content validation
    if not signatures:
        if file_type in ['txt', 'md']:
            # Check if content is valid text
            try:
                content.decode('utf-8')
                return True
            except UnicodeDecodeError:
                return False
        return True
    
    # Check magic bytes
    for signature in signatures:
        if content.startswith(signature):
            return True
    
    return False

def safe_encoder(obj):
    """Safely encode JSON for SSE, ensuring no newlines or special chars break the format"""
    json_str = json.dumps(obj, ensure_ascii=True, separators=(',', ':'))
    # Replace any remaining newlines or carriage returns
    json_str = json_str.replace('\n', '\\n').replace('\r', '\\r')
    
    # Debug: Log if the original JSON contained problematic characters
    original_json = json.dumps(obj, ensure_ascii=True, separators=(',', ':'))
    if '\n' in original_json or '\r' in original_json:
        print(f"DEBUG: Original JSON contained problematic chars: {repr(original_json)}")
    
    return json_str

def validate_style_skeleton(skel: Dict[str, Any]) -> Dict[str, Any]:
    """Comprehensive style skeleton validation with security checks."""
    validation = {
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "suggestions": []
    }
    
    try:
        # Basic validation
        if not isinstance(skel, dict):
            validation["errors"].append("Style skeleton must be a dictionary")
            validation["is_valid"] = False
            return validation
        
        # Check for required style sections
        required_styles = ["Normal", "Heading 1", "Heading 2", "Heading 3"]
        for style_name in required_styles:
            if style_name not in skel:
                validation["warnings"].append(f"Missing style: {style_name}")
        
        # Validate each style definition
        for style_name, style_def in skel.items():
            if not isinstance(style_def, dict):
                validation["errors"].append(f"Style '{style_name}' must be a dictionary")
                validation["is_valid"] = False
                continue
            
            # Check for required style properties
            required_props = ["font", "size", "color"]
            for prop in required_props:
                if prop not in style_def:
                    validation["warnings"].append(f"Style '{style_name}' missing '{prop}' property")
            
            # Validate font property
            if "font" in style_def:
                font = style_def["font"]
                if not isinstance(font, str):
                    validation["errors"].append(f"Style '{style_name}' font must be a string")
                    validation["is_valid"] = False
                elif len(font) > 100:
                    validation["warnings"].append(f"Style '{style_name}' font name is very long")
            
            # Validate size property
            if "size" in style_def:
                size = style_def["size"]
                if not isinstance(size, (int, float)):
                    validation["errors"].append(f"Style '{style_name}' size must be a number")
                    validation["is_valid"] = False
                elif size < 1 or size > 1000:
                    validation["warnings"].append(f"Style '{style_name}' size ({size}) is unusual")
            
            # Validate color property
            if "color" in style_def:
                color = style_def["color"]
                if not isinstance(color, str):
                    validation["errors"].append(f"Style '{style_name}' color must be a string")
                    validation["is_valid"] = False
                elif not color.startswith("#") or len(color) != 7:
                    validation["warnings"].append(f"Style '{style_name}' color format may be invalid")
            
            # Check for potential security issues
            for key, value in style_def.items():
                if isinstance(value, str):
                    # Check for potential script injection
                    if "<script" in value.lower() or "javascript:" in value.lower():
                        validation["errors"].append(f"Style '{style_name}' contains potentially dangerous content")
                        validation["is_valid"] = False
                    
                    # Check for extremely long values (potential DoS)
                    if len(value) > 1000:
                        validation["warnings"].append(f"Style '{style_name}' '{key}' is very long ({len(value)} characters)")
        
        # Check for excessive number of styles
        if len(skel) > 100:
            validation["warnings"].append(f"Style skeleton has many styles ({len(skel)}), consider simplifying")
        
        return validation
        
    except Exception as e:
        validation["is_valid"] = False
        validation["errors"].append(f"Validation error: {str(e)}")
        return validation

def get_drive_service_oauth():
    """Get Google Drive service using OAuth credentials"""
    try:
        from googleapiclient.discovery import build
        creds = get_google_credentials()
        if not creds:
            return None
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception:
        return None

def _check_google_drive_connection() -> bool:
    """Check if Google Drive is properly connected and authenticated with comprehensive validation."""
    try:
        # Check if credentials file exists and is readable
        creds_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "backend/google_credentials.json")
        if not os.path.exists(creds_file):
            return False
        
        if not os.access(creds_file, os.R_OK):
            return False
        
        # Try to load and validate credentials file structure
        try:
            with open(creds_file, 'r') as f:
                creds_data = json.load(f)
            
            # Validate required fields
            required_fields = ['type', 'project_id', 'private_key', 'client_email']
            if not all(field in creds_data for field in required_fields):
                return False
            
            # Validate credential type
            if creds_data.get('type') != 'service_account':
                return False
            
            # Validate private key format
            private_key = creds_data.get('private_key', '')
            if not private_key.startswith('-----BEGIN PRIVATE KEY-----'):
                return False
            
        except (json.JSONDecodeError, KeyError, ValueError):
            return False
        
        # Check OAuth credentials
        creds = get_google_credentials()
        if not creds:
            return False
        
        # Check if credentials are valid and not expired
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(requests.Request())
                except Exception:
                    return False
            else:
                return False
        
        # Test the connection by trying to list a few files
        service = get_drive_service_oauth()
        if not service:
            return False
        
        # Try a simple API call to verify connection
        results = service.files().list(pageSize=1, fields="files(id, name)").execute()
        return True
        
    except Exception:
        return False
# Memory limits for shared state
MAX_UPLOADED_FILES = 1000
MAX_JOBS_SNAPSHOT = 5000
MAX_ACTIVE_TASKS = 100

# Request and processing limits
MAX_REQUEST_SIZE = 50 * 1024 * 1024  # 50MB
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_REQUEST_TIMEOUT = 300  # 5 minutes
MAX_REFINEMENT_PASSES = 10
MAX_HEURISTICS_SIZE = 1024 * 1024  # 1MB for heuristics dict

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/refiner.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Standardized error response utilities
class APIError(Exception):
    """Custom exception for API errors with standardized format"""
    def __init__(self, message: str, status_code: int = 500, error_code: str = None, details: Dict[str, Any] = None):
        self.message = message
        self.status_code = status_code
        self.error_code = error_code or f"ERROR_{status_code}"
        self.details = details or {}
        super().__init__(self.message)

def _make_json_safe(obj: Any) -> Any:
    """Recursively convert objects to JSON-serializable forms."""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        pass
    # Exceptions -> string
    if isinstance(obj, BaseException):
        return str(obj)
    # Dicts
    if isinstance(obj, dict):
        return {str(_make_json_safe(k)): _make_json_safe(v) for k, v in obj.items()}
    # Lists/Tuples/Sets
    if isinstance(obj, (list, tuple, set)):
        return [_make_json_safe(v) for v in obj]
    # Fallback to string repr
    return repr(obj)

def create_error_response(message: str, status_code: int = 500, error_code: str = None, details: Dict[str, Any] = None) -> JSONResponse:
    """Create a standardized error response (always JSON-serializable)."""
    error_data = {
        "error": message,
        "status_code": status_code,
        "error_code": error_code or f"ERROR_{status_code}",
        "timestamp": datetime.now().isoformat(),
        "details": details or {}
    }
    safe_data = _make_json_safe(error_data)
    return JSONResponse(safe_data, status_code=status_code)

def handle_api_error(func):
    """Deprecated: Prefer global exception handlers to avoid FastAPI 422 on wrappers."""
    return func

from backend.core.diff_utils import generate_diff, format_change_for_api, format_statistics_for_api
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Only for static typing; avoided at runtime to prevent heavy imports on serverless cold start
    from backend.pipeline_service import RefinementPipeline  # type: ignore
    from backend.language_model import OpenAIModel  # type: ignore
else:
    RefinementPipeline = None  # runtime placeholder
    OpenAIModel = None  # runtime placeholder
    analytics_store = None  # runtime placeholder
from backend.storage import LocalSink, DriveSink  # ⭐ REAL STORAGE
from backend.prompt_schema import ADVANCED_COMMANDS  # ⭐ REAL SCHEMA
from backend.core.database import list_jobs as db_list_jobs

# Lazy/guarded imports for heavy modules to prevent serverless cold-start crashes
ConversationalRefiner = None
RefinementMemory = None
refine_with_feedback = None
if os.getenv("LIGHTWEIGHT_MODE", "").strip() != "1":
    try:
        from backend.conversation_refiner import ConversationalRefiner
    except Exception as _e:
        logger.warning(f"Conversation refiner unavailable: {_e}")
    try:
        from backend.Andy_speech import RefinementMemory, refine_with_feedback
    except Exception as _e:
        logger.warning(f"Refinement memory/feedback unavailable: {_e}")
else:
    # Provide lightweight fallbacks to satisfy type usage
    class RefinementMemory:  # minimal stub
        def __init__(self):
            self.history = []
        def log_pass(self, *args, **kwargs):
            self.history.append({"args": args, "kwargs": kwargs})
        def last_output(self):
            return None
        def last_score(self):
            return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: eager init pipeline to avoid lazy-load lock hang
    if os.getenv("LIGHTWEIGHT_MODE", "").strip() == "1":
        logger.info("LIGHTWEIGHT_MODE=1: Skipping pipeline initialization at startup")
    else:
        logger.info("Initializing pipeline at startup...")
        try:
            get_pipeline()
            logger.info("Pipeline initialized successfully")
        except Exception as e:
            logger.error(f"Pipeline init error: {e}")
    # Startup: optionally launch periodic cleanup in background
    cleanup_task = None
    if os.getenv("DISABLE_CLEANUP", "").strip() != "1":
        cleanup_task = asyncio.create_task(periodic_cleanup())
        logger.info("Periodic cleanup task started")
    try:
        yield
    finally:
        # Shutdown: cancel background task
        if cleanup_task:
            logger.info("Shutting down cleanup task...")
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled successfully")

app = FastAPI(title="Turbo Alan Refiner API", version="3.0.0", lifespan=lifespan)
# Global flag to track database status
database_initialized = False

# Root route (optional) for uptime checks
@app.get("/")
async def root():
    return {"status": "ok", "service": "Turbo Alan Refiner API"}

# Global exception handlers for standardized errors
@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    logger.warning(f"APIError: {exc.message}")
    return create_error_response(exc.message, exc.status_code, exc.error_code, exc.details)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Mirror FastAPI structure but standardized
    details = exc.errors()
    return create_error_response("Validation error", 422, "REQUEST_VALIDATION_ERROR", {"detail": details})

@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return create_error_response("Internal server error", 500, "INTERNAL_ERROR")

# Ensure DB tables exist
try:
    init_database()
    database_initialized = True
    print("✅ Database initialized successfully")
except Exception as e:
    print(f"❌ Database initialization failed: {e}")
    database_initialized = False
    # In production, you might want to exit here
    # import sys; sys.exit(1)
    # For now, log the error but continue
    from backend.logger import log_exception
    log_exception("DATABASE_INIT_FAILED", e)

# Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    # Allow disabling rate limit in debug to isolate hangs
    if os.getenv("DISABLE_RATE_LIMIT", "").strip() == "1":
        return await call_next(request)
    try:
        # Get client IP with proper header checking
        client_ip = request.client.host if request.client else "unknown"
        
        # Check for forwarded IP headers (common in proxy setups)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first IP in the chain (original client)
            client_ip = forwarded_for.split(",")[0].strip()
        
        # Additional header checks
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            client_ip = real_ip.strip()
        
        # Validate IP format (basic check)
        if not client_ip or client_ip == "unknown" or len(client_ip) > 45:  # IPv6 max length
            client_ip = "unknown"
        
        # Special handling for file uploads - stricter rate limiting
        is_file_upload = request.url.path.startswith("/files/upload")
        upload_rate_limit = 10  # Lower limit for uploads
        
        # Thread-safe rate limiting
        with rate_limit_lock:
            # Clean old entries
            current_time = time.time()
            for ip in list(rate_limit_storage.keys()):
                if current_time - rate_limit_storage[ip]["last_reset"] > RATE_LIMIT_WINDOW:
                    del rate_limit_storage[ip]
            
            # Check rate limit with different limits for different endpoints
            max_requests = upload_rate_limit if is_file_upload else RATE_LIMIT_MAX_REQUESTS
            
            if client_ip in rate_limit_storage:
                if rate_limit_storage[client_ip]["count"] >= max_requests:
                    return JSONResponse(
                        {"error": "Rate limit exceeded", "retry_after": RATE_LIMIT_WINDOW, "limit_type": "upload" if is_file_upload else "general"}, 
                        status_code=429
                    )
                rate_limit_storage[client_ip]["count"] += 1
            else:
                rate_limit_storage[client_ip] = {
                    "count": 1,
                    "last_reset": current_time,
                    "is_upload": is_file_upload
                }
        
        return await call_next(request)
    except Exception as e:
        from backend.logger import log_exception
        log_exception("RATE_LIMIT_ERROR", e)
        return await call_next(request)

# Simple API key middleware (fail-closed): set BACKEND_API_KEY to enforce
@app.middleware("http")
async def api_key_guard(request, call_next):
    # Allow disabling API key in debug to isolate hangs
    if os.getenv("DISABLE_API_KEY", "").strip() == "1":
        return await call_next(request)
    try:
        required = os.getenv("BACKEND_API_KEY", "").strip()
        
        # If no API key is configured, allow all requests (local development)
        if not required:
            return await call_next(request)
        
        # Allow health check without key even when API key is configured
        if request.url.path.startswith("/health"):
            return await call_next(request)
            
        provided = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if provided and provided.strip() == required:
            return await call_next(request)
        print(f"API_KEY_GUARD: REJECTED {request.method} {request.url.path} - provided: {provided[:20] if provided else 'None'}... required: {required[:20] if required else 'None'}...")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    except Exception as e:
        print(f"API_KEY_GUARD: EXCEPTION {request.method} {request.url.path} - {e}")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

# In-memory task registry for background jobs
active_tasks: Dict[str, asyncio.Task] = {}

# Rate limiting storage with thread safety
rate_limit_storage: Dict[str, Dict[str, Any]] = {}
rate_limit_lock = threading.RLock()
# Track task creation times for reliable eviction of oldest tasks
active_task_times: Dict[str, float] = {}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 100  # requests per window

async def _run_job_background(req: RefinementRequest, job_id: str) -> None:
    try:
        # Set task timeout to prevent infinite processing
        timeout_seconds = 300  # 5 minutes max per job
        
        # Broadcast initial job id for WS clients listening
        head = {'type': 'job', 'jobId': job_id}
        try:
            await ws_manager.broadcast(job_id, head)  # type: ignore
        except Exception as e:
            from backend.logger import log_exception
            log_exception("WS_BROADCAST_ERROR", e)
        
        # Consume stream to drive processing while updating DB via _refine_stream side-effects
        async with asyncio.timeout(timeout_seconds):
            async for _ in _refine_stream(req, job_id):
                # stream frames ignored; DB + WS already handled
                await asyncio.sleep(0)  # yield control
                
    except asyncio.TimeoutError:
        # Job timed out
        from backend.logger import log_exception
        log_exception("JOB_TIMEOUT", f"Job {job_id} timed out after {timeout_seconds} seconds")
        safe_upsert_job(job_id, {"status": "timeout", "error": f"Job timed out after {timeout_seconds} seconds"})
        safe_jobs_snapshot_set(job_id, {"type": "timeout", "jobId": job_id, "error": "Job timed out"})
        
    except Exception as e:
        # Log the error and update job status
        from backend.logger import log_exception
        log_exception("BACKGROUND_JOB_ERROR", e)
        safe_upsert_job(job_id, {"status": "failed", "error": str(e)})
        safe_jobs_snapshot_set(job_id, {"type": "error", "jobId": job_id, "error": str(e)})
        
    finally:
        # Always clean up the task, regardless of success/failure/timeout
        safe_active_tasks_del(job_id)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CORS_ALLOW_ORIGINS", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request timeout middleware
@app.middleware("http")
async def request_timeout_middleware(request, call_next):
    try:
        # Set a timeout for the request processing
        async with asyncio.timeout(MAX_REQUEST_TIMEOUT):
            response = await call_next(request)
            return response
    except asyncio.TimeoutError:
        return JSONResponse(
            {"error": "Request timeout", "message": f"Request exceeded {MAX_REQUEST_TIMEOUT} seconds"},
            status_code=408
        )
    except Exception as e:
        from backend.logger import log_exception
        log_exception("REQUEST_TIMEOUT_ERROR", e)
        return JSONResponse(
            {"error": "Internal server error", "message": "Request processing failed"},
            status_code=500
        )

# Minimal request logging (debug-only)
@app.middleware("http")
async def request_logger(request, call_next):
    if os.getenv("DEBUG_REQUEST_LOG", "").strip() == "1":
        print(f"REQ {request.method} {request.url.path}")
    response = await call_next(request)
    if os.getenv("DEBUG_REQUEST_LOG", "").strip() == "1":
        print(f"RES {request.method} {request.url.path} -> {response.status_code}")
    return response

# Enhanced Pydantic models
class RefinementRequest(BaseModel):
    files: List[Dict[str, Any]]
    passes: int = 1
    entropy_level: str = "medium"
    output_settings: Dict[str, Any] = {"type": "local", "path": "./output"}
    heuristics: Dict[str, Any] = {}
    settings: Dict[str, Any] = {}
    user_id: str = "default"
    use_memory: bool = True
    aggressiveness: str = "Auto"
    earlyStop: bool = True
    scannerRisk: int = 15
    keywords: List[str] = []
    schemaLevels: Dict[str, Any] = {}
    strategy_mode: str = "model"
    entropy: Dict[str, Any] = {}
    formatting_safeguards: Dict[str, Any] = {}
    history_analysis: Dict[str, Any] = {}
    refiner_dry_run: bool = False
    annotation_mode: Dict[str, Any] = {}
    
    def __init__(self, **data):
        super().__init__(**data)
        # Normalize and be lenient to avoid 422s from common client payloads
        try:
            # user_id: fallback to default if invalid
            if not self.user_id or not isinstance(self.user_id, str):
                self.user_id = "default"
            if len(self.user_id) < 3:
                self.user_id = "user_" + self.user_id
            if len(self.user_id) > 50:
                self.user_id = self.user_id[:50]

            # passes: clamp
            if not isinstance(self.passes, int):
                try:
                    self.passes = int(self.passes)
                except Exception:
                    self.passes = 1
            self.passes = max(1, min(self.passes, MAX_REFINEMENT_PASSES))

            # entropy_level: normalize
            level_map = {"balanced": "medium", "med": "medium", "mid": "medium"}
            ent = (self.entropy_level or "medium").lower()
            ent = level_map.get(ent, ent)
            if ent not in ["low", "medium", "high"]:
                ent = "medium"
            self.entropy_level = ent

            # aggressiveness: case-insensitive
            ag = (self.aggressiveness or "Auto").strip()
            ag_norm = ag.lower()
            if ag_norm in ("auto", "low", "medium", "high"):
                self.aggressiveness = ag_norm.capitalize()
            else:
                self.aggressiveness = "Auto"

            # scannerRisk: coerce & clamp
            try:
                self.scannerRisk = int(self.scannerRisk)
            except Exception:
                self.scannerRisk = 15
            self.scannerRisk = max(0, min(self.scannerRisk, 100))

            # strategy_mode: normalize
            sm = (self.strategy_mode or "model").lower()
            if sm not in ["model", "manual", "hybrid"]:
                sm = "model"
            self.strategy_mode = sm

            # files: ensure list with minimal required keys; don't hard fail
            if not isinstance(self.files, list):
                self.files = []
            # Trim to a reasonable max
            if len(self.files) > 50:
                self.files = self.files[:50]
            # Ensure each file has id/name/type defaults
            normalized_files = []
            for idx, f in enumerate(self.files):
                if not isinstance(f, dict):
                    continue
                fid = f.get("id") or f.get("file_id") or f"file_{idx}"
                name = f.get("name") or f.get("filename") or fid
                ftype = f.get("type") or "local"
                normalized_files.append({**f, "id": fid, "name": name, "type": ftype})
            self.files = normalized_files

            # size guards for heuristics/settings
            if len(str(self.heuristics)) > MAX_HEURISTICS_SIZE:
                self.heuristics = {}
            if len(str(self.settings)) > MAX_HEURISTICS_SIZE:
                self.settings = {}

            # output_settings defaults
            if not isinstance(self.output_settings, dict):
                self.output_settings = {"type": "local", "path": "./output"}
            if self.output_settings.get("type") not in ("local", "drive"):
                self.output_settings["type"] = "local"
            if self.output_settings.get("type") == "local":
                path = self.output_settings.get("path") or self.output_settings.get("dir") or "./output"
                if not isinstance(path, str):
                    path = "./output"
                # sanitize
                if ".." in path or path.startswith("/") or "\\" in path:
                    path = "./output"
                self.output_settings["path"] = path
        except Exception:
            # Never fail constructor; rely on endpoint logic to handle deeper errors
            pass

class ChatRequest(BaseModel):
    message: str
    flags: Dict[str, Any] = {}
    schema_levels: Dict[str, Any] = {}
    user_id: str = "default"
    use_memory: bool = True
    current_file: Optional[str] = None
    current_pass: Optional[int] = None
    recent_changes: List[str] = []
    
    def __init__(self, **data):
        super().__init__(**data)
        # Validate user_id
        if not self.user_id or len(self.user_id) < 3 or len(self.user_id) > 50:
            raise ValueError("user_id must be between 3 and 50 characters")
        if not self.user_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("user_id must contain only alphanumeric characters, underscores, and hyphens")

class MemoryRequest(BaseModel):
    user_id: str
    action: str
    original_text: str = ""
    refined_text: str = ""
    score: float = None
    notes: List[str] = []
    heuristics: Dict[str, Any] = {}
    flags: Dict[str, Any] = {}

class FileUploadResponse(BaseModel):
    file_id: str
    filename: str
    size: int
    temp_path: str
    file_type: str
    mime_type: str

class FileDownloadRequest(BaseModel):
    file_id: str
    output_format: str = "original"  # "original", "txt", "docx", "md"

# Global instances with dependency injection and thread safety
_settings = None
_pipeline = None
_model = None
_global_lock = threading.RLock()  # Use RLock to allow reentrant calls

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _global_lock:
            if _settings is None:  # Double-checked locking
                _settings = Settings.load()
    return _settings

def get_model() -> "OpenAIModel":
    global _model
    if _model is None:
        with _global_lock:
            if _model is None:  # Double-checked locking
                settings = get_settings()
                # Lazy import to avoid pulling heavy deps at module import time
                from backend.language_model import OpenAIModel as _OpenAIModel
                _model = _OpenAIModel(settings.openai_api_key, model=settings.openai_model)
    return _model

def get_pipeline() -> "RefinementPipeline":
    global _pipeline
    if _pipeline is None:
        with _global_lock:
            if _pipeline is None:  # Double-checked locking
                settings = get_settings()
                model = get_model()
                from backend.pipeline_service import RefinementPipeline as _RefinementPipeline
                _pipeline = _RefinementPipeline(settings, model)
    return _pipeline

# Enhanced memory management with persistence
class MemoryManager:
    def __init__(self):
        self.user_memories: Dict[str, RefinementMemory] = {}
    
    def get_memory(self, user_id: str) -> RefinementMemory:
        if user_id not in self.user_memories:
            self.user_memories[user_id] = RefinementMemory()
        return self.user_memories[user_id]
    
    def log_refinement_pass(self, user_id: str, original: str, refined: str, score: float = None, notes: List[str] = None):
        memory = self.get_memory(user_id)
        memory.log_pass(original, refined, score, notes)
    
    def get_memory_context(self, user_id: str) -> Dict[str, Any]:
        memory = self.get_memory(user_id)
        return {
            "total_passes": len(memory.history),
            "last_output": memory.last_output(),
            "last_score": memory.last_score(),
            "has_history": bool(memory.history),
            "recent_passes": memory.history[-5:] if memory.history else []
        }

memory_manager = MemoryManager()

# File storage for uploaded files with thread safety
uploaded_files: Dict[str, Dict[str, Any]] = {}
jobs_snapshot: Dict[str, Dict[str, Any]] = {}
shared_state_lock = threading.Lock()
MAX_UPLOADED_FILES = 1000  # Prevent memory exhaustion
MAX_JOBS_SNAPSHOT = 500   # Prevent memory exhaustion

# Thread-safe access methods for shared state
def safe_uploaded_files_get(file_id: str) -> Optional[Dict[str, Any]]:
    """Thread-safe get from uploaded_files"""
    with shared_state_lock:
        return uploaded_files.get(file_id)

def safe_uploaded_files_set(file_id: str, file_info: Dict[str, Any]) -> None:
    """Thread-safe set to uploaded_files with size limits and LRU eviction."""
    with shared_state_lock:
        # Enforce size limits with LRU eviction
        if len(uploaded_files) >= MAX_UPLOADED_FILES:
            # Remove oldest files (LRU eviction)
            oldest_files = sorted(uploaded_files.items(), key=lambda x: x[1].get("uploaded_at", 0))
            for old_id, old_info in oldest_files[:MAX_UPLOADED_FILES // 2]:
                try:
                    if os.path.exists(old_info.get("temp_path", "")):
                        os.unlink(old_info["temp_path"])
                except Exception:
                    pass
                del uploaded_files[old_id]
        
        uploaded_files[file_id] = file_info

def safe_uploaded_files_del(file_id: str) -> bool:
    """Thread-safe delete from uploaded_files"""
    with shared_state_lock:
        if file_id in uploaded_files:
            del uploaded_files[file_id]
            return True
        return False

def safe_jobs_snapshot_set(job_id: str, job_info: Dict[str, Any]) -> None:
    """Thread-safe set to jobs_snapshot with size limits and LRU eviction."""
    with shared_state_lock:
        # Enforce size limits with LRU eviction
        if len(jobs_snapshot) >= MAX_JOBS_SNAPSHOT:
            # Remove oldest jobs (LRU eviction)
            oldest_jobs = sorted(jobs_snapshot.items(), key=lambda x: x[1].get("timestamp", 0))
            for old_id in [job_id for job_id, _ in oldest_jobs[:MAX_JOBS_SNAPSHOT // 2]]:
                del jobs_snapshot[old_id]
        
        jobs_snapshot[job_id] = job_info

def safe_jobs_snapshot_get(job_id: str) -> Optional[Dict[str, Any]]:
    """Thread-safe get from jobs_snapshot"""
    with shared_state_lock:
        return jobs_snapshot.get(job_id)

def safe_active_tasks_set(job_id: str, task: asyncio.Task) -> None:
    """Thread-safe set to active_tasks with size limits."""
    with shared_state_lock:
        # Enforce size limits
        if len(active_tasks) >= MAX_ACTIVE_TASKS:
            # Evict oldest tasks using tracked timestamps
            sorted_ids = sorted(active_task_times.items(), key=lambda kv: kv[1])
            evict_ids = [jid for jid, _ in sorted_ids[:max(1, MAX_ACTIVE_TASKS // 2)]]
            for old_id in evict_ids:
                active_tasks.pop(old_id, None)
                active_task_times.pop(old_id, None)
        
        active_tasks[job_id] = task
        active_task_times[job_id] = time.time()

def safe_active_tasks_del(job_id: str) -> bool:
    """Thread-safe delete from active_tasks"""
    with shared_state_lock:
        if job_id in active_tasks:
            del active_tasks[job_id]
            return True
        return False
# Database operation wrapper with proper error handling
def safe_upsert_job(job_id: str, job_data: Dict[str, Any]) -> bool:
    """Safely upsert job with proper error handling"""
    try:
        upsert_job(job_id, job_data)
        return True
    except Exception as e:
        from backend.logger import log_exception
        log_exception("DATABASE_UPSERT_ERROR", e)
        # Store in memory fallback
        safe_jobs_snapshot_set(job_id, {**job_data, "db_error": str(e), "timestamp": time.time()})
        return False

def safe_get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Safely get job with fallback to memory"""
    try:
        result = get_job(job_id)
        if result:
            return result
    except Exception as e:
        from backend.logger import log_exception
        log_exception("DATABASE_GET_ERROR", e)
    
    # Fallback to memory
    return safe_jobs_snapshot_get(job_id)

async def cleanup_old_files():
    """Clean up old uploaded files to prevent memory leaks"""
    try:
        current_time = time.time()
        max_age = 24 * 60 * 60  # 24 hours
        
        files_to_remove = []
        with shared_state_lock:
            for file_id, file_info in uploaded_files.items():
                if current_time - file_info.get("uploaded_at", 0) > max_age:
                    files_to_remove.append(file_id)
        
        for file_id in files_to_remove:
            try:
                file_info = safe_uploaded_files_get(file_id)
                if file_info and os.path.exists(file_info["temp_path"]):
                    os.unlink(file_info["temp_path"])
                safe_uploaded_files_del(file_id)
                logger.debug(f"Cleaned up old file: {file_id}")
            except (OSError, IOError) as e:
                logger.warning(f"Failed to clean up file {file_id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error cleaning up file {file_id}: {e}")
        
        if files_to_remove:
            logger.info(f"Cleaned up {len(files_to_remove)} old uploaded files")
            
    except Exception as e:
        logger.error(f"File cleanup task error: {e}")

# (migrated to lifespan handler)

async def periodic_cleanup():
    """Run cleanup every 15 minutes to prevent memory leaks"""
    while True:
        try:
            await asyncio.sleep(900)  # Wait 15 minutes
            await cleanup_old_files()
            await cleanup_stale_tasks()
            await cleanup_memory_usage()
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}")

async def cleanup_memory_usage():
    """Clean up memory usage when limits are exceeded"""
    try:
        with shared_state_lock:
            # Check if we're approaching limits
            if len(uploaded_files) > MAX_UPLOADED_FILES * 0.8:  # 80% of limit
                logger.warning(f"Uploaded files approaching limit: {len(uploaded_files)}/{MAX_UPLOADED_FILES}")
                # Remove oldest files
                sorted_files = sorted(uploaded_files.items(), key=lambda x: x[1].get("uploaded_at", 0))
                files_to_remove = [fid for fid, _ in sorted_files[:len(uploaded_files) // 4]]  # Remove 25%
                
                for file_id in files_to_remove:
                    try:
                        file_info = uploaded_files[file_id]
                        if os.path.exists(file_info["temp_path"]):
                            os.unlink(file_info["temp_path"])
                        del uploaded_files[file_id]
                    except Exception as e:
                        logger.warning(f"Failed to remove file {file_id}: {e}")
                
                logger.info(f"Emergency cleanup removed {len(files_to_remove)} files")
            
            if len(jobs_snapshot) > MAX_JOBS_SNAPSHOT * 0.8:  # 80% of limit
                logger.warning(f"Jobs snapshot approaching limit: {len(jobs_snapshot)}/{MAX_JOBS_SNAPSHOT}")
                # Remove oldest jobs
                sorted_jobs = sorted(jobs_snapshot.items(), key=lambda x: x[1].get("timestamp", 0))
                jobs_to_remove = [jid for jid, _ in sorted_jobs[:len(jobs_snapshot) // 4]]  # Remove 25%
                
                for job_id in jobs_to_remove:
                    del jobs_snapshot[job_id]
                
                logger.info(f"Emergency cleanup removed {len(jobs_to_remove)} job snapshots")
                
    except Exception as e:
        logger.error(f"Memory cleanup error: {e}")

async def cleanup_stale_tasks():
    """Clean up stale background tasks"""
    try:
        with shared_state_lock:
            stale_tasks = []
            for job_id, task in active_tasks.items():
                if task.done() or task.cancelled():
                    stale_tasks.append(job_id)
            
            for job_id in stale_tasks:
                del active_tasks[job_id]
                active_task_times.pop(job_id, None)
                # Mark job as failed if it was stale
                safe_upsert_job(job_id, {"status": "failed", "error": "Task became stale"})
            
            if stale_tasks:
                print(f"Cleaned up {len(stale_tasks)} stale background tasks")
                
    except Exception as e:
        from backend.logger import log_exception
        log_exception("STALE_TASK_CLEANUP_ERROR", e)

# Job storage for analytics (in production, use proper database)
_jobs_storage: Dict[str, Dict[str, Any]] = {}

@app.get("/health")
async def health() -> Dict[str, Any]:
    """Comprehensive health check endpoint with fail-closed approach."""
    # Start pessimistic - assume degraded until proven otherwise
    health_status = {
        "status": "degraded",
        "version": "3.0.0", 
        "timestamp": time.time(),
        "features": ["real_pipeline", "memory_system", "enhanced_chat", "real_file_processing", "storage_integration"],
        "checks": {}
    }
    
    all_checks_passed = True
    
    # Check database
    try:
        if not database_initialized:
            health_status["checks"]["database"] = "not_initialized"
            all_checks_passed = False
        else:
            # Test database connectivity
            test_jobs = list_jobs(1)
            health_status["checks"]["database"] = "ok"
    except Exception as e:
        health_status["checks"]["database"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check OpenAI API key
    try:
        settings = get_settings()
        if settings.openai_api_key:
            health_status["checks"]["openai"] = "configured"
        else:
            health_status["checks"]["openai"] = "not_configured"
            all_checks_passed = False
    except Exception as e:
        health_status["checks"]["openai"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check Google Drive connection
    try:
        drive_connected = _check_google_drive_connection()
        health_status["checks"]["google_drive"] = "connected" if drive_connected else "not_connected"
    except Exception as e:
        health_status["checks"]["google_drive"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check active tasks
    try:
        with shared_state_lock:
            health_status["checks"]["active_tasks"] = len(active_tasks)
    except Exception as e:
        health_status["checks"]["active_tasks"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check rate limiting
    try:
        with rate_limit_lock:
            health_status["checks"]["rate_limited_ips"] = len(rate_limit_storage)
    except Exception as e:
        health_status["checks"]["rate_limited_ips"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check file storage
    try:
        output_dir = "./output"
        if os.path.exists(output_dir) and os.access(output_dir, os.W_OK):
            health_status["checks"]["file_storage"] = "ok"
        else:
            health_status["checks"]["file_storage"] = "error: output directory not writable"
            all_checks_passed = False
    except Exception as e:
        health_status["checks"]["file_storage"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Check memory usage
    try:
        with shared_state_lock:
            health_status["checks"]["uploaded_files_count"] = len(uploaded_files)
            health_status["checks"]["jobs_snapshot_count"] = len(jobs_snapshot)
    except Exception as e:
        health_status["checks"]["memory_usage"] = f"error: {str(e)}"
        all_checks_passed = False
    
    # Only mark as "ok" if ALL checks passed
    if all_checks_passed:
        health_status["status"] = "ok"
    
    return health_status

@app.get("/health/fast")
async def health_fast() -> Dict[str, Any]:
    """Ultra-fast health endpoint for debugging hangs (no checks)."""
    return {"status": "ok", "ts": time.time()}

@app.get("/jobs/{job_id}/status")
async def job_status(job_id: str) -> Dict[str, Any]:
    """Return the latest progress snapshot for a jobId (polling fallback with DB)."""
    row = get_job(job_id)
    if row:
        return {"jobId": job_id, **row}
    snap = jobs_snapshot.get(job_id)
    return snap or {"jobId": job_id, "status": "unknown"}

@app.get("/jobs")
async def jobs_list() -> Dict[str, Any]:
    return {"jobs": list_jobs(100)}

# Mount websocket router (optional)
try:
    from .websocket_progress import router as ws_router, manager as ws_manager
    app.include_router(ws_router)
except Exception:
    # Fallback WebSocket manager for when websocket_progress is not available
    class DummyWebSocketManager:
        async def broadcast(self, job_id: str, message: dict):
            pass  # No-op when WebSocket is not available
    
    ws_manager = DummyWebSocketManager()

@app.get("/settings")
async def get_settings_endpoint() -> Dict[str, Any]:
    s = get_settings()
    return {
        "openaiApiKey": "sk-***" if s.openai_api_key else "",
        "openaiModel": s.openai_model,
        "targetScannerRisk": s.target_scanner_risk,
        "minWordRatio": s.min_word_ratio,
        "googleDriveConnected": _check_google_drive_connection(),
        "defaultOutputLocation": "local",
        "supportedFileTypes": [".txt", ".docx", ".md"],
        "schemaDefaults": {
            "microstructure_control": 2,
            "macrostructure_analysis": 1,
            "anti_scanner_techniques": 3,
            "entropy_management": 2,
            "semantic_tone_tuning": 1,
            "formatting_safeguards": 3,
            "refiner_control": 2,
            "history_analysis": 1,
            "annotation_mode": 0,
            "humanize_academic": 2,
        },
        "strategyMode": os.getenv("STRATEGY_MODE", "model"),
        "availableSchemas": list(ADVANCED_COMMANDS.keys()),
    }

@app.post("/settings")
async def save_settings_endpoint(req: Request):
    try:
        data = await req.json()
        
        # Get current settings
        settings = get_settings()
        
        # Update settings with provided data
        if "openaiApiKey" in data:
            settings.openai_api_key = data["openaiApiKey"]
        if "openaiModel" in data:
            settings.openai_model = data["openaiModel"]
        if "targetScannerRisk" in data:
            settings.target_scanner_risk = data["targetScannerRisk"]
        if "minWordRatio" in data:
            settings.min_word_ratio = data["minWordRatio"]
        
        # Save settings to environment and config file
        try:
            # Update environment variables
            if settings.openai_api_key:
                os.environ["OPENAI_API_KEY"] = settings.openai_api_key
            if settings.openai_model:
                os.environ["OPENAI_MODEL"] = settings.openai_model
            if settings.target_scanner_risk:
                os.environ["TARGET_SCANNER_RISK"] = str(settings.target_scanner_risk)
            if settings.min_word_ratio:
                os.environ["MIN_WORD_RATIO"] = str(settings.min_word_ratio)
            
            # Persist settings to config file
            config_path = os.path.join(os.getcwd(), "config.json")
            config_data = {
                "openai_api_key": settings.openai_api_key,
                "openai_model": settings.openai_model,
                "target_scanner_risk": settings.target_scanner_risk,
                "min_word_ratio": settings.min_word_ratio,
                "aggressiveness": settings.aggressiveness,
                "random_seed": settings.random_seed,
                "batch_pace_delay_s": settings.batch_pace_delay_s
            }
            
            with open(config_path, 'w') as f:
                json.dump(config_data, f, indent=2)
            
            # Reset global instances to pick up new settings
            global _settings, _model, _pipeline
            with _global_lock:
                _settings = None
                _model = None
                _pipeline = None
            
        except Exception as e:
            from backend.logger import log_exception
            log_exception("SETTINGS_SAVE_ERROR", e)
            return JSONResponse({"error": f"Failed to save settings: {str(e)}"}, status_code=500)
        
        return {"success": True, "message": "Settings saved successfully"}
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

@app.get("/history/profile")
async def history_profile() -> Dict[str, float]:
    return derive_history_profile()

# Enhanced file upload with real file type detection
@app.post("/files/upload")
@handle_api_error
async def upload_file(file: UploadFile = File(...)) -> FileUploadResponse:
    if not file.filename:
        raise APIError("No filename provided", 400, "MISSING_FILENAME")
    
    # Validate filename for security - comprehensive path traversal protection
    if not file.filename or len(file.filename) > 255:
        raise APIError("Invalid filename", 400, "INVALID_FILENAME")
    
    # Normalize path and check for traversal attempts
    import os.path
    normalized_path = os.path.normpath(file.filename)
    if normalized_path != file.filename or '..' in normalized_path or normalized_path.startswith('/') or normalized_path.startswith('\\'):
        raise APIError("Invalid filename - path traversal detected", 400, "PATH_TRAVERSAL_DETECTED")
    
    # Additional security checks
    dangerous_chars = ['<', '>', ':', '"', '|', '?', '*', '\x00']
    if any(char in file.filename for char in dangerous_chars):
        raise APIError("Invalid filename - contains dangerous characters", 400, "DANGEROUS_CHARACTERS")
    
    filename_lower = file.filename.lower()
    if filename_lower.endswith('.txt'):
        file_type, mime_type = "txt", "text/plain"
    elif filename_lower.endswith('.docx'):
        file_type, mime_type = "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif filename_lower.endswith('.doc'):
        file_type, mime_type = "doc", "application/msword"
    elif filename_lower.endswith('.pdf'):
        file_type, mime_type = "pdf", "application/pdf"
    elif filename_lower.endswith('.md'):
        file_type, mime_type = "md", "text/markdown"
    else:
        raise APIError("Unsupported file type. Supported: .txt, .docx, .doc, .pdf, .md", 400, "UNSUPPORTED_FILE_TYPE")
    
    # Validate file size BEFORE reading content to prevent memory exhaustion
    
    # Check reported size first (if available)
    if file.size and file.size > MAX_FILE_SIZE:
        raise APIError(f"File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB", 400, "FILE_TOO_LARGE")
    
    # Read content in chunks to prevent memory exhaustion
    content = b""
    chunk_size = 8192  # 8KB chunks
    total_read = 0
    
    try:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > MAX_FILE_SIZE:
                raise APIError(f"File content too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB", 400, "FILE_CONTENT_TOO_LARGE")
            content += chunk
    except Exception as e:
        raise APIError(f"Error reading file: {str(e)}", 400, "FILE_READ_ERROR")
    
    # Validate file content matches declared type
    if not validate_file_content(content, file_type):
        raise APIError(f"File content does not match declared type: {file_type}", 400, "FILE_CONTENT_MISMATCH")
    
    suffix = f".{file_type}"
    temp_file = None
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_file.close()  # Close the file handle
        
        # Write content asynchronously
        async with aiofiles.open(temp_file.name, 'wb') as f:
            await f.write(content)
        
        file_id = f"file_{len(content)}_{hash(file.filename) % 10000}"
        file_info = {
            "filename": file.filename,
            "temp_path": temp_file.name,
            "size": len(content),
            "file_type": file_type,
            "mime_type": mime_type,
            "uploaded_at": time.time()
        }
        safe_uploaded_files_set(file_id, file_info)
        
        return FileUploadResponse(
            file_id=file_id,
            filename=file.filename,
            size=len(content),
            temp_path=temp_file.name,
            file_type=file_type,
            mime_type=mime_type
        )
    except Exception as e:
        # Clean up temp file on any error
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except OSError:
                pass
        raise APIError(f"File upload failed: {str(e)}", 500, "FILE_UPLOAD_ERROR")

# Enhanced file download with format conversion
@app.post("/files/download")
@handle_api_error
async def download_file(request: FileDownloadRequest):
    file_info = safe_uploaded_files_get(request.file_id)
    if not file_info:
        raise APIError("File not found", 404, "FILE_NOT_FOUND")
    
    temp_path = file_info["temp_path"]
    if not os.path.exists(temp_path):
        raise APIError("File no longer exists on server", 404, "FILE_MISSING")
    try:
        text_content = read_text_from_file(temp_path)
        if request.output_format == "original":
            return StreamingResponse(
                open(temp_path, 'rb'),
                media_type=file_info["mime_type"],
                headers={"Content-Disposition": f"attachment; filename={file_info['filename']}"}
            )
        elif request.output_format in ("txt", "md"):
            media_type = "text/plain" if request.output_format == "txt" else "text/markdown"
            return StreamingResponse(
                io.BytesIO(text_content.encode('utf-8')),
                media_type=media_type,
                headers={"Content-Disposition": f"attachment; filename={Path(file_info['filename']).stem}.{request.output_format}"}
            )
        elif request.output_format == "docx":
            temp_docx = tempfile.NamedTemporaryFile(delete=False, suffix='.docx'); temp_docx.close()
            output_path = write_text_to_file(
                output_dir=os.path.dirname(temp_docx.name),
                base_name=Path(file_info['filename']).stem,
                ext=".docx",
                text=text_content,
                original_file=temp_path,
                iteration=1
            )
            return StreamingResponse(
                open(output_path, 'rb'),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f"attachment; filename={Path(file_info['filename']).stem}.docx"}
            )
        else:
            raise HTTPException(400, f"Unsupported output format: {request.output_format}")
    except Exception as e:
        from backend.logger import log_exception
        log_exception("FILE_DOWNLOAD_ERROR", e)
        raise HTTPException(500, f"File download failed: {str(e)}")

# Google Drive integration
@app.post("/files/drive/upload")
async def upload_to_drive(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    file_id = data.get("file_id"); folder_id = data.get("folder_id")
    if not file_id or file_id not in uploaded_files:
        return JSONResponse({"error": "File not found"}, status_code=404)
    file_info = uploaded_files[file_id]; temp_path = file_info["temp_path"]
    try:
        creds = get_google_credentials()
        if not creds:
            return JSONResponse({"error": "Google Drive not authenticated"}, status_code=401)
        doc_id = create_google_doc(
            local_file_path=temp_path,
            title=Path(file_info["filename"]).stem,
            folder_id=folder_id,
            creds=creds
        )
        return {"success": True, "doc_id": doc_id, "message": "File uploaded to Google Drive successfully"}
    except Exception as e:
        from backend.logger import log_exception
        log_exception("DRIVE_UPLOAD_ERROR", e)
        return JSONResponse({"error": f"Google Drive upload failed: {str(e)}"}, status_code=500)

async def _validate_and_resolve_file_path(file_info: Dict[str, Any], file_id: str) -> str:
    """Validate and resolve file path with security checks"""
    # Try to get file path from uploaded_files registry first
    if file_id in uploaded_files:
        file_path = uploaded_files[file_id]["temp_path"]
    else:
        # Fallback to file_info paths - check multiple possible path fields
        file_path = (file_info.get("path") or 
                   file_info.get("temp_path") or 
                   file_info.get("source") or "")
    
    # If still no path, try to construct from filename with strict security validation
    if not file_path and file_info.get("name"):
        filename = file_info.get("name")
        # Strict filename validation
        if filename and len(filename) <= 255 and filename.isprintable():
            # Remove any directory components and dangerous characters
            safe_filename = os.path.basename(filename)
            # Additional validation: only allow alphanumeric, dots, hyphens, underscores
            if all(c.isalnum() or c in '.-_' for c in safe_filename) and not safe_filename.startswith('.'):
                # Only check in the output directory - no other locations
                output_dir = os.path.abspath("./output")
                os.makedirs(output_dir, exist_ok=True)
                possible_path = os.path.join(output_dir, safe_filename)
                
                # Verify the resolved path is within the output directory
                try:
                    resolved_path = os.path.realpath(possible_path)
                    if resolved_path.startswith(output_dir) and os.path.exists(resolved_path) and os.path.isfile(resolved_path):
                        file_path = resolved_path
                except (OSError, ValueError):
                    # Path resolution failed or is invalid
                    pass
    
    if not file_path or not os.path.exists(file_path):
        raise APIError(f'File not found: {file_path or "no path provided"}', 404, "FILE_NOT_FOUND")
    
    return file_path

async def _read_and_validate_file(file_path: str, file_id: str, job_id: str) -> str:
    """Read file content and validate it exists"""
    try:
        logger.debug(f"Reading file: {file_path}")
        original_text = read_text_from_file(file_path)
        logger.debug(f"Read {len(original_text)} characters from file")
        
        msg = {'type': 'stage_update', 'jobId': job_id, 'fileId': file_id, 'stage': 'read', 'status': 'completed', 'message': f'Read {len(original_text)} characters'}
        safe_jobs_snapshot_set(job_id, msg)
        try:
            upsert_job(job_id, {"current_stage": "read", "status": "running"})
        except Exception:
            pass
        
        # WS broadcast with proper failure handling
        try:
            await ws_manager.broadcast(job_id, msg)  # type: ignore
        except Exception as e:
            logger.warning(f"WebSocket broadcast failed: {e}")
            # Mark job as degraded due to WebSocket failure
            degraded_msg = {'type': 'warning', 'jobId': job_id, 'message': 'WebSocket connection failed - using polling fallback', 'degraded': True}
            safe_jobs_snapshot_set(job_id, degraded_msg)
            safe_upsert_job(job_id, {"status": "running", "degraded": True, "ws_failed": True})
        
        # Store original version (pass 0) for diff generation
        try:
            file_version_manager.store_version(
                file_id=file_id,
                pass_number=0,
                content=original_text,
                file_path=file_path,
                metadata={
                    "job_id": job_id,
                    "original": True,
                    "file_size": len(original_text)
                }
            )
        except Exception as e:
            logger.warning(f"Failed to store original version: {e}")
        
        return original_text
        
    except Exception as e:
        error_msg = str(e).replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        err = {'type': 'error', 'jobId': job_id, 'fileId': file_id, 'error': f'File read failed: {error_msg}'}
        safe_jobs_snapshot_set(job_id, err)
        try:
            upsert_job(job_id, {"current_stage": "read", "status": "failed", "error": err.get("error")})
        except Exception:
            pass
        try:
            await ws_manager.broadcast(job_id, err)  # type: ignore
        except Exception as e:
            logger.warning(f"WebSocket broadcast failed: {e}")
        raise APIError(f'File read failed: {error_msg}', 500, "FILE_READ_ERROR")

async def _check_infinite_recursion_risk(current_text: str, original_text: str, pass_num: int, file_id: str, job_id: str) -> bool:
    """Check for infinite recursion risk and return True if should stop"""
    if pass_num > 1:
        # Check for exact duplicates before processing
        if current_text == original_text and pass_num > 2:
            logger.warning(f"Pass {pass_num} would process identical text, stopping to prevent infinite recursion")
            warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'message': 'Identical text detected, stopping refinement'}
            try:
                await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
            except Exception:
                pass
            safe_jobs_snapshot_set(job_id, warning_msg)
            return True
        
        # Check for minimal changes in previous passes
        if pass_num > 3:
            # Calculate similarity with original text
            import difflib
            similarity = difflib.SequenceMatcher(None, original_text, current_text).ratio()
            if similarity > 0.99:  # 99% similar
                logger.warning(f"Pass {pass_num} shows minimal changes from original, stopping to prevent infinite recursion")
                warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'message': f'Minimal changes detected ({similarity:.1%} similarity), stopping refinement'}
                try:
                    await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                except Exception:
                    pass
                safe_jobs_snapshot_set(job_id, warning_msg)
                return True
    
    return False

async def _process_refinement_pass(
    pipeline, 
    file_path: str, 
    current_text: str, 
    pass_num: int, 
    request: RefinementRequest, 
    file_id: str, 
    job_id: str,
    output_sink
) -> tuple:
    """Process a single refinement pass and return (success, final_text, metrics)"""
    try:
        logger.debug(f"Starting pass {pass_num} of {request.passes}")
        
        # Emit pass start event
        start_evt = {'type': 'pass_start', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'totalPasses': request.passes}
        try:
            await ws_manager.broadcast(job_id, start_evt)  # type: ignore
        except Exception:
            pass
        safe_jobs_snapshot_set(job_id, start_evt)
        try:
            upsert_job(job_id, {"current_stage": f"pass_{pass_num}_start", "status": "running"})
        except Exception:
            pass
        
        # Emit plan event with strategy weights and entropy settings
        plan_evt = {
            'type': 'plan',
            'jobId': job_id,
            'fileId': file_id,
            'pass': pass_num,
            'weights': request.heuristics.get('strategy_weights', {
                'clarity': 0.6,
                'persuasion': 0.3,
                'brevity': 0.3,
                'formality': 0.6
            }) if request.heuristics else {
                'clarity': 0.6,
                'persuasion': 0.3,
                'brevity': 0.3,
                'formality': 0.6
            },
            'entropy': request.heuristics.get('entropy', {
                'risk_preference': 0.5,
                'repeat_penalty': 0.0,
                'phrase_penalty': 0.0
            }) if request.heuristics else {
                'risk_preference': 0.5,
                'repeat_penalty': 0.0,
                'phrase_penalty': 0.0
            },
            'formatting': request.heuristics.get('formatting_safeguards', {}).get('mode', 'smart') if request.heuristics and request.heuristics.get('formatting_safeguards') else 'smart',
            'aggressiveness': request.aggressiveness or 'Auto'
        }
        try:
            await ws_manager.broadcast(job_id, plan_evt)  # type: ignore
        except Exception:
            pass
        safe_jobs_snapshot_set(job_id, plan_evt)
        logger.debug(f"Emitted plan event for pass {pass_num}: {plan_evt}")
        
        # Run the pipeline pass
        logger.debug(f"About to call pipeline.run_pass for pass {pass_num}")
        ps, rr, ft = pipeline.run_pass(
            input_path=file_path,
            pass_index=pass_num,
            prev_final_text=current_text,
            entropy_level=request.entropy_level,
            output_sink=output_sink,
            drive_title_base=Path(file_path).stem,
            heuristics_overrides=request.heuristics,
            job_id=job_id
        )
        logger.debug(f"pipeline.run_pass completed for pass {pass_num}")
        
        # Emit strategy event if available
        try:
            if hasattr(pipeline, '_last_strategy') and pipeline._last_strategy:
                strategy_evt = {
                    'type': 'strategy',
                    'jobId': job_id,
                    'fileId': file_id,
                    'pass': pass_num,
                    'weights': pipeline._last_strategy.get('weights', {}),
                    'rationale': pipeline._last_strategy.get('rationale', ''),
                    'approach': pipeline._last_strategy.get('approach', ''),
                    'plan': pipeline._last_strategy.get('plan', {})
                }
                await ws_manager.broadcast(job_id, strategy_evt)  # type: ignore
                safe_jobs_snapshot_set(job_id, strategy_evt)
                logger.debug(f"Emitted strategy event for pass {pass_num}")
        except Exception as e:
            logger.debug(f"Failed to emit strategy event: {e}")
        
        # Emit stage updates
        for stage_name, stage_state in ps.stages.items():
            st_evt = {'type': 'stage_update', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'stage': stage_name, 'status': stage_state.status, 'duration': stage_state.duration_ms}
            try:
                await ws_manager.broadcast(job_id, st_evt)  # type: ignore
            except Exception:
                pass
            safe_jobs_snapshot_set(job_id, st_evt)
            try:
                upsert_job(job_id, {"current_stage": stage_name, "status": "running"})
            except Exception:
                pass
        
        # Calculate metrics
        change_percent = ps.metrics.change_pct if ps.metrics else 0.0
        tension_percent = ps.metrics.tension_pct if ps.metrics else 0.0
        scanner_risk = ps.metrics.scanner_risk if ps.metrics else 0.0
        
        metrics = {
            'changePercent': change_percent,
            'tensionPercent': tension_percent,
            'scannerRisk': scanner_risk,
            'success': rr.success,
            'localPath': rr.local_path,
            'docId': rr.doc_id,
            'originalLength': len(current_text),
            'finalLength': len(ft),
            'processingTime': sum(stage.duration_ms for stage in ps.stages.values())
        }
        # Include per-pass token counts if provided by pipeline
        try:
            tp = getattr(pipeline, '_last_pass_token_stats', None)
            if isinstance(tp, dict):
                metrics['inputTokensPreflight'] = int(tp.get('preflightInTokens', 0) or 0)
                metrics['inputTokensUsed'] = int(tp.get('usedInTokens', 0) or 0)
        except Exception:
            pass
        
        # Store file version for diff generation
        try:
            file_version_manager.store_version(
                file_id=file_id,
                pass_number=pass_num,
                content=ft,
                file_path=rr.local_path,
                metrics=metrics,
                metadata={
                    "job_id": job_id,
                    "entropy_level": request.entropy_level,
                    "heuristics": request.heuristics,
                    "processing_time": sum(stage.duration_ms for stage in ps.stages.values())
                }
            )
        except Exception as e:
            logger.warning(f"Failed to store version: {e}")
        
        # Get cost information for this pass
        pass_cost_info = {}
        if hasattr(pipeline, '_pass_costs') and pipeline._pass_costs:
            total_pass_cost = sum(cost['total_cost'] for cost in pipeline._pass_costs)
            pass_cost_info = {
                'totalCost': total_pass_cost,
                'costBreakdown': pipeline._pass_costs,
                'requestCount': len(pipeline._pass_costs)
            }
        
        # Emit pass complete event
        pc_evt = {
            'type': 'pass_complete', 
            'jobId': job_id, 
            'fileId': file_id, 
            'pass': pass_num, 
            'metrics': metrics,
            'inputChars': len(current_text),
            'outputChars': len(ft),
            'outputPath': rr.local_path if hasattr(rr, 'local_path') and rr.local_path else None,
            'cost': pass_cost_info
        }
        try:
            await ws_manager.broadcast(job_id, pc_evt)  # type: ignore
        except Exception:
            pass
        safe_jobs_snapshot_set(job_id, pc_evt)
        try:
            prog = min(100.0, (pass_num / max(1, request.passes)) * 100.0)
            upsert_job(job_id, {"current_stage": "pass_complete", "progress": prog, "status": "running"})
        except Exception:
            pass
        
        return True, ft, metrics
        
    except Exception as e:
        error_msg = str(e).replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        err2 = {'type': 'error', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'error': error_msg}
        try:
            await ws_manager.broadcast(job_id, err2)  # type: ignore
        except Exception:
            pass
        safe_jobs_snapshot_set(job_id, err2)
        try:
            upsert_job(job_id, {"current_stage": "error", "status": "failed", "error": err2.get("error")})
        except Exception:
            pass
        logger.error(f"Pass {pass_num} failed: {e}")
        return False, current_text, {}

async def _refine_stream(request: RefinementRequest, job_id: str) -> AsyncGenerator[str, None]:
    print(f"DEBUG: Starting refinement stream for job {job_id}")
    pipeline = get_pipeline()
    print(f"DEBUG: Pipeline initialized successfully")
    memory = memory_manager.get_memory(request.user_id) if request.use_memory else None
    processed_files = 0  # Track successfully processed files
    print(f"DEBUG: Processing {len(request.files)} files")
    try:
        for file_info in request.files:
            file_id = file_info.get("id", "unknown")
            file_name = file_info.get("name", file_info.get("fileName", Path(file_id).name if '/' in file_id else file_id))
            
            try:
                # Validate and resolve file path
                file_path = await _validate_and_resolve_file_path(file_info, file_id)
                
                # Read and validate file content
                original_text = await _read_and_validate_file(file_path, file_id, job_id)
                
                # Yield the read completion message
                msg = {'type': 'stage_update', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'stage': 'read', 'status': 'completed', 'message': f'Read {len(original_text)} characters'}
                yield f"{safe_encoder(msg)}\n\n"
                
            except APIError as e:
                # Handle file not found or other API errors
                err = {'type': 'error', 'jobId': job_id, 'fileId': file_id, 'error': e.message}
                safe_jobs_snapshot_set(job_id, err)
                yield f"{safe_encoder(err)}\n\n"
                continue
            
            # Track the current text for each pass (starts with original)
            current_text = original_text
            file_processed_successfully = False
            
            for pass_num in range(1, request.passes + 1):
                print(f"DEBUG: Starting pass {pass_num} of {request.passes}")
                start_evt = {'type': 'pass_start', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'totalPasses': request.passes}
                try:
                    await ws_manager.broadcast(job_id, start_evt)  # type: ignore
                except Exception:
                    pass
                jobs_snapshot[job_id] = start_evt
                try:
                    upsert_job(job_id, {"current_stage": f"pass_{pass_num}_start", "status": "running"})
                except Exception:
                    pass
                yield f"{safe_encoder(start_evt)}\n\n"
                
                # Emit plan event with strategy weights and entropy settings for this pass
                plan_evt = {
                    'type': 'plan',
                    'jobId': job_id,
                    'fileId': file_id,
                    'fileName': file_name,
                    'pass': pass_num,
                    'weights': request.heuristics.get('strategy_weights', {
                        'clarity': 0.6,
                        'persuasion': 0.3,
                        'brevity': 0.3,
                        'formality': 0.6
                    }) if request.heuristics else {
                        'clarity': 0.6,
                        'persuasion': 0.3,
                        'brevity': 0.3,
                        'formality': 0.6
                    },
                    'entropy': request.heuristics.get('entropy', {
                        'risk_preference': 0.5,
                        'repeat_penalty': 0.0,
                        'phrase_penalty': 0.0
                    }) if request.heuristics else {
                        'risk_preference': 0.5,
                        'repeat_penalty': 0.0,
                        'phrase_penalty': 0.0
                    },
                    'formatting': request.heuristics.get('formatting_safeguards', {}).get('mode', 'smart') if request.heuristics and request.heuristics.get('formatting_safeguards') else 'smart',
                    'aggressiveness': request.aggressiveness or 'Auto'
                }
                try:
                    await ws_manager.broadcast(job_id, plan_evt)  # type: ignore
                except Exception:
                    pass
                jobs_snapshot[job_id] = plan_evt
                yield f"{safe_encoder(plan_evt)}\n\n"
                print(f"DEBUG: Emitted plan event for pass {pass_num}: {plan_evt}")
                
                output_sink = None
                if request.output_settings.get("type") == "local":
                    output_sink = LocalSink(request.output_settings.get("path", "./output"))
                elif request.output_settings.get("type") == "drive":
                    try:
                        output_sink = DriveSink(request.output_settings.get("folder_id"), get_google_credentials())
                    except Exception as e:
                        error_msg = str(e).replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
                        yield f"{safe_encoder({'type': 'warning', 'fileId': file_id, 'message': f'Google Drive unavailable, using local: {error_msg}'})}\n\n"
                        output_sink = LocalSink("./output")
                # Early infinite recursion detection - check before processing
                if pass_num > 1:
                    # Check for exact duplicates before processing
                    if current_text == original_text and pass_num > 2:
                        print(f"WARNING: Pass {pass_num} would process identical text, stopping to prevent infinite recursion")
                        warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'message': 'Identical text detected, stopping refinement'}
                        try:
                            await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                        except Exception:
                            pass
                        jobs_snapshot[job_id] = warning_msg
                        yield f"{safe_encoder(warning_msg)}\n\n"
                        break  # Stop processing this file
                    
                    # Check for minimal changes in previous passes
                    if pass_num > 3:
                        # Calculate similarity with original text
                        import difflib
                        similarity = difflib.SequenceMatcher(None, original_text, current_text).ratio()
                        if similarity > 0.99:  # 99% similar
                            print(f"WARNING: Pass {pass_num} shows minimal changes from original, stopping to prevent infinite recursion")
                            warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'message': f'Minimal changes detected ({similarity:.1%} similarity), stopping refinement'}
                            try:
                                await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                            except Exception:
                                pass
                            jobs_snapshot[job_id] = warning_msg
                            yield f"{safe_encoder(warning_msg)}\n\n"
                            break  # Stop processing this file

                try:
                    running_evt = {'type': 'stage_update', 'jobId': job_id, 'fileId': file_id, 'pass': pass_num, 'stage': 'processing', 'status': 'running'}
                    try:
                        await ws_manager.broadcast(job_id, running_evt)  # type: ignore
                    except Exception:
                        pass
                    jobs_snapshot[job_id] = running_evt
                    try:
                        upsert_job(job_id, {"current_stage": "processing", "status": "running"})
                    except Exception:
                        pass
                    yield f"{safe_encoder(running_evt)}\n\n"
                    print(f"DEBUG: About to call pipeline.run_pass for pass {pass_num}")
                    print(f"DEBUG: Input params: entropy_level={request.entropy_level}, file_path={file_path}")
                    print(f"DEBUG: Heuristics: {request.heuristics}")
                    
                    try:
                        # Direct call (remove per-pass timeout guard)
                        ps, rr, ft = pipeline.run_pass(
                            input_path=file_path,
                            pass_index=pass_num,
                            prev_final_text=current_text,  # Use current text as input for next pass
                            entropy_level=request.entropy_level,
                            output_sink=output_sink,
                            drive_title_base=Path(file_path).stem,
                            heuristics_overrides=request.heuristics,
                            job_id=job_id
                        )
                        print(f"DEBUG: pipeline.run_pass completed for pass {pass_num}")
                    except Exception as e:
                        print(f"ERROR: pipeline.run_pass failed for pass {pass_num}: {e}")
                        import traceback
                        traceback.print_exc()
                        raise
                    
                    # Emit actual strategy used (if available from pipeline)
                    try:
                        if hasattr(pipeline, '_last_strategy') and pipeline._last_strategy:
                            strategy_evt = {
                                'type': 'strategy',
                                'jobId': job_id,
                                'fileId': file_id,
                                'fileName': file_name,
                                'pass': pass_num,
                                'weights': pipeline._last_strategy.get('weights', {}),
                                'rationale': pipeline._last_strategy.get('rationale', ''),
                                'approach': pipeline._last_strategy.get('approach', ''),
                                'plan': pipeline._last_strategy.get('plan', {})
                            }
                            await ws_manager.broadcast(job_id, strategy_evt)  # type: ignore
                            jobs_snapshot[job_id] = strategy_evt
                            yield f"{safe_encoder(strategy_evt)}\n\n"
                            print(f"DEBUG: Emitted strategy event for pass {pass_num}")
                    except Exception as e:
                        print(f"DEBUG: Failed to emit strategy event: {e}")
                    
                    # Validate that the pass actually produced meaningful changes to prevent infinite recursion
                    if pass_num > 1:
                        # Check for exact duplicates
                        if ft == current_text:
                            print(f"WARNING: Pass {pass_num} produced identical text, stopping to prevent infinite recursion")
                            warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'message': 'Pass produced identical text, stopping refinement'}
                            try:
                                await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                            except Exception:
                                pass
                            jobs_snapshot[job_id] = warning_msg
                            yield f"{safe_encoder(warning_msg)}\n\n"
                            break  # Stop processing this file
                        
                        # Check for minimal changes (less than 0.1% difference)
                        import difflib
                        similarity = difflib.SequenceMatcher(None, current_text, ft).ratio()
                        if similarity > 0.999:  # 99.9% similar
                            print(f"WARNING: Pass {pass_num} produced minimal changes ({similarity:.3f} similarity), stopping to prevent infinite recursion")
                            warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'message': f'Pass produced minimal changes ({similarity:.1%} similarity), stopping refinement'}
                            try:
                                await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                            except Exception:
                                pass
                            jobs_snapshot[job_id] = warning_msg
                            yield f"{safe_encoder(warning_msg)}\n\n"
                            break  # Stop processing this file
                        
                        # Check for diminishing returns (changes getting smaller)
                        if pass_num > 2:
                            prev_length = len(current_text)
                            current_length = len(ft)
                            change_ratio = abs(current_length - prev_length) / max(prev_length, 1)
                            if change_ratio < 0.001:  # Less than 0.1% change
                                print(f"WARNING: Pass {pass_num} shows diminishing returns ({change_ratio:.4f} change ratio), stopping refinement")
                                warning_msg = {'type': 'warning', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'message': f'Diminishing returns detected ({change_ratio:.2%} change), stopping refinement'}
                                try:
                                    await ws_manager.broadcast(job_id, warning_msg)  # type: ignore
                                except Exception:
                                    pass
                                jobs_snapshot[job_id] = warning_msg
                                yield f"{safe_encoder(warning_msg)}\n\n"
                                break  # Stop processing this file
                    
                    for stage_name, stage_state in ps.stages.items():
                        st_evt = {'type': 'stage_update', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'stage': stage_name, 'status': stage_state.status, 'duration': stage_state.duration_ms}
                        try:
                            await ws_manager.broadcast(job_id, st_evt)  # type: ignore
                        except Exception:
                            pass
                        jobs_snapshot[job_id] = st_evt
                        try:
                            upsert_job(job_id, {"current_stage": stage_name, "status": "running"})
                        except Exception:
                            pass
                        yield f"{safe_encoder(st_evt)}\n\n"
                    change_percent = ps.metrics.change_pct if ps.metrics else 0.0
                    tension_percent = ps.metrics.tension_pct if ps.metrics else 0.0
                    scanner_risk = ps.metrics.scanner_risk if ps.metrics else 0.0
                    if memory and request.use_memory:
                        # Log against the current text (previous pass output), not original
                        memory_manager.log_refinement_pass(request.user_id, current_text, ft, score=scanner_risk, notes=[f"Pass {pass_num}", f"Entropy: {request.entropy_level}"])
                    metrics = {
                        'changePercent': change_percent,
                        'tensionPercent': tension_percent,
                        'scannerRisk': scanner_risk,
                        'success': rr.success,
                        'localPath': rr.local_path,
                        'docId': rr.doc_id,
                        'originalLength': len(current_text),  # Use current text length
                        'finalLength': len(ft),
                        'processingTime': sum(stage.duration_ms for stage in ps.stages.values())
                    }
                    # Store file version for diff generation
                    try:
                        file_version_manager.store_version(
                            file_id=file_id,
                            pass_number=pass_num,
                            content=ft,
                            file_path=rr.local_path,
                            metrics=metrics,
                            metadata={
                                "job_id": job_id,
                                "entropy_level": request.entropy_level,
                                "heuristics": request.heuristics,
                                "processing_time": sum(stage.duration_ms for stage in ps.stages.values())
                            }
                        )
                    except Exception as e:
                        # Log but don't fail the refinement
                        from backend.logger import log_exception
                        log_exception("VERSION_STORAGE_ERROR", e)
                    
                    # Get cost information for this pass
                    pass_cost_info = {}
                    if hasattr(pipeline, '_pass_costs') and pipeline._pass_costs:
                        total_pass_cost = sum(cost['total_cost'] for cost in pipeline._pass_costs)
                        pass_cost_info = {
                            'totalCost': total_pass_cost,
                            'costBreakdown': pipeline._pass_costs,
                            'requestCount': len(pipeline._pass_costs)
                        }
                    
                    pc_evt = {
                        'type': 'pass_complete', 
                        'jobId': job_id, 
                        'fileId': file_id,
                        'fileName': file_name,
                        'pass': pass_num, 
                        'metrics': metrics,
                        'inputChars': len(current_text),
                        'outputChars': len(ft),
                        'outputPath': rr.local_path if hasattr(rr, 'local_path') and rr.local_path else None,
                        'cost': pass_cost_info
                    }
                    print(f"📤 About to yield pass_complete event for pass {pass_num}")
                    try:
                        await ws_manager.broadcast(job_id, pc_evt)  # type: ignore
                    except Exception:
                        pass
                    jobs_snapshot[job_id] = pc_evt
                    try:
                        prog = min(100.0, (pass_num / max(1, request.passes)) * 100.0)
                        upsert_job(job_id, {"current_stage": "pass_complete", "progress": prog, "status": "running"})
                    except Exception:
                        pass
                    yield f"{safe_encoder(pc_evt)}\n\n"
                    # Force flush by yielding a keepalive immediately after
                    yield ":keepalive\n\n"
                    print(f"✅ Yielded pass_complete event for pass {pass_num}")
                    # Update current text for next pass
                    current_text = ft
                    file_processed_successfully = True
                except Exception as e:
                    error_msg = str(e).replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
                    err2 = {'type': 'error', 'jobId': job_id, 'fileId': file_id, 'fileName': file_name, 'pass': pass_num, 'error': error_msg}
                    try:
                        await ws_manager.broadcast(job_id, err2)  # type: ignore
                    except Exception:
                        pass
                    jobs_snapshot[job_id] = err2
                    try:
                        upsert_job(job_id, {"current_stage": "error", "status": "failed", "error": err2.get("error")})
                    except Exception:
                        pass
                    yield f"{safe_encoder(err2)}\n\n"
                    from backend.logger import log_exception; log_exception("REFINEMENT_STREAM_ERROR", e)
            
            # Increment counter for successfully processed file (only once per file)
            if file_processed_successfully:
                processed_files += 1
        
        # Check if any files were successfully processed (after the loop)
        if processed_files == 0:
            no_files_msg = {'type': 'error', 'jobId': job_id, 'error': 'No files were successfully processed. Please check file paths and try again.'}
            try:
                await ws_manager.broadcast(job_id, no_files_msg)  # type: ignore
            except Exception:
                pass
            jobs_snapshot[job_id] = no_files_msg
            try:
                upsert_job(job_id, {"current_stage": "failed", "status": "failed", "error": no_files_msg.get("error")})
            except Exception:
                pass
            yield f"{safe_encoder(no_files_msg)}\n\n"
            return  # Exit early if no files processed
        
        done = {'type': 'complete', 'jobId': job_id, 'message': 'Refinement processing complete', 'memory_context': memory_manager.get_memory_context(request.user_id) if request.use_memory else {}}
        try:
            await ws_manager.broadcast(job_id, done)  # type: ignore
        except Exception:
            pass
        jobs_snapshot[job_id] = done
        try:
            upsert_job(job_id, {"current_stage": "completed", "progress": 100.0, "status": "completed", "result": {"ok": True}})
        except Exception:
            pass
        yield f"{safe_encoder(done)}\n\n"
        # Signal explicit done event for SSE consumers
        yield "event: done\ndata: {}\n\n"
    except Exception as e:
        error_msg = str(e).replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r')
        err3 = {'type': 'error', 'jobId': job_id, 'error': f'Stream processing failed: {error_msg}'}
        try:
            await ws_manager.broadcast(job_id, err3)  # type: ignore
        except Exception:
            pass
        jobs_snapshot[job_id] = err3
        try:
            upsert_job(job_id, {"current_stage": "failed", "status": "failed", "error": err3.get("error")})
        except Exception:
            pass
        yield f"{safe_encoder(err3)}\n\n"
        # Signal explicit error completion for SSE consumers
        yield "event: error\ndata: {}\n\n"
        from backend.logger import log_exception; log_exception("REFINEMENT_STREAM_FATAL", e)

@app.post("/refine/run")
async def refine_run(request: RefinementRequest):
    print(f"REFINE_RUN: Received request with {len(request.files)} files")
    if not request.files:
        return JSONResponse({"error": "No files provided"}, status_code=400)
    print("REFINE_RUN: Checking pipeline...")
    try:
        if not get_pipeline():
            return JSONResponse({"error": "Pipeline not initialized"}, status_code=500)
        print("REFINE_RUN: Pipeline OK")
    except Exception as e:
        print(f"REFINE_RUN: Pipeline error: {e}")
        return JSONResponse({"error": f"Pipeline initialization failed: {str(e)}"}, status_code=500)
    job_id = str(uuid.uuid4())
    try:
        upsert_job(job_id, {"status": "running", "progress": 0.0, "current_stage": "initializing"})
    except Exception:
        pass
    async def event_gen():
        # Small preamble to nudge immediate flush on some clients/proxies
        yield ":ok\n\n"
        print(f"SSE[{job_id}] preamble sent")
        # Send initial job id event
        head = {'type': 'job', 'jobId': job_id}
        yield f"data: {safe_encoder(head)}\n\n"
        print(f"SSE[{job_id}] head sent")
        event_count = 0
        async for chunk in _refine_stream(request, job_id):
            # Preserve properly formatted SSE frames (data:, event:, comments starting with :) and otherwise wrap as data
            if chunk.startswith("data:") or chunk.startswith(":") or chunk.startswith("event:"):
                yield chunk if chunk.endswith("\n\n") else f"{chunk}\n\n"
            else:
                yield f"data: {chunk if chunk.endswith("\n\n") else chunk + '\n\n'}"
            event_count += 1
            if event_count % 10 == 0:
                print(f"SSE[{job_id}] events forwarded: {event_count}")
        # Explicit terminal events after stream completes normally
        final_event = { 'type': 'stream_end', 'jobId': job_id, 'total_events': event_count, 'message': 'Stream completed successfully' }
        yield f"data: {safe_encoder(final_event)}\n\n"
        print(f"SSE[{job_id}] stream_end sent")
        yield ": stream-complete\n\n"
    return EventSourceResponse(
        event_gen(), 
        ping=5,
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )

@app.post("/jobs/queue")
async def queue_job(request: RefinementRequest):
    try:
        if not get_pipeline():
            return JSONResponse({"error": "Pipeline not initialized"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": f"Pipeline initialization failed: {str(e)}"}, status_code=500)
    job_id = str(uuid.uuid4())
    try:
        upsert_job(job_id, {"status": "running", "progress": 0.0, "current_stage": "queued"})
    except Exception:
        pass
    # Start background task
    task = asyncio.create_task(_run_job_background(request, job_id))
    active_tasks[job_id] = task
    return {"jobId": job_id, "status": "queued"}

@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    task = active_tasks.get(job_id)
    if not task:
        return JSONResponse({"error": "job not running"}, status_code=404)
    task.cancel()
    try:
        upsert_job(job_id, {"status": "cancelled", "current_stage": "cancelled"})
    except Exception:
        pass
    return {"jobId": job_id, "status": "cancelled"}

@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: RefinementRequest):
    # Launch a new background run with same effective request
    new_id = str(uuid.uuid4())
    try:
        upsert_job(new_id, {"status": "running", "progress": 0.0, "current_stage": "queued"})
    except Exception:
        pass
    task = asyncio.create_task(_run_job_background(request, new_id))
    active_tasks[new_id] = task
    return {"jobId": new_id, "status": "queued", "retryOf": job_id}

@app.post("/chat")
async def chat(request: ChatRequest):
    # Enhanced input validation
    if not request.message or not request.message.strip():
        return JSONResponse({"error": "message is required"}, status_code=400)
    
    # Validate message length
    if len(request.message) > 10000:  # 10KB limit
        return JSONResponse({"error": "message too long. Maximum 10,000 characters"}, status_code=400)
    
    # Validate user_id if provided
    if hasattr(request, 'user_id') and request.user_id and len(request.user_id) > 100:
        return JSONResponse({"error": "user_id too long"}, status_code=400)
    
    settings = get_settings()
    api_key = settings.openai_api_key
    if not api_key:
        return JSONResponse({"error": "OPENAI_API_KEY not configured"}, status_code=500)
    try:
        refiner = ConversationalRefiner(api_key)
        if request.schema_levels:
            refiner.schema_levels = {str(k): int(v) for k, v in request.schema_levels.items()}
        
        # Update context with request information
        if hasattr(request, 'current_file') and request.current_file:
            refiner.update_context(current_file=request.current_file)
        if hasattr(request, 'current_pass') and request.current_pass:
            refiner.update_context(current_pass=request.current_pass)
        if hasattr(request, 'recent_changes') and request.recent_changes:
            refiner.update_context(recent_changes=request.recent_changes)
        
        memory_context = {}
        if request.use_memory:
            memory_context = memory_manager.get_memory_context(request.user_id)
            memory = memory_manager.get_memory(request.user_id)
            if memory.history:
                recent_passes = memory.history[-3:]
                context_hint = f"\n\nMemory Context: User has {len(memory.history)} previous refinement passes. Recent scores: {[p.get('score', 'N/A') for p in recent_passes]}"
                enhanced_message = request.message + context_hint
            else:
                enhanced_message = request.message
        else:
            enhanced_message = request.message
        reply = refiner.chat(enhanced_message, flags=request.flags)
        return {"reply": reply, "memory_context": memory_context, "schema_info": {"active_schemas": [{"name": k, "description": ADVANCED_COMMANDS[k]["description"]} for k in request.flags.keys() if k in ADVANCED_COMMANDS], "available_schemas": list(ADVANCED_COMMANDS.keys())}}
    except Exception as e:
        from backend.logger import log_exception; log_exception("CHAT_API_ERROR", e)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/memory")
async def memory_endpoint(request: MemoryRequest):
    try:
        memory = memory_manager.get_memory(request.user_id)
        if request.action == "log_pass":
            memory_manager.log_refinement_pass(request.user_id, request.original_text, request.refined_text, request.score, request.notes)
            return {"success": True, "message": "Pass logged successfully", "memory_context": memory_manager.get_memory_context(request.user_id)}
        elif request.action == "get_history":
            return {"success": True, "message": "History retrieved", "history": memory.history, "memory_context": memory_manager.get_memory_context(request.user_id)}
        elif request.action == "clear_history":
            memory.history.clear(); return {"success": True, "message": "History cleared", "memory_context": memory_manager.get_memory_context(request.user_id)}
        elif request.action == "refine_with_feedback":
            settings = get_settings()
            if not settings.openai_api_key:
                return JSONResponse({"error": "OPENAI_API_KEY not configured"}, status_code=500)
            refined_text = refine_with_feedback(settings.openai_api_key, request.original_text, request.heuristics, memory, request.flags)
            return {"success": True, "message": "Text refined with feedback", "refined_text": refined_text, "memory_context": memory_manager.get_memory_context(request.user_id)}
        else:
            return JSONResponse({"error": f"Unknown action: {request.action}"}, status_code=400)
    except Exception as e:
        from backend.logger import log_exception; log_exception("MEMORY_API_ERROR", e)
        return JSONResponse({"error": f"Memory operation failed: {str(e)}"}, status_code=500)

@app.get("/memory/{user_id}/stats")
async def get_memory_stats(user_id: str):
    """Get memory statistics for user"""
    try:
        memory = memory_manager.get_memory(user_id)
        context = memory_manager.get_memory_context(user_id)
        
        return {
            "user_id": user_id,
            "total_passes": len(memory.history),
            "has_history": bool(memory.history),
            "last_score": memory.last_score(),
            "memory_size": len(str(memory.history)),
            "context": context
        }
        
    except Exception as e:
        from backend.logger import log_exception; log_exception("MEMORY_STATS_ERROR", e)
        raise HTTPException(500, f"Failed to get memory stats: {str(e)}")

@app.post("/memory/{user_id}/export")
async def export_memory(user_id: str):
    """Export user's memory data"""
    try:
        memory = memory_manager.get_memory(user_id)
        
        return {
            "user_id": user_id,
            "exported_data": {
                "history": memory.history,
                "export_timestamp": time.time(),
                "total_passes": len(memory.history)
            },
            "message": "Memory exported successfully"
        }
        
    except Exception as e:
        from backend.logger import log_exception; log_exception("MEMORY_EXPORT_ERROR", e)
        raise HTTPException(500, f"Failed to export memory: {str(e)}")

@app.post("/memory/{user_id}/import")
async def import_memory(user_id: str, request: Request):
    """Import memory data for user"""
    try:
        body = await request.json()
        
        if "history" not in body:
            raise HTTPException(400, "Missing 'history' in import data")
        
        # Get user memory (creates if doesn't exist)
        memory = memory_manager.get_memory(user_id)
        
        # Import the history
        memory.history = body["history"]
        
        return {
            "success": True,
            "user_id": user_id,
            "imported_passes": len(memory.history),
            "message": "Memory imported successfully"
        }
        
    except Exception as e:
        from backend.logger import log_exception; log_exception("MEMORY_IMPORT_ERROR", e)
        raise HTTPException(500, f"Failed to import memory: {str(e)}")

@app.get("/schema")
async def get_schema_info():
    return {"commands": ADVANCED_COMMANDS, "descriptions": {k: v["description"] for k, v in ADVANCED_COMMANDS.items()}, "categories": {"processing": ["microstructure_control", "macrostructure_analysis", "anti_scanner_techniques"], "optimization": ["entropy_management", "semantic_tone_tuning"], "safety": ["formatting_safeguards", "refiner_control"], "analysis": ["history_analysis", "annotation_mode", "humanize_academic"]}}

@app.get("/files")
async def list_files():
    with shared_state_lock:
        files_list = [{"file_id": fid, "filename": info["filename"], "size": info["size"], "file_type": info["file_type"], "uploaded_at": info["uploaded_at"]} for fid, info in uploaded_files.items()]
    return {"files": files_list}

@app.delete("/files/{file_id}")
async def delete_file(file_id: str):
    file_info = safe_uploaded_files_get(file_id)
    if not file_info:
        raise HTTPException(404, "File not found")
    
    try:
        if os.path.exists(file_info["temp_path"]): 
            os.unlink(file_info["temp_path"])
        safe_uploaded_files_del(file_id)
        return {"success": True, "message": "File deleted successfully"}
    except Exception as e:
        from backend.logger import log_exception
        log_exception("FILE_DELETE_ERROR", e)
        raise HTTPException(500, f"File deletion failed: {str(e)}")

@app.get("/analytics/jobs")
async def analytics_jobs() -> Dict[str, Any]:
    return {"jobs": db_list_jobs(200)}

@app.get("/logs")
async def read_logs(lines: int = 200) -> Dict[str, Any]:
    try:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        log_path = os.path.join(project_root, "logs", "refiner.log")
        if not os.path.exists(log_path):
            return {"lines": []}
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.readlines()
        tail = data[-max(1, min(lines, 1000)):] if data else []
        return {"lines": [ln.rstrip("\n") for ln in tail]}
    except Exception as e:
        return {"error": str(e), "lines": []}

# =============================================================================
# GOOGLE DRIVE INTEGRATION ENDPOINTS
# =============================================================================

class DriveFileRequest(BaseModel):
    folder_id: str = "root"
    limit: int = 100

class DriveDownloadRequest(BaseModel):
    file_id: str
    output_format: str = "docx"  # docx, txt, md

class DriveUploadRequest(BaseModel):
    file_id: str  # local file ID from uploaded_files
    folder_id: str = "root"
    title: str = ""

@app.get("/drive/files")
async def list_drive_files(request: DriveFileRequest = Depends()):
    """List files from Google Drive folder"""
    try:
        creds = get_google_credentials()
        if not creds:
            raise HTTPException(500, "Google Drive not authenticated")
        
        service = get_drive_service_oauth()
        if not service:
            raise HTTPException(500, "Failed to initialize Drive service")
        
        # Query files in folder
        query = f"'{request.folder_id}' in parents and trashed=false"
        results = service.files().list(
            q=query,
            pageSize=request.limit,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)"
        ).execute()
        
        files = results.get('files', [])
        return {
            "files": [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "mime_type": f["mimeType"],
                    "size": f.get("size", "0"),
                    "modified_time": f.get("modifiedTime", ""),
                    "web_view_link": f.get("webViewLink", ""),
                    "is_document": f["mimeType"] == "application/vnd.google-apps.document"
                }
                for f in files
            ],
            "folder_id": request.folder_id,
            "total": len(files)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to list Drive files: {str(e)}")

@app.get("/drive/files/{file_id}")
async def get_drive_file_info(file_id: str):
    """Get specific Drive file metadata"""
    try:
        creds = get_google_credentials()
        if not creds:
            raise HTTPException(500, "Google Drive not authenticated")
        
        service = get_drive_service_oauth()
        if not service:
            raise HTTPException(500, "Failed to initialize Drive service")
        
        file_metadata = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, webViewLink, parents"
        ).execute()
        
        return {
            "id": file_metadata["id"],
            "name": file_metadata["name"],
            "mime_type": file_metadata["mimeType"],
            "size": file_metadata.get("size", "0"),
            "modified_time": file_metadata.get("modifiedTime", ""),
            "web_view_link": file_metadata.get("webViewLink", ""),
            "parents": file_metadata.get("parents", []),
            "is_document": file_metadata["mimeType"] == "application/vnd.google-apps.document"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to get Drive file: {str(e)}")

@app.post("/drive/files/{file_id}/download")
async def download_drive_file_endpoint(file_id: str, request: DriveDownloadRequest):
    """Download file from Google Drive and return local file ID"""
    try:
        creds = get_google_credentials()
        if not creds:
            raise HTTPException(500, "Google Drive not authenticated")
        
        # Get file info first
        service = get_drive_service_oauth()
        file_metadata = service.files().get(fileId=file_id).execute()
        file_name = file_metadata.get("name", f"drive_file_{file_id}")
        
        # Create temp file for download
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{request.output_format}") as tmp:
            temp_path = tmp.name
        
        # Download the file
        downloaded_path = download_drive_file(file_id, temp_path)
        
        # Generate local file ID
        local_file_id = str(uuid.uuid4())
        
        # Store in uploaded_files registry
        uploaded_files[local_file_id] = {
            "id": local_file_id,
            "name": file_name,
            "path": downloaded_path,
            "size": os.path.getsize(downloaded_path),
            "type": "drive_download",
            "drive_id": file_id,
            "created_at": time.time()
        }
        
        return {
            "local_file_id": local_file_id,
            "file_name": file_name,
            "file_path": downloaded_path,
            "drive_id": file_id,
            "size": os.path.getsize(downloaded_path)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to download Drive file: {str(e)}")

@app.post("/drive/upload")
async def upload_to_drive_endpoint(request: DriveUploadRequest):
    """Upload local file to Google Drive"""
    try:
        creds = get_google_credentials()
        if not creds:
            raise HTTPException(500, "Google Drive not authenticated")
        
        # Get local file info
        local_file = uploaded_files.get(request.file_id)
        if not local_file:
            raise HTTPException(404, "Local file not found")
        
        # Use title from request or derive from filename
        title = request.title or Path(local_file["name"]).stem
        
        # Upload to Drive
        drive_id = create_google_doc(
            local_file["path"],
            title,
            request.folder_id,
            creds
        )
        
        return {
            "drive_id": drive_id,
            "title": title,
            "folder_id": request.folder_id,
            "local_file_id": request.file_id,
            "web_view_link": f"https://docs.google.com/document/d/{drive_id}/edit"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to upload to Drive: {str(e)}")

@app.get("/drive/folders")
async def list_drive_folders():
    """List Google Drive folders"""
    try:
        creds = get_google_credentials()
        if not creds:
            raise HTTPException(500, "Google Drive not authenticated")
        
        service = get_drive_service_oauth()
        if not service:
            raise HTTPException(500, "Failed to initialize Drive service")
        
        # Query folders
        results = service.files().list(
            q="mimeType='application/vnd.google-apps.folder' and trashed=false",
            pageSize=100,
            fields="files(id, name, parents)"
        ).execute()
        
        folders = results.get('files', [])
        return {
            "folders": [
                {
                    "id": f["id"],
                    "name": f["name"],
                    "parents": f.get("parents", [])
                }
                for f in folders
            ],
            "total": len(folders)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to list Drive folders: {str(e)}")

@app.post("/drive/auth")
async def authenticate_drive():
    """Check Drive authentication status"""
    try:
        creds = get_google_credentials()
        if creds and creds.valid:
            return {
                "authenticated": True,
                "message": "Google Drive is authenticated and ready"
            }
        else:
            return {
                "authenticated": False,
                "message": "Google Drive authentication required. Please run desktop app to authenticate."
            }
    except Exception as e:
        return {
            "authenticated": False,
            "message": f"Authentication check failed: {str(e)}"
        }

# =============================================================================
# ADVANCED PIPELINE OPERATIONS
# =============================================================================

class AnalyzeRequest(BaseModel):
    text: str
    analysis_types: List[str] = ["microstructure", "macrostructure", "keywords", "structure"]

class TransformRequest(BaseModel):
    text: str
    transform_type: str
    parameters: Dict[str, Any] = {}

class ValidateRequest(BaseModel):
    text: str
    validate_markdown: bool = True
    validate_structure: bool = True

class EntropyRequest(BaseModel):
    text: str
    current_entropy: str = "medium"
    target_metrics: Dict[str, float] = {}

@app.post("/pipeline/analyze")
async def analyze_text(request: AnalyzeRequest):
    """Analyze text without refining - just get metrics/insights"""
    try:
        from backend.pipeline import _microstructure_det, _macrostructure_det, _keyword_integrity_checker, validate_markdown_structures, protect_markdown_structures
        
        results = {
            "text_length": len(request.text),
            "word_count": len(request.text.split()),
            "sentence_count": len([s for s in request.text.split('.') if s.strip()]),
            "analysis": {}
        }
        
        for analysis_type in request.analysis_types:
            if analysis_type == "microstructure":
                try:
                    microstructure_result = _microstructure_det(request.text)
                    results["analysis"]["microstructure"] = {
                        "result": microstructure_result,
                        "changes_made": microstructure_result != request.text,
                        "change_ratio": abs(len(microstructure_result) - len(request.text)) / max(len(request.text), 1)
                    }
                except Exception as e:
                    results["analysis"]["microstructure"] = {"error": str(e)}
            
            elif analysis_type == "macrostructure":
                try:
                    macrostructure_result = _macrostructure_det(request.text)
                    results["analysis"]["macrostructure"] = {
                        "result": macrostructure_result,
                        "changes_made": macrostructure_result != request.text,
                        "change_ratio": abs(len(macrostructure_result) - len(request.text)) / max(len(request.text), 1)
                    }
                except Exception as e:
                    results["analysis"]["macrostructure"] = {"error": str(e)}
            
            elif analysis_type == "keywords":
                # Extract potential keywords (simple heuristic)
                words = request.text.lower().split()
                word_freq = {}
                for word in words:
                    if len(word) > 4 and word.isalpha():  # Simple keyword criteria
                        word_freq[word] = word_freq.get(word, 0) + 1
                
                keywords = [word for word, freq in word_freq.items() if freq >= 2]
                results["analysis"]["keywords"] = {
                    "detected_keywords": keywords[:20],  # Top 20
                    "keyword_density": len(keywords) / max(len(words), 1),
                    "total_keywords": len(keywords)
                }
            
            elif analysis_type == "structure":
                try:
                    markdown_mapping = {}
                    protected_text, mapping = protect_markdown_structures(request.text)
                    validation = validate_markdown_structures(protected_text, mapping)
                    
                    results["analysis"]["structure"] = {
                        "has_markdown": any(marker in request.text for marker in ['#', '##', '###', '```', '|']),
                        "markdown_elements": {
                            "headings": len([line for line in request.text.split('\n') if line.strip().startswith('#')]),
                            "code_blocks": request.text.count('```'),
                            "tables": request.text.count('|'),
                            "lists": len([line for line in request.text.split('\n') if line.strip().startswith(('-', '*', '+', '1.', '2.', '3.'))])
                        },
                        "validation": validation,
                        "is_valid": not any(validation.values())
                    }
                except Exception as e:
                    results["analysis"]["structure"] = {"error": str(e)}
        
        return results
        
    except Exception as e:
        raise HTTPException(500, f"Text analysis failed: {str(e)}")

@app.post("/pipeline/transform")
async def apply_single_transform(request: TransformRequest):
    """Apply a single transform function"""
    try:
        from backend.pipeline import (
            _sentences, _protect_layout, _restore_layout,
            _delete_random_commas, _random_clause_shuffler, _inject_noise_phrases,
            _modulate_sentence_lengths, _vary_sentence_starts, _replace_long_words_with_synonyms,
            _strip_llm_style_transitions, _introduce_contractions, _inject_hedges_and_idioms,
            _vary_punctuation_and_rhythm, _human_typo_variants, _numeric_style_variation,
            _fragment_some_sentences, _paragraph_restorer, _humanizer_filter
        )
        
        # Available transforms
        transforms = {
            "sentences": _sentences,
            "protect_layout": _protect_layout,
            "delete_commas": _delete_random_commas,
            "shuffle_clauses": _random_clause_shuffler,
            "inject_noise": _inject_noise_phrases,
            "modulate_lengths": _modulate_sentence_lengths,
            "vary_starts": _vary_sentence_starts,
            "replace_synonyms": _replace_long_words_with_synonyms,
            "strip_transitions": _strip_llm_style_transitions,
            "introduce_contractions": _introduce_contractions,
            "inject_hedges": _inject_hedges_and_idioms,
            "vary_punctuation": _vary_punctuation_and_rhythm,
            "human_typos": _human_typo_variants,
            "numeric_variation": _numeric_style_variation,
            "fragment_sentences": _fragment_some_sentences,
            "restore_paragraphs": _paragraph_restorer,
            "humanizer_filter": _humanizer_filter
        }
        
        if request.transform_type not in transforms:
            available = list(transforms.keys())
            raise HTTPException(400, f"Unknown transform type: {request.transform_type}. Available: {available}")
        
        transform_func = transforms[request.transform_type]
        
        # Apply transform with parameters
        if request.parameters:
            result = transform_func(request.text, **request.parameters)
        else:
            result = transform_func(request.text)
        
        return {
            "original_text": request.text,
            "transformed_text": result,
            "transform_type": request.transform_type,
            "parameters": request.parameters,
            "changes_made": result != request.text,
            "change_ratio": abs(len(result) - len(request.text)) / max(len(request.text), 1),
            "original_length": len(request.text),
            "transformed_length": len(result)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Transform failed: {str(e)}")

@app.post("/pipeline/validate")
async def validate_text(request: ValidateRequest):
    """Validate text structure and markdown"""
    try:
        from backend.pipeline import validate_markdown_structures, protect_markdown_structures
        
        results = {
            "text_length": len(request.text),
            "word_count": len(request.text.split()),
            "validation_results": {}
        }
        
        if request.validate_markdown:
            try:
                markdown_mapping = {}
                protected_text, mapping = protect_markdown_structures(request.text)
                validation = validate_markdown_structures(protected_text, mapping)
                
                results["validation_results"]["markdown"] = {
                    "is_valid": not any(validation.values()),
                    "issues": [k for k, v in validation.items() if v],
                    "details": validation,
                    "has_markdown": any(marker in request.text for marker in ['#', '##', '###', '```', '|']),
                    "elements_found": {
                        "headings": len([line for line in request.text.split('\n') if line.strip().startswith('#')]),
                        "code_blocks": request.text.count('```'),
                        "tables": request.text.count('|'),
                        "lists": len([line for line in request.text.split('\n') if line.strip().startswith(('-', '*', '+', '1.', '2.', '3.'))])
                    }
                }
            except Exception as e:
                results["validation_results"]["markdown"] = {"error": str(e)}
        
        if request.validate_structure:
            # Basic structure validation
            lines = request.text.split('\n')
            results["validation_results"]["structure"] = {
                "line_count": len(lines),
                "empty_lines": len([line for line in lines if not line.strip()]),
                "avg_line_length": sum(len(line) for line in lines) / max(len(lines), 1),
                "has_paragraphs": len([line for line in lines if line.strip()]) > 1,
                "potential_issues": []
            }
            
            # Check for common issues
            if len(request.text) < 50:
                results["validation_results"]["structure"]["potential_issues"].append("text_too_short")
            if len(request.text) > 100000:
                results["validation_results"]["structure"]["potential_issues"].append("text_too_long")
            if request.text.count('\n') < 2:
                results["validation_results"]["structure"]["potential_issues"].append("no_paragraph_breaks")
        
        return results
        
    except Exception as e:
        raise HTTPException(500, f"Validation failed: {str(e)}")

@app.post("/pipeline/entropy")
async def adjust_entropy_level(request: EntropyRequest):
    """Adjust entropy level and get preview"""
    try:
        from backend.pipeline import adapt_entropy_level
        
        # Calculate current metrics (simplified)
        current_metrics = {
            "length": len(request.text),
            "word_count": len(request.text.split()),
            "sentence_count": len([s for s in request.text.split('.') if s.strip()]),
            "avg_sentence_length": len(request.text.split()) / max(len([s for s in request.text.split('.') if s.strip()]), 1)
        }
        
        # Get suggested entropy level (simplified implementation)
        try:
            from backend.pipeline import adapt_entropy_level
            suggested_entropy = adapt_entropy_level(0.0)
        except ImportError:
            # Fallback if function doesn't exist
            suggested_entropy = "medium"
        
        # Available entropy levels and their characteristics
        entropy_levels = {
            "low": {
                "description": "Minimal changes, high preservation",
                "transforms": ["humanizer_filter", "strategy_insight_det"],
                "risk_level": "low"
            },
            "medium": {
                "description": "Balanced refinement",
                "transforms": ["microstructure_det", "macrostructure_det", "humanizer_filter"],
                "risk_level": "medium"
            },
            "high": {
                "description": "Aggressive refinement",
                "transforms": ["all_transforms", "anti_scanner_techniques"],
                "risk_level": "high"
            }
        }
        
        return {
            "current_entropy": request.current_entropy,
            "suggested_entropy": suggested_entropy,
            "current_metrics": current_metrics,
            "target_metrics": request.target_metrics,
            "entropy_levels": entropy_levels,
            "recommendation": {
                "level": suggested_entropy,
                "reasoning": f"Based on current text metrics, {suggested_entropy} entropy is recommended",
                "expected_changes": entropy_levels.get(suggested_entropy, {}).get("description", "Unknown")
            }
        }
        
    except Exception as e:
        raise HTTPException(500, f"Entropy analysis failed: {str(e)}")

# =============================================================================
# TEXT ANNOTATIONS & PROCESSING
# =============================================================================

class AnnotationRequest(BaseModel):
    text: str
    verbosity: str = "low"  # low, medium, high

class InjectAnnotationsRequest(BaseModel):
    text: str
    annotations: List[Dict[str, Any]]
    verbosity: str = "low"

class StructureRequest(BaseModel):
    text: str
    analysis_depth: str = "medium"  # basic, medium, detailed

class KeywordRequest(BaseModel):
    text: str
    keywords: List[str] = []
    max_repeats: int = 2

@app.post("/text/annotate")
async def generate_annotations(request: AnnotationRequest):
    """Generate sidecar annotations for text"""
    try:
        # Generate annotations using the pipeline function
        try:
            from backend.pipeline import generate_sidecar_annotations
            annotations = generate_sidecar_annotations(
                before="",  # No before text for standalone analysis
                after=request.text,
                verbosity=request.verbosity
            )
        except ImportError:
            # Fallback if function doesn't exist - return empty annotations
            annotations = []
        
        return {
            "original_text": request.text,
            "annotations": [
                {
                    "start": ann.start,
                    "end": ann.end,
                    "type": ann.type,
                    "description": ann.description,
                    "confidence": ann.confidence,
                    "metadata": ann.metadata
                }
                for ann in annotations
            ],
            "verbosity": request.verbosity,
            "total_annotations": len(annotations)
        }
        
    except Exception as e:
        raise HTTPException(500, f"Annotation generation failed: {str(e)}")

@app.post("/text/inject-annotations")
async def inject_annotations(request: InjectAnnotationsRequest):
    """Inject inline annotations into text"""
    try:
        try:
            from backend.pipeline import inject_inline_annotations, AnnotationSpan
            
            # Convert annotations to AnnotationSpan objects
            annotation_spans = []
            for ann in request.annotations:
                span = AnnotationSpan(
                    start=ann.get("start", 0),
                    end=ann.get("end", 0),
                    type=ann.get("type", "info"),
                    description=ann.get("description", ""),
                    confidence=ann.get("confidence", 0.5),
                    metadata=ann.get("metadata", {})
                )
                annotation_spans.append(span)
            
            # Inject annotations
            annotated_text = inject_inline_annotations(
                text=request.text,
                anns=annotation_spans,
                verbosity=request.verbosity
            )
        except ImportError:
            # Fallback if functions don't exist - return original text
            annotated_text = request.text
        
        return {
            "original_text": request.text,
            "annotated_text": annotated_text,
            "injected_annotations": len(annotation_spans),
            "verbosity": request.verbosity,
            "changes_made": annotated_text != request.text
        }
        
    except Exception as e:
        raise HTTPException(500, f"Annotation injection failed: {str(e)}")

@app.post("/text/analyze-structure")
async def analyze_structure(request: StructureRequest):
    """Analyze document structure"""
    try:
        from backend.pipeline import _macrostructure_det
        
        # Apply macrostructure analysis
        analyzed_text = _macrostructure_det(request.text)
        
        # Basic structure analysis
        lines = request.text.split('\n')
        paragraphs = [p.strip() for p in request.text.split('\n\n') if p.strip()]
        
        structure_analysis = {
            "total_lines": len(lines),
            "total_paragraphs": len(paragraphs),
            "avg_paragraph_length": sum(len(p) for p in paragraphs) / max(len(paragraphs), 1),
            "has_headings": any(line.strip().startswith('#') for line in lines),
            "has_lists": any(line.strip().startswith(('-', '*', '+', '1.', '2.', '3.')) for line in lines),
            "has_code": '```' in request.text,
            "has_tables": '|' in request.text,
            "structure_score": 0.8 if analyzed_text != request.text else 1.0  # Simplified scoring
        }
        
        if request.analysis_depth in ["medium", "detailed"]:
            # Medium analysis
            structure_analysis.update({
                "sentence_count": len([s for s in request.text.split('.') if s.strip()]),
                "word_count": len(request.text.split()),
                "character_count": len(request.text),
                "heading_count": len([line for line in lines if line.strip().startswith('#')]),
                "list_item_count": len([line for line in lines if line.strip().startswith(('-', '*', '+', '1.', '2.', '3.'))]),
                "code_block_count": request.text.count('```') // 2,
                "table_row_count": len([line for line in lines if '|' in line])
            })
        
        if request.analysis_depth == "detailed":
            # Detailed analysis
            structure_analysis.update({
                "complexity_metrics": {
                    "avg_sentence_length": len(request.text.split()) / max(len([s for s in request.text.split('.') if s.strip()]), 1),
                    "paragraph_cohesion": 0.7,  # Simplified metric
                    "readability_score": 0.6,   # Simplified metric
                    "structure_consistency": 0.8  # Simplified metric
                },
                "macrostructure_result": analyzed_text,
                "macrostructure_changes": analyzed_text != request.text
            })
        
        return {
            "original_text": request.text,
            "analysis_depth": request.analysis_depth,
            "structure_analysis": structure_analysis,
            "recommendations": []
        }
        
    except Exception as e:
        raise HTTPException(500, f"Structure analysis failed: {str(e)}")

@app.post("/text/validate-keywords")
async def validate_keywords(request: KeywordRequest):
    """Check keyword integrity"""
    try:
        from backend.pipeline import _keyword_integrity_checker
        
        # Extract keywords if not provided
        if not request.keywords:
            words = request.text.lower().split()
            word_freq = {}
            for word in words:
                if len(word) > 4 and word.isalpha():
                    word_freq[word] = word_freq.get(word, 0) + 1
            
            # Get most frequent words as keywords
            request.keywords = [word for word, freq in word_freq.items() if freq >= 2][:10]
        
        # Apply keyword integrity check
        validated_text = _keyword_integrity_checker(request.text, request.keywords, request.max_repeats)
        
        # Analyze keyword usage
        keyword_analysis = {}
        for keyword in request.keywords:
            count = request.text.lower().count(keyword.lower())
            keyword_analysis[keyword] = {
                "count": count,
                "is_overused": count > request.max_repeats,
                "density": count / max(len(request.text.split()), 1)
            }
        
        return {
            "original_text": request.text,
            "validated_text": validated_text,
            "keywords": request.keywords,
            "max_repeats": request.max_repeats,
            "keyword_analysis": keyword_analysis,
            "changes_made": validated_text != request.text,
            "overused_keywords": [kw for kw, analysis in keyword_analysis.items() if analysis["is_overused"]],
            "recommendations": []
        }
        
    except Exception as e:
        raise HTTPException(500, f"Keyword validation failed: {str(e)}")

# =============================================================================
# STYLE MANAGEMENT
# =============================================================================

class StyleRequest(BaseModel):
    file_id: str  # local file ID from uploaded_files
    output_format: str = "json"  # json, docx

class ApplyStyleRequest(BaseModel):
    text: str
    style_skeleton: Dict[str, Any]
    output_path: str = ""

class StyleSequenceRequest(BaseModel):
    file_id: str  # local file ID from uploaded_files

@app.post("/style/extract")
async def extract_style_skeleton(request: StyleRequest):
    """Extract style skeleton from DOCX file"""
    try:
        # Get local file info
        local_file = uploaded_files.get(request.file_id)
        if not local_file:
            raise HTTPException(404, "Local file not found")
        
        # Check if it's a DOCX file
        if not local_file["name"].lower().endswith('.docx'):
            raise HTTPException(400, "Style extraction only supported for DOCX files")
        
        # Extract style skeleton using real implementation
        skeleton = make_style_skeleton_from_docx(local_file["path"])
        
        if request.output_format == "docx":
            # Save skeleton as DOCX template
            output_path = f"./output/style_skeleton_{request.file_id}.docx"
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            # Create a simple DOCX with the skeleton
            from docx import Document
            doc = Document()
            doc.add_paragraph("Style Skeleton Template")
            doc.save(output_path)
            
            return {
                "file_id": request.file_id,
                "style_skeleton": skeleton,
                "output_path": output_path,
                "message": "Style skeleton extracted and saved as DOCX"
            }
        else:
            return {
                "file_id": request.file_id,
                "style_skeleton": skeleton,
                "output_format": request.output_format,
                "message": "Style skeleton extracted successfully"
            }
        
    except Exception as e:
        raise HTTPException(500, f"Style extraction failed: {str(e)}")

@app.post("/style/apply")
async def apply_style_skeleton(request: ApplyStyleRequest):
    """Apply style skeleton to text"""
    try:
        # Generate output path if not provided
        if not request.output_path:
            output_path = f"./output/styled_text_{uuid.uuid4().hex[:8]}.docx"
        else:
            output_path = request.output_path
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Apply style skeleton using real implementation
        write_docx_with_skeleton(
            text=request.text,
            out_path=output_path,
            skel=request.style_skeleton,
            seq=None  # No sequence provided
        )
        
        # Generate local file ID for the output
        local_file_id = str(uuid.uuid4())
        uploaded_files[local_file_id] = {
            "id": local_file_id,
            "name": os.path.basename(output_path),
            "path": output_path,
            "size": os.path.getsize(output_path),
            "type": "styled_output",
            "created_at": time.time()
        }
        
        return {
            "local_file_id": local_file_id,
            "output_path": output_path,
            "text_length": len(request.text),
            "style_applied": True,
            "message": "Style skeleton applied successfully"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Style application failed: {str(e)}")

@app.post("/style/sequence")
async def get_style_sequence(request: StyleSequenceRequest):
    """Get style sequence from DOCX file"""
    try:
        # Get local file info
        local_file = uploaded_files.get(request.file_id)
        if not local_file:
            raise HTTPException(404, "Local file not found")
        
        # Check if it's a DOCX file
        if not local_file["name"].lower().endswith('.docx'):
            raise HTTPException(400, "Style sequence extraction only supported for DOCX files")
        
        # Extract style sequence using real implementation
        sequence = make_style_sequence_from_docx(local_file["path"])
        
        return {
            "file_id": request.file_id,
            "style_sequence": sequence,
            "sequence_length": len(sequence),
            "message": "Style sequence extracted successfully"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Style sequence extraction failed: {str(e)}")

@app.get("/style/templates")
async def list_style_templates():
    """List available style templates"""
    try:
        # Look for template files in the project
        template_dir = "./templates"
        templates = []
        
        if os.path.exists(template_dir):
            for file in os.listdir(template_dir):
                if file.lower().endswith('.docx'):
                    file_path = os.path.join(template_dir, file)
                    templates.append({
                        "name": file,
                        "path": file_path,
                        "size": os.path.getsize(file_path),
                        "modified": os.path.getmtime(file_path)
                    })
        
        return {
            "templates": templates,
            "template_dir": template_dir,
            "total_templates": len(templates),
            "message": "Style templates listed successfully"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to list style templates: {str(e)}")

class ValidateStyleRequest(BaseModel):
    style_skeleton: Dict[str, Any]

@app.post("/style/validate")
async def validate_style_skeleton_endpoint(request: ValidateStyleRequest):
    """Validate a style skeleton"""
    try:
        validation_result = validate_style_skeleton(request.style_skeleton)
        
        return {
            "is_valid": validation_result["is_valid"],
            "errors": validation_result["errors"],
            "warnings": validation_result["warnings"],
            "suggestions": validation_result["suggestions"],
            "message": "Style skeleton validation completed"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Style validation failed: {str(e)}")

# =============================================================================
# MISSING FRONTEND ENDPOINTS
# =============================================================================

class StrategyFeedbackRequest(BaseModel):
    weights: Dict[str, float]  # clarity, persuasion, brevity, formality
    thumbs: str  # "up" or "down"
    fileId: Optional[str] = None
    pass_number: Optional[int] = None
    rationale: Optional[str] = None

@app.post("/strategy/feedback")
async def strategy_feedback(request: StrategyFeedbackRequest):
    """Handle strategy feedback from frontend"""
    try:
        # Create feedback object
        feedback = StrategyFeedback(
            feedback_id=str(uuid.uuid4()),
            user_id="default",  # In a real app, get from auth context
            weights=request.weights,
            thumbs=request.thumbs,
            file_id=request.fileId,
            pass_number=request.pass_number,
            rationale=request.rationale
        )
        
        # Store feedback
        feedback_id = strategy_feedback_manager.store_feedback(feedback)
        
        # Get updated strategy recommendations
        recommendations = strategy_feedback_manager.get_strategy_recommendations("default", {})
        
        return {
            "success": True,
            "message": "Strategy feedback recorded and analyzed",
            "feedback_id": feedback_id,
            "weights": request.weights,
            "thumbs": request.thumbs,
            "updated_recommendations": recommendations,
            "effective_weights": recommendations["effective_weights"]
        }
        
    except Exception as e:
        from backend.logger import log_exception
        log_exception("STRATEGY_FEEDBACK_ERROR", e)
        raise HTTPException(500, f"Strategy feedback failed: {str(e)}")

@app.get("/strategy/feedback/history")
async def get_strategy_feedback_history(user_id: str = "default", limit: int = 20):
    """Get strategy feedback history for a user"""
    try:
        feedback_history = strategy_feedback_manager.get_user_feedback(user_id, limit)
        
        return {
            "user_id": user_id,
            "feedback_count": len(feedback_history),
            "feedback_history": [
                {
                    "feedback_id": f.feedback_id,
                    "weights": f.weights,
                    "thumbs": f.thumbs,
                    "file_id": f.file_id,
                    "pass_number": f.pass_number,
                    "rationale": f.rationale,
                    "timestamp": f.timestamp
                }
                for f in feedback_history
            ]
        }
        
    except Exception as e:
        from backend.logger import log_exception
        log_exception("STRATEGY_FEEDBACK_HISTORY_ERROR", e)
        raise HTTPException(500, f"Failed to get feedback history: {str(e)}")

@app.get("/strategy/recommendations")
async def get_strategy_recommendations(user_id: str = "default"):
    """Get current strategy recommendations based on feedback"""
    try:
        current_metrics = {}  # Could be enhanced with actual current text metrics
        recommendations = strategy_feedback_manager.get_strategy_recommendations(user_id, current_metrics)
        
        return {
            "user_id": user_id,
            "recommendations": recommendations,
            "timestamp": time.time()
        }
        
    except Exception as e:
        from backend.logger import log_exception
        log_exception("STRATEGY_RECOMMENDATIONS_ERROR", e)
        raise HTTPException(500, f"Failed to get recommendations: {str(e)}")

class DiffRequest(BaseModel):
    fileId: str
    fromPass: int
    toPass: int
    mode: str = "sentence"


@app.get("/analytics/summary")
async def get_analytics_summary():
    """Get comprehensive analytics summary, including live OpenAI usage."""
    try:
        # Get all jobs from database
        jobs = list_jobs(1000)  # Get last 1000 jobs
        
        # Calculate basic metrics
        total_jobs = len(jobs)
        completed_jobs = [j for j in jobs if j.get("status") == "completed"]
        failed_jobs = [j for j in jobs if j.get("status") == "failed"]
        running_jobs = [j for j in jobs if j.get("status") == "running"]
        
        # Calculate performance metrics
        if completed_jobs:
            # Filter out jobs with missing metrics to avoid skewing averages
            jobs_with_metrics = [j for j in completed_jobs if j.get("metrics")]
            jobs_with_processing_time = [j for j in completed_jobs if j.get("metrics", {}).get("processingTime", 0) > 0]
            
            avg_change_percent = sum(j.get("metrics", {}).get("changePercent", 0) for j in jobs_with_metrics) / max(len(jobs_with_metrics), 1)
            avg_tension_percent = sum(j.get("metrics", {}).get("tensionPercent", 0) for j in jobs_with_metrics) / max(len(jobs_with_metrics), 1)
            avg_processing_time = sum(j.get("metrics", {}).get("processingTime", 0) for j in jobs_with_processing_time) / max(len(jobs_with_processing_time), 1)
            avg_risk_reduction = sum(j.get("metrics", {}).get("riskReduction", 0) for j in jobs_with_metrics) / max(len(jobs_with_metrics), 1)
        else:
            avg_change_percent = 0
            avg_tension_percent = 0
            avg_processing_time = 0
            avg_risk_reduction = 0
        
        # Recent activity (last 10 jobs)
        recent_activity = sorted(jobs, key=lambda x: x.get("created_at", 0), reverse=True)[:10]
        
        return JSONResponse({
            "jobs": {
                "totalJobs": total_jobs,
                "completed": len(completed_jobs),
                "failed": len(failed_jobs),
                "running": len(running_jobs),
                "successRate": (len(completed_jobs) / total_jobs * 100) if total_jobs > 0 else 0,
                "performanceMetrics": {
                    "avgChangePercent": round(avg_change_percent, 2),
                    "avgTensionPercent": round(avg_tension_percent, 2),
                    "avgProcessingTime": round(avg_processing_time, 2),
                    "avgRiskReduction": round(avg_risk_reduction, 2),
                },
                "recentActivity": [
                    {
                        "id": job.get("id", "unknown"),
                        "fileName": job.get("fileName", "Unknown"),
                        "timestamp": datetime.fromtimestamp(job.get("created_at", 0)).isoformat(),
                        "status": job.get("status", "unknown"),
                        "action": f"Processing {'completed' if job.get('status') == 'completed' else 'failed' if job.get('status') == 'failed' else 'running'}",
                    }
                    for job in recent_activity
                ]
            },
            "openai": {
                "total_requests": analytics_store.total_requests,
                "total_tokens_in": analytics_store.total_tokens_in,
                "total_tokens_out": analytics_store.total_tokens_out,
                "total_cost": analytics_store.total_cost,
                "current_model": analytics_store.current_model,
                "last_24h": analytics_store.summary_last_24h(),
            },
            "schema_usage": analytics_store.get_schema_usage_stats()
        })
    except Exception as e:
        from backend.logger import log_exception
        log_exception("ANALYTICS_SUMMARY_ERROR", e)
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/refine/diff")
async def get_diff(request: Request):
    """Get diff between two passes of a file"""
    try:
        params = request.query_params
        file_id = params.get("fileId")
        from_pass = int(params.get("fromPass", 1))
        to_pass = int(params.get("toPass", 2))
        mode = params.get("mode", "sentence")
        
        if not file_id:
            return JSONResponse({"error": "fileId is required"}, status_code=400)
        
        # Get file versions from file_versions
        try:
            from_version = file_version_manager.get_version(file_id, from_pass)
            to_version = file_version_manager.get_version(file_id, to_pass)
            
            if not from_version or not to_version:
                return JSONResponse({"error": f"Version {from_pass} or {to_pass} not found for file {file_id}"}, status_code=404)
            
            from_text = from_version.content
            to_text = to_version.content
        except Exception as e:
            return JSONResponse({"error": f"Could not retrieve file versions: {str(e)}"}, status_code=404)
        
        # Generate diff using difflib
        import difflib
        
        if mode == "word":
            # Tokenize by whitespace and compute word-level diffs
            from_tokens = from_text.split()
            to_tokens = to_text.split()
            sm = difflib.SequenceMatcher(None, from_tokens, to_tokens)
            changes = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal':
                    continue
                if tag in ('replace', 'insert'):
                    new_segment = ' '.join(to_tokens[j1:j2])
                    changes.append({
                        "type": "insert" if tag == 'insert' else "replace",
                        "originalText": '' if tag == 'insert' else ' '.join(from_tokens[i1:i2]),
                        "newText": new_segment,
                        "position": {"start": j1, "end": j2}
                    })
                if tag in ('replace', 'delete'):
                    if tag == 'delete':
                        changes.append({
                            "type": "delete",
                            "originalText": ' '.join(from_tokens[i1:i2]),
                            "newText": "",
                            "position": {"start": i1, "end": i2}
                        })
        else:  # sentence mode
            # Split into sentences more robustly by punctuation
            import re
            sentence_splitter = re.compile(r'(?<=[.!?])\s+')
            from_sentences = [s for s in sentence_splitter.split(from_text.strip()) if s]
            to_sentences = [s for s in sentence_splitter.split(to_text.strip()) if s]
            sm = difflib.SequenceMatcher(None, from_sentences, to_sentences)
            changes = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == 'equal':
                    continue
                if tag in ('replace', 'insert'):
                    changes.append({
                        "type": "insert" if tag == 'insert' else "replace",
                        "originalText": '' if tag == 'insert' else '\n'.join(from_sentences[i1:i2]),
                        "newText": '\n'.join(to_sentences[j1:j2]),
                        "position": {"start": j1, "end": j2}
                    })
                if tag in ('replace', 'delete'):
                    if tag == 'delete':
                        changes.append({
                            "type": "delete",
                            "originalText": '\n'.join(from_sentences[i1:i2]),
                            "newText": "",
                            "position": {"start": i1, "end": i2}
                        })
        
        # Cap size of changes to avoid giant payloads
        MAX_CHANGES = 2000
        MAX_TEXT_LEN = 2000
        if len(changes) > MAX_CHANGES:
            changes = changes[:MAX_CHANGES]
        # Truncate overly long text fields
        for c in changes:
            if isinstance(c.get("originalText"), str) and len(c["originalText"]) > MAX_TEXT_LEN:
                c["originalText"] = c["originalText"][:MAX_TEXT_LEN]
            if isinstance(c.get("newText"), str) and len(c["newText"]) > MAX_TEXT_LEN:
                c["newText"] = c["newText"][:MAX_TEXT_LEN]

        # Calculate statistics
        insertions = len([c for c in changes if c["type"] == "insert"])
        deletions = len([c for c in changes if c["type"] == "delete"])
        replacements = len([c for c in changes if c["type"] == "replace"])
        
        return {
            "fileId": file_id,
            "fromPass": from_pass,
            "toPass": to_pass,
            "mode": mode,
            "changes": changes,
            "statistics": {
                "totalChanges": len(changes),
                "insertions": insertions,
                "deletions": deletions,
                "replacements": replacements,
                "wordsChanged": len([c for c in changes if c["type"] in ["insert", "replace"]]),
                "charactersChanged": sum(len(c["newText"]) for c in changes if c["type"] in ["insert", "replace"])
            }
        }
    except Exception as e:
        from backend.logger import log_exception
        log_exception("DIFF_GENERATION_ERROR", e)
        return JSONResponse({"error": str(e)}, status_code=500)

def create_app() -> FastAPI:
    return app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.api.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)


