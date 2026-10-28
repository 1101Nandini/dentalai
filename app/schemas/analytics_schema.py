from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from datetime import datetime

class TimeSeriesData(BaseModel):
    date: str
    messages: int
    users: int
    tokens: int
    revenue: float

class WidgetData(BaseModel):
    type: str  # "card" or "chart"
    id: str
    title: str
    value: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    bg: Optional[str] = None
    chart_type: Optional[str] = None  # "line", "bar", "pie", "doughnut"
    grid_width: Optional[int] = 12
    labels: Optional[List[str]] = None
    datasets: Optional[List[Dict[str, Any]]] = None

class DashboardSection(BaseModel):
    title: str
    type: str  # "grid" or "charts_grid"
    widgets: List[WidgetData]

class DashboardInsightResponse(BaseModel):
    # Existing Fields
    total_users: int
    active_pro_users: int
    total_chats: int
    total_messages: int
    total_documents: Optional[int] = None
    document_types: Optional[Dict[str, int]] = None
    stripe_status_distribution: Dict[str, int]
    total_revenue: float
    failed_transactions: int
    time_series: List[TimeSeriesData]
    avg_tokens_per_message: float
    avg_tokens_per_chat: float
    avg_tokens_per_user: float
    
    # New Fields - Super Admin (only present if org_id is not filtered)
    total_super_admins: int = 0
    total_organizations: int = 0
    
    # New Fields - Organizational Visibility
    active_users: int = 0
    role_counts: Dict[str, int] = {}
    total_soap_reports: int = 0

    # New Fields - Super Admin Premium/Subscription Insights
    active_trial_subscriptions: int = 0
    active_paid_subscriptions: int = 0
    active_subscriptions_by_plan: Dict[str, int] = {}
    total_users_had_trial: int = 0
    trial_to_paid_conversion_count: int = 0
    trial_to_paid_conversion_rate: float = 0.0
    estimated_mrr: float = 0.0
    canceled_stripe_subscriptions: int = 0
    pending_cancellation_stripe_subscriptions: int = 0
    past_due_stripe_subscriptions: int = 0
    average_messages_per_chat: float = 0.0
    average_chats_per_user: float = 0.0
    total_voice_streaming_sessions: int = 0
    
    # Org vs Individual breakdowns
    individual_users_count: int = 0
    organization_users_count: int = 0
    active_org_subscriptions: int = 0
    active_individual_subscriptions: int = 0
    active_org_trials: int = 0

    # Dynamic Dashboard Schema
    dynamic_sections: Optional[List[DashboardSection]] = None


