from app.dependencies import get_org_admin_or_super, require_permission
from fastapi import APIRouter, Depends, Query, HTTPException
from datetime import datetime
from typing import Optional

from app.core.response import api_response
from app.services.analytics_service import AnalyticsService
from app.core.constants import UserRole


router = APIRouter()

@router.get("/dashboard")
async def get_dashboard_insights(
    start_date: str = Query(..., description="Start date (YYYY-MM-DD or ISO format)"),
    end_date: str = Query(..., description="End date (YYYY-MM-DD or ISO format)"),
    organization_id: Optional[str] = Query(None, description="Filter by org (Super Admin only)"),
    current_user=Depends(require_permission("analytics:dashboard"))
):
    import time
    import logging
    logger = logging.getLogger(__name__)
    t_route_start = time.perf_counter()
    """
    Retrieve dashboard insights for a specific date range.
    Requires Super Admin or Org Admin privileges.
    - Org Admins will ALWAYS only see their own organization's data.
    - Super Admins can see global data, or filter by organization_id.
    """
    try:
        # Enforce Org Admin scoping strictly
        role = current_user.get("role")
        is_org_admin = (role == UserRole.ORG_ADMIN)
        if is_org_admin:
            organization_id = current_user.get("organization_id")
            
        # Basic parsing of date strings
        try:
            parsed_start = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except ValueError:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d")

        try:
            parsed_end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d")
        
        # Ensure dates are timezone-aware (UTC)
        if parsed_start.tzinfo is None:
            from datetime import timezone
            parsed_start = parsed_start.replace(tzinfo=timezone.utc)
        if parsed_end.tzinfo is None:
            from datetime import timezone
            parsed_end = parsed_end.replace(tzinfo=timezone.utc)

        t_service_start = time.perf_counter()
        data = await AnalyticsService.get_dashboard_insights(
            parsed_start, parsed_end, org_id=organization_id, is_org_admin=is_org_admin
        )
        
        logger.info(f"[ANALYTICS-ROUTE] Service call took: {time.perf_counter() - t_service_start:.4f}s")
        t_json_start = time.perf_counter()

        response = api_response(
            success=True,
            message="Dashboard insights retrieved successfully.",
            data=data
        )
        
        logger.info(f"[ANALYTICS-ROUTE] JSON response formatting took: {time.perf_counter() - t_json_start:.4f}s")
        logger.info(f"[ANALYTICS-ROUTE] Total route execution took: {time.perf_counter() - t_route_start:.4f}s")
        
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing date range: {str(e)}")
