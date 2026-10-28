import os
import sys
import asyncio
import json
import logging
import re
from typing import List, Dict, Any
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage

# Ensure app is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.services.rag_service import RagService
from app.config import settings

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

TOP_K = 5

EVALUATION_DATASET = [
    {
        "query": "My patient just had a knee replacement 3 months ago. Do I need to prescribe pre-med before their cleaning appointment?",
        "gold_pages": [83],
        "gold_answer": "In general, for patients with prosthetic joint implants, prophylactic antibiotics are not recommended prior to dental procedures to prevent prosthetic joint infections."
    },
    {
        "query": "My patient is coming in for a cleaning. She doesn’t need prophylactic after joint replacement but her physician requests she does take prophylactic meds. What do I do?",
        "gold_pages": [79, 84],
        "gold_answer": "If the patient’s physician requests prophylaxis for a dental procedure but the patient does not meet criteria for needing it, the physician should prescribe the prophylaxis. The patient should take it under the physician’s direction and then proceed with the dental procedure."
    },
    {
        "query": "When does a patient need prophylactic antibiotics before an appointment?",
        "gold_pages": [79],
        "gold_answer": "A patient needs prophylactic antibiotics for dental procedures that bleed or could bleed if they have a prosthetic cardiac valve replacement, previous infective endocarditis, or congenital heart disease."
    },
    {
        "query": "For a patient that needs prophylactic antibiotics before a dental procedure, what is the standard med and dosage?",
        "gold_pages": [80, 86],
        "gold_answer": "The standard regimen is Amoxicillin 500 mg, dispense 4 tablets, with instructions to take 4 tablets (2 g) 30 to 60 minutes before the procedure."
    },
    {
        "query": "My patient has very severe dental anxiety. What is the best prescription and how to prescribe it?",
        "gold_pages": [16],
        "gold_answer": "If there are no allergies or contraindications, the most commonly used anxiety control medication is Valium 5 mg. Dispense 4 tablets with instructions to take 1 tablet in the evening before going to bed and 1 tablet 1 hour before the appointment. The patient should not drive themselves to or from the appointment, it should not be prescribed to pregnant women, and its half-life is 44 to 100 hours."
    },
    {
        "query": "What are signs and symptoms of an oral infection?",
        "gold_pages": [30],
        "gold_answer": "Signs and symptoms of an oral infection include swelling, often pain, and radiolucency associated with teeth or gums."
    },
    {
        "query": "My patient is allergic to penicillin, and hasn’t responded to clindamycin. What else can I prescribe?",
        "gold_pages": [31],
        "gold_answer": "Azithromycin is the next antibiotic of choice. If there is no response to this protocol, refer to or consult with an oral-maxillofacial surgeon, endodontist, periodontist, or infectious disease physician."
    },
    {
        "query": "I think my patient may have a fungal infection. There are no allergies that I am aware of. What is the medication to help?",
        "gold_pages": [37],
        "gold_answer": "For local treatment of oral fungal infection use Clotrimazole Troche or Nystatin Tablets. For systemic treatment use Diflucan."
    },
    {
        "query": "My patient has lesions in the mouth. How do I tell a difference between aphthous and herpes simplex?",
        "gold_pages": [46],
        "gold_answer": "Aphthous lesions occur exclusively on unattached mucosa such as the cheek or floor of the mouth, whereas intraoral secondary herpes simplex occurs only on attached mucosa."
    },
    {
        "query": "What is the best formulation of Magic Mouthwash?",
        "gold_pages": [53],
        "gold_answer": "There is no single specific formulation for Magic Mouthwash. Most formulations contain viscous lidocaine or diphenhydramine for analgesia and an antacid such as Maalox, Milk of Magnesia, or sucralfate to coat the mucosa. They may also include tetracycline, nystatin, or corticosteroids such as hydrocortisone or dexamethasone."
    },
    {
        "query": "What toothpaste is best to help with sensitivity?",
        "gold_pages": [56],
        "gold_answer": "Potassium nitrate is the active ingredient in most FDA-approved desensitizing toothpastes except Crest ProHealth, which contains stannous fluoride."
    },
    {
        "query": "I think my patient may have a dry socket after extraction. What is the best treatment?",
        "gold_pages": [73],
        "gold_answer": "Treatment of dry socket includes gently irrigating the socket with saline, filling the socket with iodoform gauze coated with local anesthetic and eugenol, removing the gauze after two days, and repeating the application if pain persists."
    },
    {
        "query": "What is the maximum dosage for a child for amoxicillin?",
        "gold_pages": [75],
        "gold_answer": "For infants older than 3 months and children under 40 kg, amoxicillin dosing is 20 to 100 mg/kg/day in divided doses every 8 hours."
    },
]

