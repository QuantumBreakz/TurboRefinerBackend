"""
Database models and setup for the FastAPI backend.
"""

import os
import time
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass
class RefinementJob:
    """Represents a refinement job in the system."""
    id: str
    user_id: str
    status: str  # "pending", "running", "completed", "failed", "cancelled"
    progress: float = 0.0
    current_stage: str = "initializing"
    error_message: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    created_at: float = None
    updated_at: float = None
    completed_at: Optional[float] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = time.time()
        if self.updated_at is None:
            self.updated_at = time.time()

# In-memory storage for jobs (in production, use SQLite/PostgreSQL)
_jobs_storage: Dict[str, RefinementJob] = {}

def init_database():
    """Initialize the database (in-memory for now)."""
    global _jobs_storage
    _jobs_storage = {}
    print("✅ Database initialized (in-memory storage)")

def upsert_job(job_id: str, job_data: Dict[str, Any]):
    """Create or update a job in the database."""
    global _jobs_storage
    
    if job_id in _jobs_storage:
        # Update existing job
        job = _jobs_storage[job_id]
        for key, value in job_data.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = time.time()
    else:
        # Create new job
        job_data['id'] = job_id
        job_data['user_id'] = job_data.get('user_id', 'default')
        job = RefinementJob(**job_data)
        _jobs_storage[job_id] = job
    
    return job

def get_job(job_id: str) -> Optional[RefinementJob]:
    """Get a job by ID."""
    return _jobs_storage.get(job_id)

def list_jobs(user_id: Optional[str] = None, limit: int = 50) -> list[RefinementJob]:
    """List jobs, optionally filtered by user."""
    jobs = list(_jobs_storage.values())
    
    if user_id:
        jobs = [job for job in jobs if job.user_id == user_id]
    
    # Sort by created_at descending
    jobs.sort(key=lambda x: x.created_at, reverse=True)
    
    return jobs[:limit]

def delete_job(job_id: str) -> bool:
    """Delete a job by ID."""
    if job_id in _jobs_storage:
        del _jobs_storage[job_id]
        return True
    return False

def cleanup_old_jobs(days_to_keep: int = 30):
    """Clean up old completed/failed jobs."""
    cutoff_time = time.time() - (days_to_keep * 24 * 3600)
    
    jobs_to_remove = []
    for job_id, job in _jobs_storage.items():
        if (job.status in ['completed', 'failed', 'cancelled'] and 
            job.created_at < cutoff_time):
            jobs_to_remove.append(job_id)
    
    for job_id in jobs_to_remove:
        del _jobs_storage[job_id]
    
    return len(jobs_to_remove)
