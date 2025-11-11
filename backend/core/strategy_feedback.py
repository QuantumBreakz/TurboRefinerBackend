"""
Strategy feedback storage and analysis system.
"""

import json
import time
import os
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

@dataclass
class StrategyFeedback:
    feedback_id: str
    user_id: str
    weights: Dict[str, float]  # e.g., {"clarity": 0.8, "persuasion": 0.7}
    thumbs: str  # "up" or "down"
    timestamp: float
    file_id: Optional[str] = None
    pass_number: Optional[int] = None
    rationale: Optional[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

class StrategyFeedbackManager:
    """Manages strategy feedback storage and analysis."""
    
    def __init__(self, storage_dir: str = "strategy_feedback"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        
        # In-memory cache for active feedback
        self._feedback_cache: Dict[str, List[StrategyFeedback]] = {}
        
        # Default strategy weights
        self._default_weights = {
            "clarity": 0.7,
            "persuasion": 0.6,
            "brevity": 0.5,
            "formality": 0.5,
            "originality": 0.6,
            "scanner_risk": 0.8  # Lower is better for scanner risk
        }
    
    def store_feedback(self, feedback: StrategyFeedback) -> str:
        """Store user feedback for a given strategy."""
        if feedback.user_id not in self._feedback_cache:
            self._feedback_cache[feedback.user_id] = []
        
        self._feedback_cache[feedback.user_id].append(feedback)
        
        # Keep only the last 50 feedbacks per user
        self._feedback_cache[feedback.user_id] = self._feedback_cache[feedback.user_id][-50:]
        
        # Persist to disk
        self._persist_feedback(feedback)
        
        return feedback.feedback_id
    
    def get_user_feedback(self, user_id: str, limit: int = 20) -> List[StrategyFeedback]:
        """Retrieve recent feedback for a specific user."""
        if user_id not in self._feedback_cache:
            self._load_user_feedback(user_id)
        
        feedback_list = self._feedback_cache.get(user_id, [])
        return feedback_list[-limit:] if limit else feedback_list
    
    def get_strategy_recommendations(self, user_id: str, current_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Generate strategy recommendations based on user feedback and current metrics."""
        feedback_history = self.get_user_feedback(user_id, limit=10)
        
        # Start with default weights
        effective_weights = self._default_weights.copy()
        
        if not feedback_history:
            return {
                "effective_weights": effective_weights,
                "recommendation_message": "No feedback yet, using default strategy.",
                "suggested_actions": []
            }
        
        # Aggregate feedback
        up_votes = [f for f in feedback_history if f.thumbs == "up"]
        down_votes = [f for f in feedback_history if f.thumbs == "down"]
        
        # Adjust weights based on feedback
        for feedback in up_votes:
            for key, value in feedback.weights.items():
                if key in effective_weights:
                    effective_weights[key] = min(1.0, effective_weights[key] + value * 0.1)
        
        for feedback in down_votes:
            for key, value in feedback.weights.items():
                if key in effective_weights:
                    effective_weights[key] = max(0.0, effective_weights[key] - value * 0.15)
        
        # Normalize weights (optional, but good for consistent scaling)
        total_weight = sum(effective_weights.values())
        if total_weight > 0:
            effective_weights = {k: v / total_weight for k, v in effective_weights.items()}
        
        recommendation_message = "Strategy adjusted based on your recent feedback."
        suggested_actions = []
        
        # Example of using current_metrics (simplified)
        if current_metrics.get("scanner_risk", 0.0) > 0.5 and effective_weights.get("scanner_risk", 0) < 0.7:
            suggested_actions.append("Consider increasing 'anti_scanner_techniques' for this text.")
            effective_weights["scanner_risk"] = min(1.0, effective_weights["scanner_risk"] + 0.1)
        
        return {
            "effective_weights": effective_weights,
            "recommendation_message": recommendation_message,
            "suggested_actions": suggested_actions
        }
    
    def _persist_feedback(self, feedback: StrategyFeedback):
        """Persist feedback to disk."""
        user_dir = self.storage_dir / feedback.user_id
        user_dir.mkdir(exist_ok=True)
        
        feedback_file = user_dir / f"feedback_{feedback.feedback_id}.json"
        
        feedback_data = asdict(feedback)
        with open(feedback_file, 'w', encoding='utf-8') as f:
            json.dump(feedback_data, f, indent=2, ensure_ascii=False)
    
    def _load_user_feedback(self, user_id: str):
        """Load user feedback from disk."""
        user_dir = self.storage_dir / user_id
        
        if not user_dir.exists():
            self._feedback_cache[user_id] = []
            return
        
        feedback_list = []
        for feedback_file in user_dir.glob("feedback_*.json"):
            try:
                with open(feedback_file, 'r', encoding='utf-8') as f:
                    feedback_data = json.load(f)
                
                feedback = StrategyFeedback(**feedback_data)
                feedback_list.append(feedback)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"Error loading feedback {feedback_file}: {e}")
                continue
        
        # Sort by timestamp
        feedback_list.sort(key=lambda x: x.timestamp)
        self._feedback_cache[user_id] = feedback_list
    
    def clear_user_feedback(self, user_id: str):
        """Clear all feedback for a specific user."""
        if user_id in self._feedback_cache:
            del self._feedback_cache[user_id]
        
        user_dir = self.storage_dir / user_id
        if user_dir.exists():
            for feedback_file in user_dir.glob("feedback_*.json"):
                feedback_file.unlink()
            user_dir.rmdir()
    
    def cleanup_old_feedback(self, days_to_keep: int = 90):
        """Clean up feedback older than specified days."""
        cutoff_time = time.time() - (days_to_keep * 24 * 3600)
        
        for user_id in list(self._feedback_cache.keys()):
            user_feedback = self._feedback_cache[user_id]
            kept_feedback = []
            
            for feedback in user_feedback:
                if feedback.timestamp > cutoff_time:
                    kept_feedback.append(feedback)
                else:
                    # Remove old feedback file
                    user_dir = self.storage_dir / user_id
                    feedback_file = user_dir / f"feedback_{feedback.feedback_id}.json"
                    if feedback_file.exists():
                        feedback_file.unlink()
            
            self._feedback_cache[user_id] = kept_feedback

# Global instance
strategy_feedback_manager = StrategyFeedbackManager()