EVAL_SYSTEM_PROMPT = """You are an expert RAG accuracy judge.
You will evaluate the AI's generated answer against a known Ground Truth answer, using the Retrieved Context.

Score the following dimensions:

1. FAITHFULNESS (0-10): Is the generated answer entirely derived from the retrieved context?
   - 0 = Complete hallucination.
   - 10 = Every claim is directly backed by the retrieved context.

2. ANSWER RELEVANCE (0-10): Does the generated answer directly address the user's question and match the Ground Truth?
   - 0 = Dodged the question entirely or directly contradicts the ground truth.
   - 10 = Direct, helpful, and matches the core facts of the ground truth.

3. CITATION_ACCURACY (0-10): Does the generated answer cite or rely on the correct page/section from the retrieved context?
   - 0 = Citation/section is clearly wrong.
   - 10 = Citation/section aligns well with the best supporting retrieved page(s).

Return ONLY a raw JSON object. Do NOT include markdown code blocks or any other explanation.
{
    "faithfulness": 9,
    "answer_relevance": 8,
    "citation_accuracy": 8,
    "reasoning": "Brief explanation..."
}
"""

def calculate_ir_metrics(retrieved_docs: List[Dict], gold_pages: List[int]) -> Dict[str, float]:
    if not gold_pages:
        return {
            "precision_at_k": 0.0,
            "recall_at_k": 0.0,
            "mrr": 0.0,
            "hit_rate": 0.0,
            "page_accuracy_at_1": 0.0,
        }

    gold_set = {int(p) for p in gold_pages if p is not None}

    relevant_ranks: List[int] = []
    matched_pages = set()

    for rank, doc in enumerate(retrieved_docs, start=1):
        page = doc.get("page")
        if page is None:
            continue
        try:
            page = int(page)
        except Exception:
            continue

        if page in gold_set:
            relevant_ranks.append(rank)
            matched_pages.add(page)

    k = len(retrieved_docs)
    hit_rate = 1.0 if relevant_ranks else 0.0
    precision_at_k = (len(relevant_ranks) / k) if k > 0 else 0.0
    recall_at_k = (len(matched_pages) / len(gold_set)) if gold_set else 0.0
    mrr = (1.0 / relevant_ranks[0]) if relevant_ranks else 0.0

    top1_page = retrieved_docs[0].get("page") if retrieved_docs else None
    try:
        top1_page = int(top1_page) if top1_page is not None else None
    except Exception:
        top1_page = None

    page_accuracy_at_1 = 1.0 if top1_page in gold_set else 0.0

    return {
        "precision_at_k": precision_at_k,
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "hit_rate": hit_rate,
        "page_accuracy_at_1": page_accuracy_at_1,
    }


def extract_cited_pages(answer: str) -> List[int]:
    if not answer:
        return []

    pages = set()

    for start, end in re.findall(r'pages?\s+(\d+)\s*[-–]\s*(\d+)', answer, flags=re.IGNORECASE):
        try:
            s, e = int(start), int(end)
            if s <= e:
                for p in range(s, e + 1):
                    pages.add(p)
        except Exception:
            pass

    for p in re.findall(r'page\s+(\d+)', answer, flags=re.IGNORECASE):
        try:
            pages.add(int(p))
        except Exception:
            pass

    return sorted(pages)


def calculate_citation_match(answer: str, gold_pages: List[int]) -> float:
    cited_pages = set(extract_cited_pages(answer))
    gold_set = {int(p) for p in gold_pages if p is not None}
    if not cited_pages:
        return 0.0
    return 1.0 if cited_pages.intersection(gold_set) else 0.0


