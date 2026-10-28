import re
from typing import Any, Dict, List, Optional, Tuple, Set


def normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def get_primary_page_number(bounding_regions: Optional[List[Dict[str, Any]]]) -> Optional[int]:
    if not bounding_regions:
        return None
    for region in bounding_regions:
        page_number = region.get("page_number")
        if page_number is not None:
            return int(page_number)
    return None


def get_primary_polygon(bounding_regions: Optional[List[Dict[str, Any]]]) -> List[float]:
    if not bounding_regions:
        return []
    for region in bounding_regions:
        polygon = region.get("polygon") or []
        if polygon:
            return polygon
    return []


def get_bbox(polygon: List[float]) -> Tuple[float, float, float, float]:
    """Convert an 8-float polygon to a (min_x, min_y, max_x, max_y) bounding box."""
    if not polygon or len(polygon) < 8:
        return (0.0, 0.0, 0.0, 0.0)
    xs = polygon[0::2]
    ys = polygon[1::2]
    return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))


def calculate_overlap_ratio(bbox_inner: Tuple[float, float, float, float], bbox_outer: Tuple[float, float, float, float]) -> float:
    """Calculate what percentage of bbox_inner is contained within bbox_outer."""
    x_left = max(bbox_inner[0], bbox_outer[0])
    y_top = max(bbox_inner[1], bbox_outer[1])
    x_right = min(bbox_inner[2], bbox_outer[2])
    y_bottom = min(bbox_inner[3], bbox_outer[3])

    if x_right < x_left or y_bottom < y_top:
        return 0.0

    intersection_area = (x_right - x_left) * (y_bottom - y_top)
    inner_area = (bbox_inner[2] - bbox_inner[0]) * (bbox_inner[3] - bbox_inner[1])
    
    if inner_area <= 0:
        return 0.0
        
    return intersection_area / inner_area


def polygon_top_left(polygon: List[float], y_snap: float = 0.2) -> Tuple[float, float]:
    """Return (snapped_y, min_x) for reading-order sorting.

    y_snap groups elements in the same horizontal band so that 2-column text
    at a similar vertical position sorts left-to-right instead of interleaving.
    """
    if not polygon or len(polygon) < 2:
        return (10**9, 10**9)

    xs = polygon[0::2]
    ys = polygon[1::2]
    snapped_y = round(min(ys) / y_snap) * y_snap
    return (snapped_y, min(xs))


def _is_implicit_heading(content: str) -> bool:
    """Treat short ALL-CAPS or Title-Case lines as section headings."""
    if not content or len(content) > 80:
        return False
    if content.endswith(".") and not content.isupper():
        return False
    return content.isupper() or content.istitle()


def find_section_heading_for_element(
    paragraphs: List[Dict[str, Any]],
    element_polygon: List[float],
    element_page: int,
) -> Dict[str, Optional[str]]:
    """Find the nearest section heading hierarchy spatially above the element on the same page."""
    # Patterns for hierarchical levels (matching chunk_pdf.py)
    H1_RE = re.compile(r"^(?:chapter\s+\d+|ch\s*\.\s*\d+|[1-9]\.)(?:\s|$)", re.IGNORECASE)
    H2_RE = re.compile(r"^[1-9]\.[1-9](?:\s|$)", re.IGNORECASE)
    H3_RE = re.compile(r"^[1-9]\.[1-9]\.[1-9](?:\s|$)", re.IGNORECASE)
    HEADING_ROLES = {"title", "sectionheading", "pageheader", "heading"}

    figure_y, _ = polygon_top_left(element_polygon)
    
    # We need to find the "best" (nearest above) for each level
    # or find the nearest subheading and then its parents.
    # Spatially, we just find the nearest heading and then look back 
    # to find its Chapter/Topic.
    
    best_heading_idx = -1
    best_y = -1.0

    for idx, para in enumerate(paragraphs):
        if para.get("page_number") != element_page:
            continue

        role = (para.get("role") or "").lower()
        content = normalize_text(para.get("content"))

        is_explicit_heading = role in HEADING_ROLES
        is_implicit = (not role) and _is_implicit_heading(content)

        if not (is_explicit_heading or is_implicit):
            continue

        para_polygon = get_primary_polygon(para.get("bounding_regions") or [])
        para_y, _ = polygon_top_left(para_polygon)

        if para_y <= figure_y and para_y > best_y:
            best_heading_idx = idx
            best_y = para_y

    result = {
        "chapter_name": "Document",
        "topic_name": "General",
        "subheading_name": "General",
        "section_heading": "Document"
    }

    if best_heading_idx != -1:
        # We found the nearest heading. Now walk backwards from it 
        # to find its hierarchy (Topic/Chapter).
        current_sub = normalize_text(paragraphs[best_heading_idx].get("content"))
        current_topic = None
        current_ch = None
        
        # If the nearest heading is itself an H1 or H2, adjust accordingly
        if H1_RE.match(current_sub):
            current_ch = current_sub
            current_sub = "General"
        elif H2_RE.match(current_sub):
            current_topic = current_sub
            current_sub = "General"

        # Walk back to fill gaps
        for i in range(best_heading_idx, -1, -1):
            p = paragraphs[i]
            r = (p.get("role") or "").lower()
            c = normalize_text(p.get("content"))
            if not (r in HEADING_ROLES or _is_implicit_heading(c)):
                continue
                
            if H1_RE.match(c) and not current_ch:
                current_ch = c
            elif H2_RE.match(c) and not current_topic:
                current_topic = c
        
        result["chapter_name"] = current_ch or "Document"
        result["topic_name"] = current_topic or current_ch or "General"
        result["subheading_name"] = current_sub or current_topic or "General"
        result["section_heading"] = current_sub or current_topic or current_ch or "Document"

    return result


