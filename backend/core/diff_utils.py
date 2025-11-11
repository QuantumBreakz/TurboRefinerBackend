"""
Diff utilities for comparing text versions and generating structured diffs.
"""

import difflib
import re
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum

class ChangeType(Enum):
    INSERT = "insert"
    DELETE = "delete"
    REPLACE = "replace"
    UNCHANGED = "unchanged"

@dataclass
class TextChange:
    type: ChangeType
    original_text: str
    new_text: str
    position: Dict[str, int]  # {"start": int, "end": int}
    confidence: float = 1.0
    context: str = ""

@dataclass
class DiffStatistics:
    total_changes: int
    insertions: int
    deletions: int
    replacements: int
    words_changed: int
    characters_changed: int
    similarity_score: float
    unchanged_words: int
    changed_sentences: int

@dataclass
class DiffResult:
    file_id: str
    from_pass: int
    to_pass: int
    mode: str
    changes: List[TextChange]
    statistics: DiffStatistics
    metadata: Dict[str, Any]

def calculate_similarity(text1: str, text2: str) -> float:
    """Calculate similarity score between two texts using sequence matcher."""
    matcher = difflib.SequenceMatcher(None, text1, text2)
    return matcher.ratio()

def word_diff(original: str, modified: str) -> List[TextChange]:
    """Generate word-level diff between two texts."""
    changes = []
    
    # Split into words while preserving whitespace
    original_words = re.findall(r'\S+|\s+', original)
    modified_words = re.findall(r'\S+|\s+', modified)
    
    # Use difflib to find differences
    matcher = difflib.SequenceMatcher(None, original_words, modified_words)
    
    position = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        original_segment = ''.join(original_words[i1:i2])
        modified_segment = ''.join(modified_words[j1:j2])
        
        if tag == 'equal':
            # Unchanged text
            position += len(original_segment)
            continue
        elif tag == 'delete':
            # Text was removed
            changes.append(TextChange(
                type=ChangeType.DELETE,
                original_text=original_segment,
                new_text="",
                position={"start": position, "end": position + len(original_segment)},
                confidence=1.0,
                context=f"Removed: {original_segment[:50]}..."
            ))
            position += len(original_segment)
        elif tag == 'insert':
            # Text was added
            changes.append(TextChange(
                type=ChangeType.INSERT,
                original_text="",
                new_text=modified_segment,
                position={"start": position, "end": position},
                confidence=1.0,
                context=f"Added: {modified_segment[:50]}..."
            ))
        elif tag == 'replace':
            # Text was replaced
            changes.append(TextChange(
                type=ChangeType.REPLACE,
                original_text=original_segment,
                new_text=modified_segment,
                position={"start": position, "end": position + len(original_segment)},
                confidence=0.8,  # Lower confidence for replacements
                context=f"Replaced: {original_segment[:30]}... → {modified_segment[:30]}..."
            ))
            position += len(original_segment)
    
    return changes

def sentence_diff(original: str, modified: str) -> List[TextChange]:
    """Generate sentence-level diff between two texts."""
    changes = []
    
    # Split into sentences (simple approach)
    sentence_pattern = r'[.!?]+\s+'
    original_sentences = re.split(sentence_pattern, original)
    modified_sentences = re.split(sentence_pattern, modified)
    
    # Clean up empty sentences
    original_sentences = [s.strip() for s in original_sentences if s.strip()]
    modified_sentences = [s.strip() for s in modified_sentences if s.strip()]
    
    matcher = difflib.SequenceMatcher(None, original_sentences, modified_sentences)
    
    position = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        original_segment = '. '.join(original_sentences[i1:i2])
        modified_segment = '. '.join(modified_sentences[j1:j2])
        
        if tag == 'equal':
            position += len(original_segment) + 2  # +2 for ". "
            continue
        elif tag == 'delete':
            changes.append(TextChange(
                type=ChangeType.DELETE,
                original_text=original_segment,
                new_text="",
                position={"start": position, "end": position + len(original_segment)},
                confidence=1.0,
                context=f"Sentence removed"
            ))
            position += len(original_segment) + 2
        elif tag == 'insert':
            changes.append(TextChange(
                type=ChangeType.INSERT,
                original_text="",
                new_text=modified_segment,
                position={"start": position, "end": position},
                confidence=1.0,
                context=f"New sentence added"
            ))
        elif tag == 'replace':
            changes.append(TextChange(
                type=ChangeType.REPLACE,
                original_text=original_segment,
                new_text=modified_segment,
                position={"start": position, "end": position + len(original_segment)},
                confidence=0.7,
                context=f"Sentence rewritten"
            ))
            position += len(original_segment) + 2
    
    return changes