async def generate_dry_run_answer(query: str, context: str) -> str:
    system_prompt = f"""You are Petal AI, a highly exact, evidence-based clinical dental AI assistant.

CRITICAL RULES:
1. STRICT GROUNDING: You are FORBIDDEN from using your pre-trained knowledge to answer clinical questions. Use ONLY the facts provided in the "Retrieved Context" below.
2. FORCE EXTRACTION: You must actively synthesize and extract answers from the retrieved context. If the answer is present in any form within the text or figures, you MUST provide it. Only say "I'm sorry, I do not have enough information" if the topic is completely absent from the context.
3. RESPONSE STYLE: Answer concisely for straightforward handbook questions. Do not over-explain.
4. SOURCE LINE: End with one source line in this format:
   Source: <document name>, page <page number>

--- RETRIEVED CONTEXT ---
{context}
"""
    llm = ChatGoogleGenerativeAI(
        model=settings.GEMINI_CHAT_MODEL,
        project=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_LOCATION,
        credentials=settings.get_vertex_credentials(),
        vertexai=True,
        temperature=0.1,
    )

    resp = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=query),
    ])
    return resp.content


async def evaluate_llm(query: str, context: str, answer: str, gold_answer: str) -> dict:
    judge_llm = ChatGoogleGenerativeAI(
        model=settings.GEMINI_CHAT_MODEL,
        project=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_LOCATION,
        credentials=settings.get_vertex_credentials(),
        vertexai=True,
        temperature=0.0,
    )

    user_eval_prompt = f"""
QUESTION:
{query}

GROUND TRUTH ANSWER:
{gold_answer}

RETRIEVED CONTEXT:
{context}

GENERATED ANSWER:
{answer}
"""

    resp = await judge_llm.ainvoke([
        SystemMessage(content=EVAL_SYSTEM_PROMPT),
        HumanMessage(content=user_eval_prompt),
    ])

    content = resp.content.strip()
    if content.startswith("```json"):
        content = content.replace("```json", "", 1)
    if content.startswith("```"):
        content = content.replace("```", "", 1)
    if content.endswith("```"):
        content = content[::-1].replace("```", "", 1)[::-1]
    content = content.strip()

    try:
        return json.loads(content)
    except Exception as exc:
        logger.error(f"LLM Evaluation JSON Parse Error: {exc}\nRaw Content: {resp.content}")
        return {
            "faithfulness": 0,
            "answer_relevance": 0,
            "citation_accuracy": 0,
            "reasoning": f"JSON parse error: {exc}"
        }