def collect_nearby_paragraphs(
    paragraphs: List[Dict[str, Any]],
    element_polygon: List[float],
    element_page: int,
    max_before: int = 2,
    max_after: int = 2,
    figure_number: Optional[str] = None
) -> Dict[str, Any]:
    """
    Collect the closest paragraphs spatially above and below an element.
    If figure_number is provided (e.g. "4.2"), prioritize paragraphs 
    that explicitly mention "Fig. 4.2" or "Figure 4.2".
    """
    if not paragraphs:
        return {
            "before": [],
            "after": [],
            "combined_text": "",
        }

    figure_y, figure_x = polygon_top_left(element_polygon)

    above_scored: List[Tuple[float, Dict[str, Any]]] = []
    below_scored: List[Tuple[float, Dict[str, Any]]] = []

    for idx, para in enumerate(paragraphs):
        if para.get("page_number") != element_page:
            continue

        content = normalize_text(para.get("content"))
        if not content:
            continue
            
        role = para.get("role")
        if role in {"pageHeader", "pageFooter", "pageNumber"}:
            continue

        para_polygon = get_primary_polygon(para.get("bounding_regions") or [])
        if not para_polygon:
            continue
            
        para_y, para_x = polygon_top_left(para_polygon)
        
        # Calculate Manhattan-like distance with heavier weight on vertical distance
        distance = abs(para_y - figure_y) * 1000 + abs(para_x - figure_x)
        
        # Check for explicit figure reference (callout)
        is_callout = False
        if figure_number and len(figure_number) > 0:
            # Match "Fig. 4.2", "Figure 4.2", "(Fig 4.2)", "4.2"
            pattern = rf"(?:fig(?:ure)?\.?\s*{re.escape(figure_number)})"
            if re.search(pattern, content, re.IGNORECASE):
                is_callout = True
                # Give a Massive distance boost to callouts
                distance -= 5000 
        
        para_with_idx = para.copy()
        para_with_idx["_idx"] = idx  # preserve original order

        if para_y <= figure_y:
            above_scored.append((distance, para_with_idx))
        else:
            below_scored.append((distance, para_with_idx))

    above_scored.sort(key=lambda x: x[0])
    below_scored.sort(key=lambda x: x[0])

    before = [p for _, p in above_scored[:max_before]]
    after = [p for _, p in below_scored[:max_after]]

    # Re-sort by original reading order index
    before = sorted(before, key=lambda x: x["_idx"])
    after = sorted(after, key=lambda x: x["_idx"])

    combined_parts = [x["content"] for x in before] + [x["content"] for x in after]
    combined_text = "\n\n".join([x for x in combined_parts if x])

    return {
        "before": before,
        "after": after,
        "combined_text": combined_text,
    }


def infer_content_type_hint(caption_text: str, nearby_text: str) -> Optional[str]:
    """Keyword-based content type hint to bootstrap downstream classification."""
    combined = ((caption_text or "") + " " + (nearby_text or "")).lower()
    if any(k in combined for k in ["x-ray", "xray", "radiograph", "ct scan", "ct ", "mri", "ultrasound", "fluoroscopy", "pano", "cepha"]):
        return "radiology"
    if any(k in combined for k in ["figure", "graph", "chart", "plot", "histogram", "scatter", "table"]):
        return "chart"
    if any(k in combined for k in ["photo", "photograph", "specimen", "microscopy", "histology", "clinical"]):
        return "photo"
    return None
