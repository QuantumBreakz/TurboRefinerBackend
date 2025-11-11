from __future__ import annotations

import os
from typing import Protocol, Optional, TYPE_CHECKING

from .logger import get_logger, log_exception

if TYPE_CHECKING:
    # Only imported for type checking; avoids hard dependency at runtime
    from google.auth.credentials import Credentials  # type: ignore

logger = get_logger(__name__)


class OutputSink(Protocol):
    def write(self, local_tmp_path: str, dest_name: str) -> str: ...


class LocalSink:
    def __init__(self, output_dir: str) -> None:
        self.output_dir = output_dir
        # Ensure destination directory exists
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception as e:
            # Log and re-raise to surface configuration errors early
            log_exception("LOCAL_SINK_INIT", e)
            raise

    def write(self, local_tmp_path: str, dest_name: str) -> str:
        # Preserve original extension if dest_name has none
        ext = os.path.splitext(local_tmp_path)[1]
        if not os.path.splitext(dest_name)[1] and ext:
            dest_name = dest_name + ext
        dest = os.path.join(self.output_dir, dest_name)

        # Validate source file exists
        if not os.path.exists(local_tmp_path):
            raise FileNotFoundError(f"Source file does not exist: {local_tmp_path}")

        # Generate unique filename if destination exists
        if os.path.exists(dest):
            base_name, ext = os.path.splitext(dest_name)
            counter = 1
            while os.path.exists(dest):
                # Create variation with similarity indicator
                variation_name = f"{base_name}_sim{counter}{ext}"
                dest = os.path.join(self.output_dir, variation_name)
                counter += 1
                # Prevent infinite loop
                if counter > 1000:
                    logger.warning(f"Too many similar files for {dest_name}, using timestamp")
                    from datetime import datetime
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    variation_name = f"{base_name}_sim_{timestamp}{ext}"
                    dest = os.path.join(self.output_dir, variation_name)
                    break
            logger.debug(f"Generated unique filename: {os.path.basename(dest)}")

        # Perform atomic move into destination directory
        try:
            os.replace(local_tmp_path, dest)
            return dest
        except Exception as e:
            log_exception("LOCAL_SINK_WRITE", e)
            raise


class DriveSink:
    def __init__(self, folder_id: str, creds: "Credentials") -> None:
        self.folder_id = folder_id
        self.creds = creds

    def write(self, local_tmp_path: str, dest_name: str) -> str:
        from .utils import create_google_doc

        if not os.path.exists(local_tmp_path):
            raise FileNotFoundError(f"Source file does not exist: {local_tmp_path}")

        # Attempt upload; ensure local temp cleanup on success
        try:
            doc_id = create_google_doc(local_tmp_path, dest_name, self.folder_id, self.creds)
        except Exception as e:
            log_exception("DRIVE_SINK_UPLOAD", e)
            raise RuntimeError(f"Failed to upload '{dest_name}' to Google Drive: {e}") from e
        else:
            # Best-effort cleanup of local temp file after successful upload
            try:
                os.remove(local_tmp_path)
            except Exception:
                # Non-fatal; keep going if cleanup fails
                pass
            return doc_id