def calculate_statistics(changes: List[TextChange], original: str, modified: str) -> DiffStatistics:
    """Calculate comprehensive diff statistics."""
    insertions = sum(1 for c in changes if c.type == ChangeType.INSERT)
    deletions = sum(1 for c in changes if c.type == ChangeType.DELETE)
    replacements = sum(1 for c in changes if c.type == ChangeType.REPLACE)
    
    words_changed = sum(
        len(c.original_text.split()) + len(c.new_text.split())
        for c in changes
    )
    
    characters_changed = sum(
        len(c.original_text) + len(c.new_text)
        for c in changes
    )
    
    similarity_score = calculate_similarity(original, modified)
    
    # Count unchanged words
    original_words = len(original.split())
    modified_words = len(modified.split())
    unchanged_words = min(original_words, modified_words) - words_changed
    
    # Count changed sentences (approximate)
    changed_sentences = len([c for c in changes if c.type != ChangeType.UNCHANGED])
    
    return DiffStatistics(
        total_changes=len(changes),
        insertions=insertions,
        deletions=deletions,
        replacements=replacements,
        words_changed=words_changed,
        characters_changed=characters_changed,
        similarity_score=similarity_score,
        unchanged_words=unchanged_words,
        changed_sentences=changed_sentences
    )

def generate_diff(
    file_id: str,
    from_pass: int,
    to_pass: int,
    original_text: str,
    modified_text: str,
    mode: str = "sentence"
) -> DiffResult:
    """Generate a comprehensive diff between two text versions."""
    
    if mode == "word":
        changes = word_diff(original_text, modified_text)
    else:  # sentence mode (default)
        changes = sentence_diff(original_text, modified_text)
    
    statistics = calculate_statistics(changes, original_text, modified_text)
    
    metadata = {
        "processing_time": 0.1,  # Placeholder - would be actual timing
        "strategy_used": "microstructure_control",
        "entropy_level": "medium",
        "original_length": len(original_text),
        "modified_length": len(modified_text),
        "length_change_percent": ((len(modified_text) - len(original_text)) / len(original_text)) * 100 if original_text else 0
    }
    
    return DiffResult(
        file_id=file_id,
        from_pass=from_pass,
        to_pass=to_pass,
        mode=mode,
        changes=changes,
        statistics=statistics,
        metadata=metadata
    )

def format_change_for_api(change: TextChange) -> Dict[str, Any]:
    """Convert TextChange to API response format."""
    return {
        "type": change.type.value,
        "originalText": change.original_text,
        "newText": change.new_text,
        "position": change.position,
        "confidence": change.confidence,
        "context": change.context
    }

def format_statistics_for_api(stats: DiffStatistics) -> Dict[str, Any]:
    """Convert DiffStatistics to API response format."""
    return {
        "totalChanges": stats.total_changes,
        "insertions": stats.insertions,
        "deletions": stats.deletions,
        "replacements": stats.replacements,
        "wordsChanged": stats.words_changed,
        "charactersChanged": stats.characters_changed,
        "similarityScore": round(stats.similarity_score, 3),
        "unchangedWords": stats.unchanged_words,
        "changedSentences": stats.changed_sentences
    }




