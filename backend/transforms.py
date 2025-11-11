from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Union
from enum import Enum

from .logger import get_logger, log_performance, log_exception

logger = get_logger(__name__)


class TransformResult:
    """Result of a transform operation with metadata."""
    
    def __init__(self, text: str, success: bool = True, error: Optional[Exception] = None, 
                 metadata: Optional[Dict[str, Any]] = None):
        self.text = text
        self.success = success
        self.error = error
        self.metadata = metadata or {}
        self.timestamp = time.time()
    
    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"TransformResult({status}, len={len(self.text)})"


class TransformSeverity(Enum):
    """Severity levels for transforms."""
    LOW = "low"           # Non-destructive, safe operations
    MEDIUM = "medium"     # Some risk, validation recommended
    HIGH = "high"         # Destructive, requires validation
    CRITICAL = "critical" # High risk, requires rollback capability


class TextTransform(Protocol):
    """Enhanced protocol for text transformations."""
    
    def apply(self, text: str, **kwargs: Any) -> TransformResult: ...
    def name(self) -> str: ...
    def severity(self) -> TransformSeverity: ...
    def validate_input(self, text: str) -> bool: ...
    def validate_output(self, result: TransformResult) -> bool: ...


@dataclass
class FunctionTransform:
    """Enhanced function-based transform with validation and error handling."""
    
    fn: Callable[[str], str]
    label: str
    severity: TransformSeverity = TransformSeverity.MEDIUM
    input_validator: Optional[Callable[[str], bool]] = None
    output_validator: Optional[Callable[[TransformResult], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def apply(self, text: str, **kwargs: Any) -> TransformResult:
        """Apply the transform with comprehensive error handling and validation."""
        start_time = time.perf_counter()
        
        # Input validation
        if not self.validate_input(text):
            error_msg = f"Input validation failed for transform '{self.label}'"
            logger.warning(error_msg)
            return TransformResult(
                text=text, 
                success=False, 
                error=ValueError(error_msg),
                metadata={"validation_failed": "input"}
            )
        
        try:
            # Apply the transform
            result_text = self.fn(text)
            
            # Create result
            result = TransformResult(
                text=result_text,
                success=True,
                metadata={
                    **self.metadata,
                    "original_length": len(text),
                    "result_length": len(result_text),
                    "duration_ms": (time.perf_counter() - start_time) * 1000
                }
            )
            
            # Output validation
            if not self.validate_output(result):
                error_msg = f"Output validation failed for transform '{self.label}'"
                logger.warning(error_msg)
                result.success = False
                result.error = ValueError(error_msg)
                result.metadata["validation_failed"] = "output"
            
            # Log performance
            log_performance("TRANSFORM_APPLY", result.metadata["duration_ms"],
                          transform_name=self.label,
                          severity=self.severity.value,
                          success=result.success)
            
            return result
            
        except Exception as e:
            duration = (time.perf_counter() - start_time) * 1000
            log_exception("TRANSFORM_ERROR", e)
            
            return TransformResult(
                text=text,  # Return original on failure
                success=False,
                error=e,
                metadata={
                    **self.metadata,
                    "duration_ms": duration,
                    "error_type": type(e).__name__
                }
            )
    
    def name(self) -> str:
        return self.label
    
    def severity(self) -> TransformSeverity:
        return self.severity
    
    def validate_input(self, text: str) -> bool:
        """Validate input text before transformation."""
        if self.input_validator:
            return self.input_validator(text)
        
        # Default validation
        return isinstance(text, str) and len(text.strip()) > 0
    
    def validate_output(self, result: TransformResult) -> bool:
        """Validate output after transformation."""
        if self.output_validator:
            return self.output_validator(result)
        
        # Default validation
        return isinstance(result.text, str) and result.success


@dataclass
class ConditionalTransform:
    """Transform that applies conditionally based on input criteria."""
    
    transform: TextTransform
    condition: Callable[[str], bool]
    label: str
    
    def apply(self, text: str, **kwargs: Any) -> TransformResult:
        if self.condition(text):
            return self.transform.apply(text, **kwargs)
        else:
            return TransformResult(
                text=text,
                success=True,
                metadata={"skipped": True, "reason": "condition_not_met"}
            )
    
    def name(self) -> str:
        return self.label
    
    def severity(self) -> TransformSeverity:
        return self.transform.severity()
    
    def validate_input(self, text: str) -> bool:
        return self.transform.validate_input(text)
    
    def validate_output(self, result: TransformResult) -> bool:
        return self.transform.validate_output(result)


@dataclass
class CompositeTransform:
    """Transform that combines multiple transforms with rollback capability."""
    
    transforms: List[TextTransform]
    label: str
    rollback_on_failure: bool = True
    stop_on_first_failure: bool = False
    
    def apply(self, text: str, **kwargs: Any) -> TransformResult:
        """Apply all transforms with rollback capability."""
        start_time = time.perf_counter()
        results = []
        current_text = text
        checkpoints = [text]  # For rollback
        
        for i, transform in enumerate(self.transforms):
            result = transform.apply(current_text, **kwargs)
            results.append(result)
            
            if not result.success:
                logger.warning(f"Transform {i+1}/{len(self.transforms)} failed: {transform.name()}")
                
                if self.stop_on_first_failure:
                    if self.rollback_on_failure:
                        return TransformResult(
                            text=checkpoints[-1],
                            success=False,
                            error=result.error,
                            metadata={
                                "failed_at_transform": i,
                                "failed_transform": transform.name(),
                                "rollback_performed": True
                            }
                        )
                    else:
                        return result
            
            # Update text and create checkpoint
            current_text = result.text
            checkpoints.append(current_text)
        
        # All transforms succeeded
        duration = (time.perf_counter() - start_time) * 1000
        return TransformResult(
            text=current_text,
            success=True,
            metadata={
                "total_transforms": len(self.transforms),
                "duration_ms": duration,
                "individual_results": [r.metadata for r in results]
            }
        )
    
    def name(self) -> str:
        return self.label
    
    def severity(self) -> TransformSeverity:
        # Use highest severity among all transforms
        severities = [t.severity() for t in self.transforms]
        severity_order = [TransformSeverity.LOW, TransformSeverity.MEDIUM, 
                         TransformSeverity.HIGH, TransformSeverity.CRITICAL]
        return max(severities, key=lambda s: severity_order.index(s))
    
    def validate_input(self, text: str) -> bool:
        return all(t.validate_input(text) for t in self.transforms)
    
    def validate_output(self, result: TransformResult) -> bool:
        return result.success


def build_profile(transforms: List[TextTransform], 
                 name: str = "custom_profile",
                 enable_rollback: bool = True) -> Callable[[str], TransformResult]:
    """Build a transform profile with enhanced error handling and rollback."""
    
    def run(text: str, **kwargs: Any) -> TransformResult:
        """Execute the transform profile."""
        if not transforms:
            return TransformResult(text=text, success=True, metadata={"empty_profile": True})
        
        # Use CompositeTransform for advanced features
        composite = CompositeTransform(
            transforms=transforms,
            label=name,
            rollback_on_failure=enable_rollback,
            stop_on_first_failure=False
        )
        
        return composite.apply(text, **kwargs)
    
    return run


def create_safe_transform(fn: Callable[[str], str], 
                         label: str,
                         severity: TransformSeverity = TransformSeverity.LOW) -> FunctionTransform:
    """Create a transform with safe defaults."""
    
    def input_validator(text: str) -> bool:
        return isinstance(text, str) and len(text) > 0
    
    def output_validator(result: TransformResult) -> bool:
        return (isinstance(result.text, str) and 
                len(result.text) > 0 and
                result.success)
    
    return FunctionTransform(
        fn=fn,
        label=label,
        severity=severity,
        input_validator=input_validator,
        output_validator=output_validator
    )


def create_conditional_transform(transform: TextTransform,
                               condition: Callable[[str], bool],
                               label: str) -> ConditionalTransform:
    """Create a conditional transform wrapper."""
    return ConditionalTransform(
        transform=transform,
        condition=condition,
        label=label
    )


# Common validators
def min_length_validator(min_len: int = 1) -> Callable[[str], bool]:
    """Create a minimum length validator."""
    return lambda text: len(text) >= min_len


def max_length_validator(max_len: int = 1000000) -> Callable[[str], bool]:
    """Create a maximum length validator."""
    return lambda text: len(text) <= max_len


def contains_text_validator(required_text: str) -> Callable[[str], bool]:
    """Create a validator that requires specific text."""
    return lambda text: required_text in text


def not_empty_validator(text: str) -> bool:
    """Validator that ensures text is not empty."""
    return len(text.strip()) > 0