async def run_evaluation():
    print("🚀 Starting RAG Evaluation pipeline for Dental Drug Booklet...\n")

    metrics = {
        "precision_at_k": 0.0,
        "recall_at_k": 0.0,
        "mrr": 0.0,
        "hit_rate": 0.0,
        "page_accuracy_at_1": 0.0,
        "citation_match": 0.0,
        "faithfulness": 0.0,
        "answer_relevance": 0.0,
        "judge_citation_accuracy": 0.0,
    }

    n = len(EVALUATION_DATASET)

    for i, data in enumerate(EVALUATION_DATASET, 1):
        q = data["query"]
        gold_pages = data["gold_pages"]
        gold_answer = data["gold_answer"]

        print("==================================================")
        print(f"Test {i}/{n}")
        print(f"Q: {q}")
        print(f"Expected Pages: {gold_pages}")
        print("Retrieving context...")

        retrieved_sources, analysis = await RagService.retrieve(
            query=q,
            top_text=8,
            top_image=4,
        )
        top_k_sources = retrieved_sources[:12]

        print(f"Retrieved {len(top_k_sources)} chunks from Azure AI Search:")
        for src in top_k_sources:
            score = src.get('_final_score', src.get('reranker_score', src.get('score', 0)))
            try:
                score = float(score or 0)
            except Exception:
                score = 0.0
            print(
                f"  - [{src.get('chunk_type')}] "
                f"{src.get('document_name', 'Unknown')} "
                f"(page {src.get('page', '?')}, score: {score:.2f})"
            )

        ir_scores = calculate_ir_metrics(top_k_sources, gold_pages)

        print("\n📊 INFORMATION RETRIEVAL (PAGE-LEVEL) METRICS:")
        print(f"  Hit Rate:            {ir_scores['hit_rate']*100:.1f}%")
        print(f"  Precision@{TOP_K}:      {ir_scores['precision_at_k']*100:.1f}%")
        print(f"  Recall@{TOP_K}:         {ir_scores['recall_at_k']*100:.1f}%")
        print(f"  MRR:                 {ir_scores['mrr']:.3f}")
        print(f"  Page Accuracy@1:     {ir_scores['page_accuracy_at_1']*100:.1f}%")

        context_parts = []
        for idx, src in enumerate(top_k_sources, 1):
            doc_name = src.get("document_name", "Unknown")
            page = src.get("page")
            page_info = f" (page {page})" if page else ""

            if str(src.get("chunk_type")).lower() in ["figure", "image", "xray"]:
                chunk_text = (
                    f"[FIGURE]\n"
                    f"Caption: {src.get('caption', '')}\n"
                    f"Clinical Description: {src.get('clinical_description', '')}\n"
                    f"Content: {src.get('content', '')}"
                )
            else:
                chunk_text = f"[TEXT]\nContent: {src.get('content', '')}"

            context_parts.append(f"[{idx}] {doc_name}{page_info}:\n{chunk_text}")

        context_text = "\n\n".join(context_parts)

        print("\nGenerating answer...")
        answer = await generate_dry_run_answer(q, context_text)

        citation_match = calculate_citation_match(answer, gold_pages)
        print(f"Citation Match (exact page overlap): {'YES' if citation_match else 'NO'}")

        print("Scoring Faithfulness, Relevance, Citation Accuracy (LLM Judge)...")
        llm_scores = await evaluate_llm(q, context_text, answer, gold_answer)

        print("\n🧠 LLM EVAL METRICS:")
        print(f"  Faithfulness:       {llm_scores.get('faithfulness')}/10")
        print(f"  Answer Relevance:   {llm_scores.get('answer_relevance')}/10")
        print(f"  Citation Accuracy:  {llm_scores.get('citation_accuracy')}/10")
        print(f"  💡 Reason: {llm_scores.get('reasoning')}")
        print("==================================================\n")

        metrics["precision_at_k"] += ir_scores["precision_at_k"]
        metrics["recall_at_k"] += ir_scores["recall_at_k"]
        metrics["mrr"] += ir_scores["mrr"]
        metrics["hit_rate"] += ir_scores["hit_rate"]
        metrics["page_accuracy_at_1"] += ir_scores["page_accuracy_at_1"]
        metrics["citation_match"] += citation_match
        metrics["faithfulness"] += llm_scores.get("faithfulness", 0)
        metrics["answer_relevance"] += llm_scores.get("answer_relevance", 0)
        metrics["judge_citation_accuracy"] += llm_scores.get("citation_accuracy", 0)

    print("🏆 FINAL RAG EXAM AVERAGE SCORES")
    print(f"(N = {n})")
    print("--- Retrieval ---")
    print(f"Average Hit Rate:           {(metrics['hit_rate'] / n)*100:.1f}%")
    print(f"Average Precision@{TOP_K}:     {(metrics['precision_at_k'] / n)*100:.1f}%")
    print(f"Average Recall@{TOP_K}:        {(metrics['recall_at_k'] / n)*100:.1f}%")
    print(f"Average MRR:                {(metrics['mrr'] / n):.3f}")
    print(f"Average Page Accuracy@1:    {(metrics['page_accuracy_at_1'] / n)*100:.1f}%")
    print(f"Average Citation Match:     {(metrics['citation_match'] / n)*100:.1f}%")
    print("--- Generation ---")
    print(f"Average Faithfulness:       {metrics['faithfulness'] / n:.1f}/10")
    print(f"Average Answer Relevance:   {metrics['answer_relevance'] / n:.1f}/10")
    print(f"Average Citation Accuracy:  {metrics['judge_citation_accuracy'] / n:.1f}/10")


if __name__ == "__main__":
    asyncio.run(run_evaluation())
