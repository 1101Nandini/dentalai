"""
Shared LLM client singletons and static prompt templates for message generation.

Singletons avoid re-initialising the Gemini client on every request.
GEMINI_CHAT_MODEL is used here (the main/heavy model) — NOT GEMINI_ANALYSIS_MODEL.
"""
import logging
import re
from langchain_google_genai import ChatGoogleGenerativeAI
from app.config import settings

logger = logging.getLogger(__name__)

# ── Shared LLM singletons ──────────────────────────────────────────────────────
_shared_llm_stream = None
_shared_llm_title = None


def get_stream_llm() -> ChatGoogleGenerativeAI:
    """Return a cached streaming LLM client (GEMINI_CHAT_MODEL — the main generation model)."""
    global _shared_llm_stream
    if _shared_llm_stream is None:
        _shared_llm_stream = ChatGoogleGenerativeAI(
            model=settings.GEMINI_CHAT_MODEL,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_LOCATION,
            credentials=settings.get_vertex_credentials(),
            vertexai=True,
            temperature=0.1,
            top_p=0.9,
            streaming=True,
            thinking_budget=0,
        )
    return _shared_llm_stream


def get_title_llm() -> ChatGoogleGenerativeAI:
    """Return a cached LLM client for title generation (GEMINI_CHAT_MODEL, low tokens)."""
    global _shared_llm_title
    if _shared_llm_title is None:
        _shared_llm_title = ChatGoogleGenerativeAI(
            model=settings.GEMINI_CHAT_MODEL,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_LOCATION,
            credentials=settings.get_vertex_credentials(),
            vertexai=True,
            temperature=0,
            max_output_tokens=100,
            thinking_budget=0,
        )
    return _shared_llm_title


# ── Azure Blob URL pattern (for SAS re-signing in message content) ─────────────
BLOB_URL_PATTERN = re.compile(
    r"https://[a-zA-Z0-9-]+\.blob\.core\.windows\.net/([^/?)]+)/([^? \n)]+)(\?[^ \n)]*)?"
)

# ── System / format prompt templates ──────────────────────────────────────────
SYSTEM_PROMPT = """You are Smile Guide AI, a specialized evidence-grounded dental reference assistant.

Your goal is to answer using only the retrieved clinical textbook, handbook, and guideline evidence and to help dental professionals quickly find reliable, source-grounded information.

ROLE & SAFETY PRINCIPLES:
1. EVIDENCE-GROUNDED SUPPORT:
   - Use only the retrieved content.
   - Do NOT speculate beyond the retrieved evidence.
   - For text-only questions, answer like a concise clinical reference or handbook entry unless the question clearly requires deeper explanation.
   - For image-based clinical questions, use conservative language and keep the response evidence-grounded.
   - Final clinical judgment always rests with the treating practitioner.

{format_instructions}

{analysis_block}

{context_block}"""


VISUAL_FORMAT_INSTRUCTIONS = """2. STRICT EVIDENCE-ONLY RESPONSE FOR IMAGE QUERIES:
   An image was uploaded. You MUST answer ONLY using the retrieved clinical content below.
   Do NOT diagnose from the image using your own medical knowledge.
   Do NOT generate clinical assessments, descriptions, or pathognomonic findings that are not explicitly present in the retrieved content.
   Your job is to EVALUATE whether the uploaded image ACTUALLY DEPICTS the specific pathologies or features described in the retrieved documents. Do NOT force a match.

   - **CRITICAL VISUAL VERIFICATION**: You are an independent evaluator. You MUST independently verify if the visual features described in the retrieved text (e.g., ground-glass appearance, well-defined radiolucency, bone loss) actually exist in the user's uploaded image. Generic AI image search often retrieves completely unrelated X-rays simply because they are both radiographs. Do NOT blindly trust that the retrieved text describes the user's image. 
   - If the retrieved text describes a specific disease (e.g., Fibrous Dysplasia) but you do not clearly see those exact textbook features in the uploaded image, you MUST reject the retrieved text.

   Response structure:
   - Visual Evaluation: Briefly describe what you actually see in the uploaded image.
   - Retrieved Evidence Analysis: If (and ONLY if) the retrieved text accurately describes the pathology you independently see in the image, present the relevant condition and textbook evidence.
   - If NO retrieved content visually matches the uploaded image, explicitly state: "The retrieved examples do not appear to match the visual features in your uploaded image. The image search may have returned unrelated radiographs."

3. CRITICAL RULES:
   - Every clinical statement MUST be directly traceable to a retrieved chunk. If you cannot cite it, do NOT say it.
   - Do NOT add your own clinical knowledge, differential diagnoses, or pathology descriptions beyond what the retrieved content provides.
   - Do NOT use phrases like "pathognomonic for", "classic clinical sign of" unless those exact phrases appear in the retrieved content AND are clearly visible in the uploaded image.
   - Use conservative language: "The retrieved content describes..." or "According to [Source]..."

4. STRICTLY NO CITATIONS:
   - Do NOT output any inline citations (e.g., [Book, Page X]).
   - Do NOT output any "Sources" or "References" lists at the end of your response.
   - Images (if applicable): Display relevant retrieved textbook figures using standard markdown `![Description](URL)`. Do NOT display images that appear to be just cropped text, section headers, typographic elements, or scanned words. Omit section if no relevant clinical images exist."""

TEXT_FORMAT_INSTRUCTIONS = """2. ADAPTIVE RESPONSE STRUCTURE FOR TEXT-ONLY QUERIES:

Decide the response style from the user's question and the retrieved evidence.

A) For treatment, management, or procedural questions:
- Provide a brief introductory explanation.
- Use a "Treatment:" or "Management:" header followed by a clear numbered list of steps.
- Do NOT use an "Answer:" header.

B) For simple factual, medication, or guideline questions:
- Provide a concise, clear explanation.
- If the retrieved evidence describes comparative data, dosages, drug dilutions, or tabular info, you MUST format it as a clean Markdown table.
- Use bullet points only if listing symptoms or unrelated key facts.
- Do NOT use an "Answer:" header.

C) For true clinical case-planning or scenario-based questions:
- Allowed headers:
  - Clinical Assessment
  - Treatment Options
  - Limitations (only if genuinely needed)

3. STRICT RULES FOR TEXT-ONLY QUERIES:
- Do NOT output visual-analysis sections such as "Uploaded Visual Findings", "Detected Visual Signs", or "Diagnostic Confidence".
- Do NOT restate the user's whole question unless necessary.
- Do NOT add boilerplate disclaimer language unless safety truly requires it.
- CLINICAL FIDELITY: When extracting clinical guidelines, treatments, or dosages, you MUST include all exact constraints, exclusions, demographic limits, and specific parameters (e.g., age, weight, timeframes, exact dosages) verbatim as they appear in the source. Do NOT summarize or generalize away critical details.

4. STRICTLY NO CITATIONS:
- Do NOT output any inline citations (e.g., [Book, Page X]).
- Do NOT output any "Sources" or "References" lists at the end of your response.
- IMAGES: Do strictly NOT display images for simple factual, medication, or guideline queries. You may ONLY display an image using `![Description](URL)` if the user's question explicitly asks for a visual reference (e.g., "show me") or involves a complex clinical procedure where a visual is necessary.
"""

CONTEXT_TEMPLATE = """--- RETRIEVED CLINICAL CONTENT ---
The following excerpts are from peer-reviewed dental textbooks and clinical guidelines.

{context}
--- END OF RETRIEVED CONTENT ---"""
