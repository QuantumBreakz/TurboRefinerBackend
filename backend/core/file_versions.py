"""
File version storage and retrieval system for tracking refinement passes.
"""

import os
import json
import time
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass
class FileVersion:
    file_id: str
    pass_number: int
    content: str
    timestamp: float
    file_path: Optional[str] = None
    metrics: Optional[Dict] = None
    metadata: Optional[Dict] = None

class FileVersionManager:
    """Manages file versions for diff generation."""
    
    def __init__(self, storage_dir: str = "file_versions", max_cache_size: int = 1000):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        
        # In-memory cache for active versions with size limit
        self._versions_cache: Dict[str, Dict[int, FileVersion]] = {}
        self._max_cache_size = max_cache_size
        self._cache_access_order: List[str] = []  # For LRU eviction
    
    def store_version(self, file_id: str, pass_number: int, content: str, 
                     file_path: Optional[str] = None, metrics: Optional[Dict] = None,
                     metadata: Optional[Dict] = None) -> FileVersion:
        """Store a file version for a specific pass."""
        
        version = FileVersion(
            file_id=file_id,
            pass_number=pass_number,
            content=content,
            timestamp=time.time(),
            file_path=file_path,
            metrics=metrics,
            metadata=metadata
        )
        
        # Store in cache with LRU management
        if file_id not in self._versions_cache:
            self._versions_cache[file_id] = {}
        self._versions_cache[file_id][pass_number] = version
        
        # Update access order for LRU
        if file_id in self._cache_access_order:
            self._cache_access_order.remove(file_id)
        self._cache_access_order.append(file_id)
        
        # Check cache size and evict if necessary
        self._manage_cache_size()
        
        # Persist to disk
        self._persist_version(version)
        
        return version
    
    def get_version(self, file_id: str, pass_number: int) -> Optional[FileVersion]:
        """Get a specific file version by pass number."""
        
        # Check cache first
        if file_id in self._versions_cache and pass_number in self._versions_cache[file_id]:
            return self._versions_cache[file_id][pass_number]
        
        # Load from disk
        version = self._load_version(file_id, pass_number)
        if version:
            # Update cache
            if file_id not in self._versions_cache:
                self._versions_cache[file_id] = {}
            self._versions_cache[file_id][pass_number] = version
        
        return version
    
    def _manage_cache_size(self):
        """Manage cache size using LRU eviction."""
        total_entries = sum(len(versions) for versions in self._versions_cache.values())
        
        while total_entries > self._max_cache_size and self._cache_access_order:
            # Remove least recently used file
            lru_file_id = self._cache_access_order.pop(0)
            if lru_file_id in self._versions_cache:
                del self._versions_cache[lru_file_id]
                total_entries = sum(len(versions) for versions in self._versions_cache.values())
    
    def get_all_versions(self, file_id: str) -> Dict[int, FileVersion]:
        """Get all versions for a file."""
        
        # Load all versions from disk if not in cache
        if file_id not in self._versions_cache:
            self._load_all_versions(file_id)
        
        return self._versions_cache.get(file_id, {})
    
    def get_latest_version(self, file_id: str) -> Optional[FileVersion]:
        """Get the latest version of a file."""
        versions = self.get_all_versions(file_id)
        if not versions:
            return None
        
        latest_pass = max(versions.keys())
        return versions[latest_pass]
    
    def _persist_version(self, version: FileVersion):
        """Persist a version to disk."""
        file_dir = self.storage_dir / version.file_id
        file_dir.mkdir(exist_ok=True)
        
        version_file = file_dir / f"pass_{version.pass_number}.json"
        
        # Store metadata
        version_data = asdict(version)
        with open(version_file, 'w', encoding='utf-8') as f:
            json.dump(version_data, f, indent=2, ensure_ascii=False)
        
        # Store content separately if it's large
        if len(version.content) > 10000:  # 10KB threshold
            content_file = file_dir / f"pass_{version.pass_number}_content.txt"
            with open(content_file, 'w', encoding='utf-8') as f:
                f.write(version.content)
            
            # Update version data to reference content file
            version_data['content_file'] = str(content_file)
            version_data['content'] = ''  # Clear content from JSON
            
            with open(version_file, 'w', encoding='utf-8') as f:
                json.dump(version_data, f, indent=2, ensure_ascii=False)
    
    def _load_version(self, file_id: str, pass_number: int) -> Optional[FileVersion]:
        """Load a version from disk."""
        version_file = self.storage_dir / file_id / f"pass_{pass_number}.json"
        
        if not version_file.exists():
            return None
        
        try:
            with open(version_file, 'r', encoding='utf-8') as f:
                version_data = json.load(f)
            
            # Load content from separate file if needed
            if 'content_file' in version_data:
                content_file = Path(version_data['content_file'])
                if content_file.exists():
                    with open(content_file, 'r', encoding='utf-8') as f:
                        version_data['content'] = f.read()
                del version_data['content_file']
            
            return FileVersion(**version_data)
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"Error loading version {file_id}:{pass_number}: {e}")
            return None
    
    def _load_all_versions(self, file_id: str):
        """Load all versions for a file from disk."""
        file_dir = self.storage_dir / file_id
        
        if not file_dir.exists():
            self._versions_cache[file_id] = {}
            return
        
        versions = {}
        for version_file in file_dir.glob("pass_*.json"):
            try:
                pass_number = int(version_file.stem.split('_')[1])
                version = self._load_version(file_id, pass_number)
                if version:
                    versions[pass_number] = version
            except (ValueError, IndexError):
                continue
        
        self._versions_cache[file_id] = versions
    
    def cleanup_old_versions(self, file_id: str, keep_latest: int = 5):
        """Clean up old versions, keeping only the latest N versions."""
        versions = self.get_all_versions(file_id)
        
        if len(versions) <= keep_latest:
            return
        
        # Sort by pass number and remove old ones
        sorted_passes = sorted(versions.keys())
        passes_to_remove = sorted_passes[:-keep_latest]
        
        for pass_num in passes_to_remove:
            self._remove_version(file_id, pass_num)
    
    def _remove_version(self, file_id: str, pass_number: int):
        """Remove a specific version."""
        # Remove from cache
        if file_id in self._versions_cache and pass_number in self._versions_cache[file_id]:
            del self._versions_cache[file_id][pass_number]
        
        # Remove from disk
        version_file = self.storage_dir / file_id / f"pass_{pass_number}.json"
        content_file = self.storage_dir / file_id / f"pass_{pass_number}_content.txt"
        
        if version_file.exists():
            version_file.unlink()
        if content_file.exists():
            content_file.unlink()

# Global instance
file_version_manager = FileVersionManager()






