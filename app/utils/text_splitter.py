"""
Text processing utilities for dental RAG chunking.

Includes:
  - Dental-specific noise filtering (page headers, printer marks, OCR artifacts)
  - Smart sentence-aware splitting with word-level overlap
  - Incomplete sentence detection and repair
  - Structured embedding text formatting
  - Deduplication helpers
"""
import re
from typing import List, Optional, Set, Tuple

import tiktoken

# ── Tokenizer ──────────────────────────────────────────────────────────────────
_enc = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in a string."""
    return len(_enc.encode(text))


def count_words(text: str) -> int:
    return len(text.split())


def split_words(text: str) -> List[str]:
    return text.split()


# ── Chunking Config ────────────────────────────────────────────────────────────
MAX_WORDS_PER_TEXT_CHUNK = 1000
OVERLAP_WORDS = 100
MIN_PARAGRAPH_WORDS = 18
MIN_FINAL_TEXT_CHUNK_WORDS = 300
MAX_FINAL_TEXT_CHUNK_WORDS = 1200
MIN_SPLIT_TAIL_WORDS = 150
SHORT_HEADING_MAX_WORDS = 10

# ── Regex Patterns ─────────────────────────────────────────────────────────────
HEADING_ROLES = {"title", "sectionHeading", "heading"}

FIGURE_REF_RE = re.compile(
    r"\b(?:fig(?:ure)?\.?)\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE
)
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
PAGE_NUMBER_ONLY_RE = re.compile(r"^\s*\d{1,4}\s*$")
IMAGE_MARKER_RE = re.compile(r"^<IMAGE FOR PAGE:", re.IGNORECASE)
PARSED_PAGE_MARKER_RE = re.compile(r"^<PARSED TEXT FOR PAGE:", re.IGNORECASE)
PURE_FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?\.?)\s*\d+(?:\.\d+)*\b.*$", re.IGNORECASE
)
NOISE_LINE_PATTERNS = [
    re.compile(r"^ch\d+[-\w]*\.indd\s+\d+\s+\d+/\d+/\d+", re.IGNORECASE),
]
PAGE_HEADER_RE = re.compile(r"^\s*\d+\s*/\s*[A-Za-z].*")
SENTENCE_END_RE = re.compile(r'[.!?]["\')\\]]?$')
LOWER_START_RE = re.compile(r"^[a-z]")
BAD_SINGLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9]$")
GENERIC_CHAPTER_HEADING_RE = re.compile(r"^\d+\s*(?:/|\b)")
DUPLICATE_LINE_NORMALIZER_RE = re.compile(r"\s+")
TABLE_REF_RE = re.compile(r"^\s*table\s+\d+(?:\.\d+)*\b.*$", re.IGNORECASE)
FIGURE_CALLOUT_ONLY_RE = re.compile(
    r"^\s*(?:\(?[a-zA-Z]\)?(?:\s*,?\s*|\s+)){2,10}\s*$"
)
TABLE_ROWISH_RE = re.compile(r"^\s*(?:[^\n|]+\|){2,}[^\n|]+\s*$")


# ── Text Normalization ─────────────────────────────────────────────────────────

def normalize_whitespace(text: str) -> str:
    """Normalize unicode spaces, collapse runs, cap blank lines."""
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_for_compare(text: str) -> str:
    text = normalize_whitespace(text).lower()
    return DUPLICATE_LINE_NORMALIZER_RE.sub(" ", text).strip()


# ── Noise Detection ───────────────────────────────────────────────────────────

def is_noise_line(text: str) -> bool:
    """Detect lines that are page numbers, headers, printer marks, etc."""
    t = normalize_whitespace(text)
    if not t:
        return True
    if PAGE_NUMBER_ONLY_RE.match(t):
        return True
    if PAGE_HEADER_RE.match(t):
        return True
    if IMAGE_MARKER_RE.match(t):
        return True
    if PARSED_PAGE_MARKER_RE.match(t):
        return True
    for pattern in NOISE_LINE_PATTERNS:
        if pattern.match(t):
            return True
    return False


def clean_inline_noise_tokens(text: str) -> str:
    """Remove single-character OCR artifacts and stray 'O'/'o' tokens."""
    text = normalize_whitespace(text)
    if not text:
        return ""
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    cleaned_parts: List[str] = []
    for part in parts:
        if BAD_SINGLE_TOKEN_RE.match(part) and len(parts) > 1:
            continue
        cleaned_parts.append(part)
    text = "\n".join(cleaned_parts)
    text = normalize_whitespace(text)
    # Remove stray single O/o not part of a word
    text = re.sub(r"(?<!\w)\b[Oo]\b(?!\w)", "", text)
    text = normalize_whitespace(text)
    return text


def clean_paragraph_text(text: str) -> str:
    """Full cleaning pipeline for a single paragraph."""
    text = normalize_whitespace(text)
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines: List[str] = []
    for line in lines:
        if not line:
            continue
        if is_noise_line(line):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = normalize_whitespace(cleaned)
    # Rejoin hyphenated line breaks (e.g. "den-\ntist" → "dentist")
    cleaned = re.sub(r"(\w)-\s+(\w)", r"\1\2", cleaned)
    cleaned = clean_inline_noise_tokens(cleaned)
    return cleaned.strip()


# ── Heading Detection ──────────────────────────────────────────────────────────

def looks_like_local_heading(text: str) -> bool:
    """Detect implicit headings (short ALL-CAPS or Title-Case lines)."""
    t = normalize_whitespace(text)
    if not t:
        return False
    if is_probable_figure_caption(t):
        return False
    if TABLE_REF_RE.match(t):
        return False
    if TABLE_ROWISH_RE.match(t):
        return False
    if FIGURE_CALLOUT_ONLY_RE.match(t):
        return False
    if len(t) < 2:
        return False
    wc = count_words(t)
    if wc == 0 or wc > SHORT_HEADING_MAX_WORDS:
        return False
    if SENTENCE_END_RE.search(t):
        return False
    alpha_chars = [c for c in t if c.isalpha()]
    if not alpha_chars:
        return False
    upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
    titleish = t == t.title()
    return upper_ratio > 0.7 or titleish


def is_probable_figure_caption(text: str) -> bool:
    t = normalize_whitespace(text)
    if not t:
        return False
    return PURE_FIGURE_CAPTION_RE.match(t) is not None


def looks_like_figure_callout_or_label(text: str) -> bool:
    t = normalize_whitespace(text)
    if not t:
        return False
    if FIGURE_CALLOUT_ONLY_RE.match(t):
        return True
    tokens = re.findall(r"[A-Za-z]", t)
    if 2 <= len(tokens) <= 10 and len(t.split()) <= 10:
        if all(len(tok.strip("()")) == 1 for tok in t.split()):
            return True
    return False


# ── Sentence Completeness Detection ────────────────────────────────────────────

def looks_incomplete_start(text: str) -> bool:
    """Detect if text starts mid-sentence (lowercase, continuation word)."""
    text = normalize_whitespace(text)
    if not text:
        return False
    first_word_match = re.match(r"^([^\s]+)", text)
    if not first_word_match:
        return False
    first_word = first_word_match.group(1).strip(".,;:()[]")
    if not first_word:
        return False
    if LOWER_START_RE.match(first_word):
        return True
    continuation_prefixes = {
        "and", "or", "but", "so", "because", "which", "that", "who", "whilst",
        "while", "where", "when", "with", "without", "depending", "more",
        "less", "then", "than", "thus", "therefore", "however", "nevertheless",
        "also", "further", "side", "end"
    }
    return first_word.lower() in continuation_prefixes


def looks_incomplete_end(text: str) -> bool:
    """Detect if text ends mid-sentence (no terminal punctuation, trailing preposition)."""
    text = normalize_whitespace(text)
    if not text:
        return False
    if SENTENCE_END_RE.search(text):
        return False
    last_word_match = re.search(r"([^\s]+)$", text)
    if not last_word_match:
        return False
    last_word = last_word_match.group(1).strip().strip(".,;:()[]")
    if not last_word:
        return True
    bad_end_words = {
        "the", "a", "an", "and", "or", "but", "to", "of", "for", "with",
        "by", "in", "on", "at", "from", "than", "as", "is", "are", "was",
        "were", "be", "been", "being", "if", "when", "where", "which", "that"
    }
    if last_word.lower() in bad_end_words:
        return True
    if text.endswith((",", ";", ":", "-", "–", "—")):
        return True
    return False


# ── Text Splitting ─────────────────────────────────────────────────────────────

def split_long_text(text: str, max_words: int, overlap_words: int) -> List[str]:
    """Split text into overlapping chunks, snapping to sentence boundaries.

    Uses word counts for size control but always breaks at sentence endings
    (period, exclamation, question mark) to preserve semantic completeness.
    Overlap is carried as trailing sentences from the previous chunk.
    """
    words = split_words(text)
    if len(words) <= max_words:
        return [text.strip()]

    # Split into sentences first
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return [text.strip()]

    output: List[str] = []
    current_sentences: List[str] = []
    current_words = 0

    for sentence in sentences:
        sent_words = count_words(sentence)

        # If a single sentence exceeds max, force split it at word boundaries
        if sent_words > max_words:
            if current_sentences:
                output.append(" ".join(current_sentences))
                current_sentences = []
                current_words = 0
            sub_words = split_words(sentence)
            for i in range(0, len(sub_words), max_words):
                piece = " ".join(sub_words[i:i + max_words])
                if piece:
                    output.append(piece)
            continue

        if current_words + sent_words > max_words and current_sentences:
            output.append(" ".join(current_sentences))

            # Overlap: carry back sentences from the end of the previous chunk
            overlap_sents: List[str] = []
            overlap_count = 0
            for s in reversed(current_sentences):
                s_words = count_words(s)
                if overlap_count + s_words > overlap_words:
                    break
                overlap_sents.insert(0, s)
                overlap_count += s_words

            current_sentences = overlap_sents
            current_words = overlap_count

        current_sentences.append(sentence)
        current_words += sent_words

    if current_sentences:
        output.append(" ".join(current_sentences))

    return output


async def semantic_grouping(
    text: str, 
    max_words: int, 
    embed_fn, 
    threshold: float = 0.85,
    overlap_sentences: int = 0
) -> List[str]:
    """
    Split text into sentences and group them by semantic meaning using embeddings.
    New chunks are started when the cosine similarity between adjacent sentences
    drops below the threshold or the max_words limit is reached.
    
    If overlap_sentences > 0, the next chunk will start with the last N sentences 
    of the previous chunk to maintain context.
    """
    if not text or not text.strip():
        return []
        
    # 1. Split into sentences (simple regex for now)
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return []
        
    if len(sentences) <= 1:
        return [text]

    # 2. Get embeddings for all sentences
    embeddings = await embed_fn(sentences)
    
    # 3. Group sentences
    chunks = []
    current_chunk_sentences = [sentences[0]]
    current_word_count = count_words(sentences[0])
    
    import numpy as np
    from scipy.spatial.distance import cosine

    for i in range(1, len(embeddings)):
        # Calculate similarity with previous sentence
        sim = 1 - cosine(embeddings[i-1], embeddings[i])
        
        sentence_words = count_words(sentences[i])
        
        # If similarity is low OR we exceed max words, start a new chunk
        if sim < threshold or (current_word_count + sentence_words > max_words):
            chunks.append(" ".join(current_chunk_sentences))
            
            # Start new chunk with overlap
            if overlap_sentences > 0 and len(current_chunk_sentences) >= overlap_sentences:
                overlap_pool = current_chunk_sentences[-overlap_sentences:]
                current_chunk_sentences = overlap_pool + [sentences[i]]
            else:
                current_chunk_sentences = [sentences[i]]
            
            current_word_count = sum(count_words(s) for s in current_chunk_sentences)
        else:
            current_chunk_sentences.append(sentences[i])
            current_word_count += sentence_words
            
    # Final flush
    if current_chunk_sentences:
        chunks.append(" ".join(current_chunk_sentences))
        
    return chunks


def split_text(
    text: str,
    max_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[str]:
    """
    Sentence-aware token splitter (original function, preserved for backward compat).
    """
    if not text or not text.strip():
        return []

    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return []

    chunks: list[str] = []
    current_sentences: list[str] = []
    current_tokens = 0

    for sentence in sentences:
        sent_tokens = count_tokens(sentence)
        if sent_tokens > max_tokens:
            if current_sentences:
                chunks.append(" ".join(current_sentences))
                current_sentences = []
                current_tokens = 0
            chunks.append(sentence)
            continue

        if current_tokens + sent_tokens > max_tokens and current_sentences:
            chunks.append(" ".join(current_sentences))
            overlap_sents: list[str] = []
            overlap_tok_count = 0
            for s in reversed(current_sentences):
                s_tok = count_tokens(s)
                if overlap_tok_count + s_tok > overlap_tokens:
                    break
                overlap_sents.insert(0, s)
                overlap_tok_count += s_tok
            current_sentences = overlap_sents
            current_tokens = overlap_tok_count

        current_sentences.append(sentence)
        current_tokens += sent_tokens

    if current_sentences:
        chunks.append(" ".join(current_sentences))
    return chunks


# ── Merge Helpers ──────────────────────────────────────────────────────────────

def merge_texts(left: str, right: str) -> str:
    """Merge two text pieces, detecting and removing any word-level overlap."""
    left = normalize_whitespace(left)
    right = normalize_whitespace(right)
    if not left:
        return right
    if not right:
        return left

    left_words = left.split()
    right_words = right.split()
    max_overlap = min(OVERLAP_WORDS, len(left_words), len(right_words))
    overlap_found = 0

    for k in range(max_overlap, 4, -1):
        if left_words[-k:] == right_words[:k]:
            overlap_found = k
            break

    if overlap_found:
        right = " ".join(right_words[overlap_found:]).strip()
    if not right:
        return left
    if SENTENCE_END_RE.search(left):
        return f"{left}\n\n{right}".strip()
    return f"{left} {right}".strip()


def dedupe_repeated_sentences(text: str) -> str:
    """Remove exact duplicate sentences within a single text block."""
    text = normalize_whitespace(text)
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    seen: Set[str] = set()
    kept: List[str] = []
    for part in parts:
        p = normalize_whitespace(part)
        if not p:
            continue
        key = normalize_for_compare(p)
        if key in seen:
            continue
        seen.add(key)
        kept.append(p)
    return normalize_whitespace(" ".join(kept))


def strip_repeated_heading_prefix(text: str, section_heading: Optional[str]) -> str:
    """Remove section heading if it appears as a prefix of the chunk text."""
    if not text or not section_heading:
        return text
    t = normalize_whitespace(text)
    h = normalize_whitespace(section_heading)
    if not t or not h:
        return text
    if t.lower().startswith(h.lower()):
        remainder = t[len(h):].strip(" \n:-")
        if remainder:
            return remainder
    return t


def remove_heading_lines_from_body(text: str, section_heading: Optional[str]) -> str:
    """Remove exact copies of the heading from within the body text."""
    if not text or not section_heading:
        return normalize_whitespace(text)
    heading_norm = normalize_for_compare(section_heading)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    kept: List[str] = []
    for line in lines:
        if normalize_for_compare(line) == heading_norm:
            continue
        kept.append(line)
    return normalize_whitespace("\n".join(kept))


def headings_compatible_for_merge(h1: Optional[str], h2: Optional[str]) -> bool:
    """Check if two section headings are compatible for merging adjacent chunks."""
    h1 = normalize_whitespace(h1 or "")
    h2 = normalize_whitespace(h2 or "")
    if not h1 or not h2:
        return True
    if h1 == h2:
        return True
    if GENERIC_CHAPTER_HEADING_RE.match(h1) or GENERIC_CHAPTER_HEADING_RE.match(h2):
        return True
    if normalize_for_compare(h1) == normalize_for_compare(h2):
        return True
    return False


# ── Span Overlap Detection ────────────────────────────────────────────────────

def get_span_ranges(spans: List[dict]) -> List[Tuple[int, int]]:
    """Extract byte offset ranges from Azure DI span metadata."""
    ranges: List[Tuple[int, int]] = []
    for s in spans or []:
        start = s.get("offset")
        length = s.get("length")
        if start is None or length is None:
            continue
        ranges.append((int(start), int(start) + int(length)))
    return ranges


def ranges_overlap(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


def collect_table_span_ranges(tables: List[dict]) -> List[Tuple[int, int]]:
    """Collect all byte-offset spans from all tables."""
    table_ranges: List[Tuple[int, int]] = []
    for table in tables:
        table_ranges.extend(get_span_ranges(table.get("spans") or []))
    return table_ranges


def paragraph_overlaps_table(
    paragraph_spans: List[dict],
    table_ranges: List[Tuple[int, int]],
) -> bool:
    """Check if a paragraph's content overlaps with any table's byte range."""
    para_ranges = get_span_ranges(paragraph_spans)
    if not para_ranges:
        return False
    for pr in para_ranges:
        for tr in table_ranges:
            if ranges_overlap(pr, tr):
                return True
    return False


# ── Figure Reference Extraction ────────────────────────────────────────────────

def extract_figure_reference(caption_text: Optional[str]) -> dict:
    """Parse figure number and aliases from caption text."""
    if not caption_text:
        return {
            "printed_figure_reference": None,
            "figure_reference_number": None,
            "figure_reference_aliases": [],
            "caption_without_reference": "",
        }
    text = normalize_whitespace(caption_text)
    match = FIGURE_REF_RE.search(text)
    if not match:
        return {
            "printed_figure_reference": None,
            "figure_reference_number": None,
            "figure_reference_aliases": [],
            "caption_without_reference": text,
        }
    printed_ref = match.group(0).strip()
    figure_number = match.group(1).strip()
    caption_wo_ref = text.replace(printed_ref, "", 1).strip(" .:-")
    aliases = list(dict.fromkeys([
        f"fig {figure_number}",
        f"fig. {figure_number}",
        f"figure {figure_number}",
        figure_number,
    ]))
    return {
        "printed_figure_reference": printed_ref,
        "figure_reference_number": figure_number,
        "figure_reference_aliases": aliases,
        "caption_without_reference": caption_wo_ref,
    }


# ── Structured Embedding Text ─────────────────────────────────────────────────

def make_embedding_text(
    chunk_type: str,
    section_heading: Optional[str],
    page_number: Optional[int],
    main_text: str,
    chapter_name: Optional[str] = None,
    topic_name: Optional[str] = None,
    subheading_name: Optional[str] = None,
    document_name: Optional[str] = None,
    clinical_description: Optional[str] = None,
    modality: Optional[str] = None,
    anatomy: Optional[List[str]] = None,
    visual_signs: Optional[List[str]] = None,
    candidate_conditions: Optional[List[str]] = None,
    extra_lines: Optional[List[str]] = None,
) -> str:
    """Build a structured text block optimized for vector embedding search."""
    lines: List[str] = []
    lines.append(f"Chunk type: {chunk_type}")
    if document_name:
        lines.append(f"Document: {document_name}")
    if chapter_name:
        lines.append(f"Chapter: {chapter_name}")
    if topic_name:
        lines.append(f"Topic: {topic_name}")
    if subheading_name:
        lines.append(f"Subheading: {subheading_name}")
    if section_heading:
        lines.append(f"Section Context: {section_heading}")
    if page_number is not None:
        lines.append(f"Page: {page_number}")
    
    if modality:
        lines.append(f"Modality: {modality}")
    if anatomy:
        lines.append(f"Anatomy: {', '.join(anatomy)}")
    if visual_signs:
        lines.append(f"Visual Signs: {', '.join(visual_signs)}")
    if candidate_conditions:
        lines.append(f"Conditions: {', '.join(candidate_conditions)}")
    
    if clinical_description:
        lines.append("Clinical Visual Description (Expert Analysis):")
        lines.append(clinical_description)
    
    lines.append("Content:")
    lines.append(main_text.strip())
    
    if extra_lines:
        for line in extra_lines:
            if line and line.strip():
                lines.append(line.strip())
    
    return "\n".join(lines).strip()
