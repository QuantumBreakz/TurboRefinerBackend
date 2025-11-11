#!/usr/bin/env python3
"""
Example demonstrating the enhanced logging capabilities.

Run with DEBUG=1 for console output:
    DEBUG=1 python logging_example.py

Run normally for file-only logging:
    python logging_example.py
"""

import os
import time
from logger import get_logger, log_event, log_exception, log_json, log_metrics, log_performance


def main():
    """Demonstrate various logging features."""
    
    # Get logger instance
    logger = get_logger('example')
    
    # Basic logging
    logger.info("Starting logging demonstration")
    log_event("DEMO_START", "Enhanced logging system demonstration")
    
    # Structured JSON logging for analytics
    log_json("USER_ACTION", "Started demo", 
             user_id="demo_user", 
             session_id="demo_123",
             feature="enhanced_logging")
    
    # Performance logging
    start_time = time.perf_counter()
    time.sleep(0.1)  # Simulate work
    duration = (time.perf_counter() - start_time) * 1000
    log_performance("DEMO_PROCESSING", duration, 
                   items_processed=42,
                   processing_type="batch")
    
    # Metrics logging
    log_metrics("DEMO_METRICS", {
        "accuracy": 0.95,
        "precision": 0.92,
        "recall": 0.88,
        "f1_score": 0.90,
        "processing_time_ms": duration
    })
    
    # Exception logging with full traceback
    try:
        # Simulate an error
        result = 1 / 0
    except Exception as e:
        log_exception("DEMO_ERROR", e)
    
    # Different log levels
    logger.debug("Debug message - only visible with DEBUG=1")
    logger.info("Info message - always visible")
    logger.warning("Warning message - important but not critical")
    logger.error("Error message - something went wrong")
    
    # Legacy compatibility
    from logger import log_event_legacy
    log_event_legacy("LEGACY_DEMO", "This still works for backward compatibility")
    
    logger.info("Logging demonstration complete")
    print("\n✅ Logging demonstration complete!")
    print("📁 Check the logs/ directory for output files")
    print("🔄 Logs are automatically rotated when they reach 10MB")
    print("📊 JSON logs are ready for analytics and ML pipelines")


if __name__ == "__main__":
    main()
