from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from bson import ObjectId
from app.core.database import get_database
from app.schemas.analytics_schema import DashboardInsightResponse, TimeSeriesData

class AnalyticsService:

    @staticmethod
    async def get_dashboard_insights(
        start_date: datetime,
        end_date: datetime,
        org_id: Optional[str] = None,
        is_org_admin: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Aggregate dashboard insights: total counts, file types, and time-series growth.
        Filters by org_id if provided.
        """
        import time
        import logging
        import asyncio
        from app.core.constants import UserRole

        if is_org_admin is None:
            is_org_admin = bool(org_id)

        logger = logging.getLogger(__name__)
        t_start = time.perf_counter()

        db = await get_database()

        start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)

        user_match = {}
        if org_id:
            user_match["organization_id"] = org_id

        valid_user_ids = []
        if org_id:
            cursor = db["users"].find(user_match, {"_id": 1})
            valid_user_ids = [str(doc["_id"]) async for doc in cursor]

        chat_match = {}
        if org_id:
            chat_match["user_id"] = {"$in": valid_user_ids}

        valid_chat_ids = []
        if org_id:
            cursor = db["chats"].find(chat_match, {"_id": 1})
            valid_chat_ids = [str(doc["_id"]) async for doc in cursor]

        msg_match = {}
        if org_id:
            msg_match["chat_id"] = {"$in": valid_chat_ids}

        total_users_match = dict(user_match)
        if not org_id:
            total_users_match["role"] = {"$ne": UserRole.SUPER_ADMIN}

        active_user_match = dict(user_match)
        active_user_match["is_active"] = True

        role_pipeline = [
            {"$match": active_user_match},
            {"$group": {"_id": "$role", "count": {"$sum": 1}}}
        ]

        session_match = {}
        if org_id:
            session_match["user_id"] = {"$in": valid_user_ids}
        soap_match = dict(session_match)
        soap_match["soap_note"] = {"$exists": True, "$ne": None}

        sub_match = {"is_active": True, "plan": "pro"}
        if org_id:
            sub_match["user_id"] = {"$in": valid_user_ids}

        stripe_match = {}
        if org_id:
            stripe_match["user_id"] = {"$in": valid_user_ids}
        stripe_status_pipeline = [
            {"$match": stripe_match},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ]

        transaction_match = {"status": "completed", "created_at": {"$gte": start_date, "$lte": end_date}, "type": "invoice.paid"}
        if org_id:
            transaction_match["user_id"] = {"$in": valid_user_ids}
        revenue_pipeline = [
            {"$match": transaction_match},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]

        failed_match = {"status": "failed"}
        if org_id:
            failed_match["user_id"] = {"$in": valid_user_ids}

        pipeline_file_types = [
            {"$match": {"is_active": True}},
            {"$group": {"_id": "$file_type", "count": {"$sum": 1}}}
        ]

        usage_match = {"type": "message"}
        if org_id:
            usage_match["organization_id"] = str(org_id)
        pipeline_global_tokens = [
            {"$match": usage_match},
            {
                "$group": {
                    "_id": None,
                    "total_tokens": {"$sum": "$token_usage.total_tokens"},
                    "msg_count": {"$sum": 1},
                    "chat_ids": {"$addToSet": "$chat_id"},
                    "users": {"$addToSet": "$user_id"}
                }
            }
        ]

        msg_time_match = {"created_at": {"$gte": start_date, "$lte": end_date}}
        if org_id:
            msg_time_match["chat_id"] = {"$in": valid_chat_ids}
        pipeline_messages = [
            {"$match": msg_time_match},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}}
        ]

        token_time_match = {"type": "message", "created_at": {"$gte": start_date, "$lte": end_date}}
        if org_id:
            token_time_match["organization_id"] = str(org_id)
        pipeline_tokens = [
            {"$match": token_time_match},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "tokens": {"$sum": "$token_usage.total_tokens"}}}
        ]

        user_time_match = {"created_at": {"$gte": start_date, "$lte": end_date}}
        if org_id:
            user_time_match["organization_id"] = org_id
        pipeline_users = [
            {"$match": user_time_match},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "count": {"$sum": 1}}},
            {"$sort": {"_id": 1}}
        ]

        rev_time_match = {"status": "completed", "created_at": {"$gte": start_date, "$lte": end_date}}
        if org_id:
            rev_time_match["user_id"] = {"$in": valid_user_ids}
        pipeline_revenue = [
            {"$match": rev_time_match},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}}, "total": {"$sum": "$amount"}}},
            {"$sort": {"_id": 1}}
        ]

        async def fetch_agg(col, pipe):
            return await db[col].aggregate(pipe).to_list(None)

        async def fetch_docs(org_id_val):
            if org_id_val: return None
            return await db["documents"].count_documents({"is_active": True})

        async def fetch_super_admins(org_id_val):
            if org_id_val: return 0
            return await db["users"].count_documents({"role": UserRole.SUPER_ADMIN})

        async def fetch_orgs(org_id_val):
            if org_id_val: return 0
            return await db["organizations"].count_documents({"is_active": True})

        async def fetch_file_types(org_id_val):
            if org_id_val: return []
            return await fetch_agg("documents", pipeline_file_types)

        # Super Admin Premium/Subscription Insights setups
        trial_sub_match = {"is_active": True, "$or": [{"is_trial": True}, {"plan": "free_trial"}]}
        paid_sub_match = {"is_active": True, "is_trial": {"$ne": True}, "plan": {"$ne": "free_trial"}}
        canceled_stripe_match = {"status": "canceled"}
        pending_stripe_match = {"status": "pending_cancellation"}
        past_due_stripe_match = {"status": "past_due"}
        voice_session_match = {}

        if org_id:
            trial_sub_match["user_id"] = {"$in": valid_user_ids}
            paid_sub_match["user_id"] = {"$in": valid_user_ids}
            canceled_stripe_match["user_id"] = {"$in": valid_user_ids}
            pending_stripe_match["user_id"] = {"$in": valid_user_ids}
            past_due_stripe_match["user_id"] = {"$in": valid_user_ids}
            voice_session_match["user_id"] = {"$in": valid_user_ids}

        active_plan_match = {"is_active": True}
        if org_id:
            active_plan_match["user_id"] = {"$in": valid_user_ids}
        pipeline_active_plans = [
            {"$match": active_plan_match},
            {"$group": {"_id": "$plan", "count": {"$sum": 1}}}
        ]

        async def fetch_conversion_stats(org_id_val, valid_user_ids_val):
            had_trial_user_match = {"has_had_trial": True}
            if org_id_val:
                had_trial_user_match["organization_id"] = org_id_val
            
            trial_users_cursor = db["users"].find(had_trial_user_match, {"_id": 1})
            trial_user_ids_list = [str(doc["_id"]) async for doc in trial_users_cursor]
            
            total_had_trial = len(trial_user_ids_list)
            converted_count = 0
            if trial_user_ids_list:
                converted_distinct = await db["subscriptions"].distinct("user_id", {
                    "user_id": {"$in": trial_user_ids_list},
                    "plan": {"$ne": "free_trial"},
                    "is_trial": {"$ne": True}
                })
                converted_count = len(converted_distinct)
                
            conversion_rate = (converted_count / total_had_trial * 100.0) if total_had_trial > 0 else 0.0
            return total_had_trial, converted_count, conversion_rate

        async def fetch_ind_users(org_id_val):
            if org_id_val: return 0
            return await db["users"].count_documents({"$or": [{"organization_id": {"$exists": False}}, {"organization_id": None}, {"organization_id": ""}]})

        async def fetch_org_users(org_id_val):
            if org_id_val:
                return await db["users"].count_documents({"organization_id": str(org_id_val)})
            return await db["users"].count_documents({"organization_id": {"$exists": True, "$ne": None, "$nin": [""]}})

        async def fetch_active_org_subs(org_id_val):
            if org_id_val:
                return await db["subscriptions"].count_documents({"organization_id": str(org_id_val), "is_active": True})
            return await db["subscriptions"].count_documents({"organization_id": {"$exists": True, "$ne": None, "$nin": [""]}, "is_active": True})

        async def fetch_active_ind_subs(org_id_val):
            if org_id_val: return 0
            return await db["subscriptions"].count_documents({"user_id": {"$exists": True, "$ne": None, "$nin": [""]}, "is_active": True})

        async def fetch_active_org_trials(org_id_val):
            if org_id_val:
                return await db["subscriptions"].count_documents({"organization_id": str(org_id_val), "is_active": True, "$or": [{"is_trial": True}, {"plan": "free_trial"}]})
            return await db["subscriptions"].count_documents({"organization_id": {"$exists": True, "$ne": None, "$nin": [""]}, "is_active": True, "$or": [{"is_trial": True}, {"plan": "free_trial"}]})

        async def fetch_total_orgs(org_id_val):
            if org_id_val: return 0
            return await db["organizations"].count_documents({})

        t_parallel = time.perf_counter()
        
        # Parallel execution of all 33 queries
        results = await asyncio.gather(
            db["users"].count_documents(total_users_match),            # 0
            db["chats"].count_documents(chat_match),                   # 1
            db["messages"].count_documents(msg_match),                 # 2
            fetch_docs(org_id),                                        # 3
            fetch_super_admins(org_id),                                # 4
            fetch_orgs(org_id),                                        # 5
            db["users"].count_documents(active_user_match),            # 6
            fetch_agg("users", role_pipeline),                         # 7
            db["dental_voice_sessions"].count_documents(soap_match),   # 8
            db["subscriptions"].count_documents(sub_match),            # 9
            fetch_agg("stripe_subscriptions", stripe_status_pipeline), # 10
            fetch_agg("transaction_logs", revenue_pipeline),           # 11
            db["transaction_logs"].count_documents(failed_match),      # 12
            fetch_file_types(org_id),                                  # 13
            fetch_agg("usage_logs", pipeline_global_tokens),           # 14
            fetch_agg("messages", pipeline_messages),                  # 15
            fetch_agg("usage_logs", pipeline_tokens),                  # 16
            fetch_agg("users", pipeline_users),                        # 17
            fetch_agg("transaction_logs", pipeline_revenue),           # 18
            db["subscriptions"].count_documents(trial_sub_match),      # 19
            db["subscriptions"].count_documents(paid_sub_match),       # 20
            fetch_agg("subscriptions", pipeline_active_plans),         # 21
            db["stripe_subscriptions"].count_documents(canceled_stripe_match),       # 22
            db["stripe_subscriptions"].count_documents(pending_stripe_match),        # 23
            db["stripe_subscriptions"].count_documents(past_due_stripe_match),        # 24
            db["dental_voice_sessions"].count_documents(voice_session_match),        # 25
            fetch_conversion_stats(org_id, valid_user_ids),            # 26
            fetch_ind_users(org_id),                                   # 27
            fetch_org_users(org_id),                                   # 28
            fetch_active_org_subs(org_id),                             # 29
            fetch_active_ind_subs(org_id),                             # 30
            fetch_active_org_trials(org_id),                           # 31
            fetch_total_orgs(org_id)                                   # 32
        )

        logger.info(f"[ANALYTICS-PERF] Parallel execution took: {time.perf_counter() - t_parallel:.4f}s")

        total_users = results[0]
        total_chats = results[1]
        total_messages = results[2] // 2
        total_documents = results[3]
        total_super_admins = results[4]
        total_organizations = results[5]
        active_users = results[6]
        
        role_counts = { "super_admin": 0, "org_admin": 0, "dentist": 0, "staff": 0, "front_office": 0, "student": 0, "solo_dentist": 0 }
        for item in results[7]: role_counts[item["_id"] or "unknown"] = item["count"]

        total_soap_reports = results[8]
        active_pro_users = results[9]
        
        stripe_status_distribution = {}
        for item in results[10]: stripe_status_distribution[item["_id"] or "unknown"] = item["count"]

        revenue_data = results[11]
        total_revenue = revenue_data[0].get("total", 0) / 100.0 if revenue_data else 0.0

        failed_transactions = results[12]

        document_types = None
        if not org_id:
            document_types = {}
            for item in results[13]: document_types[item["_id"] or "Unknown"] = item["count"]

        global_tokens_data = results[14]
        total_tokens_all, msgs_with_tokens, chats_with_tokens, users_with_tokens = 0, 0, 0, 0
        if global_tokens_data:
            total_tokens_all = global_tokens_data[0].get("total_tokens", 0)
            msgs_with_tokens = global_tokens_data[0].get("msg_count", 0)
            chat_list = global_tokens_data[0].get("chat_ids", [])
            chats_with_tokens = len([c for c in chat_list if c])
            user_list = global_tokens_data[0].get("users", [])
            users_with_tokens = len([u for u in user_list if u])
        
        avg_tokens_per_msg = (total_tokens_all / msgs_with_tokens) if msgs_with_tokens > 0 else 0
        avg_tokens_per_chat = (total_tokens_all / chats_with_tokens) if chats_with_tokens > 0 else 0
        avg_tokens_per_user = (total_tokens_all / users_with_tokens) if users_with_tokens > 0 else 0

        messages_counts = {item["_id"]: item["count"] // 2 for item in results[15]}
        tokens_counts = {item["_id"]: int(item.get("tokens", 0)) for item in results[16]}
        users_counts = {item["_id"]: item["count"] for item in results[17]}
        revenue_counts = {item["_id"]: item["total"] / 100.0 for item in results[18]}

        time_series = []
        current_day = start_date
        while current_day <= end_date:
            day_str = current_day.strftime("%Y-%m-%d")
            time_series.append(TimeSeriesData(
                date=day_str,
                messages=messages_counts.get(day_str, 0),
                users=users_counts.get(day_str, 0),
                tokens=tokens_counts.get(day_str, 0),
                revenue=revenue_counts.get(day_str, 0.0)
            ))
            current_day += timedelta(days=1)

        logger.info(f"[ANALYTICS-PERF] === Total Dashboard Insights execution: {time.perf_counter() - t_start:.4f}s ===")

        active_trial_subscriptions = results[19]
        active_paid_subscriptions = results[20]
        
        active_subscriptions_by_plan = {}
        for item in results[21]:
            active_subscriptions_by_plan[item["_id"] or "unknown"] = item["count"]
            
        canceled_stripe_subscriptions = results[22]
        pending_cancellation_stripe_subscriptions = results[23]
        past_due_stripe_subscriptions = results[24]
        total_voice_streaming_sessions = results[25]
        
        total_users_had_trial, trial_to_paid_conversion_count, trial_to_paid_conversion_rate = results[26]
        individual_users_count = results[27]
        organization_users_count = results[28]
        active_org_subscriptions = results[29]
        active_individual_subscriptions = results[30]
        active_org_trials = results[31]
        total_orgs_count = results[32]

        # Calculate MRR
        estimated_mrr = 0.0
        for plan_name, count in active_subscriptions_by_plan.items():
            if plan_name in ["free_trial", "trial"]:
                continue
            if plan_name == "student":
                estimated_mrr += 9.99 * count
            elif plan_name == "individual_monthly":
                estimated_mrr += 19.00 * count
            elif plan_name == "individual_yearly":
                estimated_mrr += (199.00 / 12.0) * count
            elif plan_name == "organization_monthly":
                estimated_mrr += 99.00 * count
            elif plan_name == "organization_yearly":
                estimated_mrr += (999.00 / 12.0) * count
            elif plan_name == "pro":
                estimated_mrr += 19.00 * count

        average_messages_per_chat = (total_messages / total_chats) if total_chats > 0 else 0.0
        average_chats_per_user = (total_chats / total_users) if total_users > 0 else 0.0

        # Build dynamic dashboard sections mapping
        dynamic_sections = []

        # 1. Volume Metrics Card Section
        volume_widgets = []
        if is_org_admin:
            # Org admin dashboard: only active users is enabled
            volume_widgets.append({
                "type": "card",
                "id": "active_users",
                "title": "Active Users",
                "value": f"{active_users:,}",
                "icon": "users",
                "color": "text-green-600",
                "bg": "bg-green-50"
            })
            # Currently commented out for org_admin dashboard except active users:
            # volume_widgets.append({
            #     "type": "card",
            #     "id": "total_chats",
            #     "title": "Total Chats",
            #     "value": f"{total_chats:,}",
            #     "icon": "message-square",
            #     "color": "text-indigo-600",
            #     "bg": "bg-indigo-50"
            # })
            # volume_widgets.append({
            #     "type": "card",
            #     "id": "total_messages",
            #     "title": "Total Messages",
            #     "value": f"{total_messages:,}",
            #     "icon": "zap",
            #     "color": "text-amber-600",
            #     "bg": "bg-amber-50"
            # })
            # volume_widgets.append({
            #     "type": "card",
            #     "id": "total_soap_reports",
            #     "title": "SOAP Reports",
            #     "value": f"{total_soap_reports:,}",
            #     "icon": "file-text",
            #     "color": "text-emerald-600",
            #     "bg": "bg-emerald-50"
            # })
        elif not org_id:
            # Super Admin: Global View
            volume_widgets.append({
                "type": "card",
                "id": "total_users",
                "title": "Total Users",
                "value": f"{total_users:,}",
                "icon": "users",
                "color": "text-slate-600",
                "bg": "bg-slate-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_chats",
                "title": "Total Chats",
                "value": f"{total_chats:,}",
                "icon": "message-square",
                "color": "text-indigo-600",
                "bg": "bg-indigo-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_messages",
                "title": "Total Messages",
                "value": f"{total_messages:,}",
                "icon": "zap",
                "color": "text-amber-600",
                "bg": "bg-amber-50"
            })
            if total_documents is not None:
                volume_widgets.append({
                    "type": "card",
                    "id": "total_documents",
                    "title": "Documents in RAG",
                    "value": f"{total_documents:,}",
                    "icon": "file-text",
                    "color": "text-cyan-600",
                    "bg": "bg-cyan-50"
                })
            volume_widgets.append({
                "type": "card",
                "id": "total_soap_reports",
                "title": "SOAP Reports",
                "value": f"{total_soap_reports:,}",
                "icon": "file-text",
                "color": "text-emerald-600",
                "bg": "bg-emerald-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_organizations",
                "title": "Total Clinics",
                "value": f"{total_organizations:,}",
                "icon": "users",
                "color": "text-blue-600",
                "bg": "bg-blue-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_super_admins",
                "title": "Super Admins",
                "value": f"{total_super_admins:,}",
                "icon": "users",
                "color": "text-rose-600",
                "bg": "bg-rose-50"
            })
        else:
            # Super Admin: Filtered by Organization
            volume_widgets.append({
                "type": "card",
                "id": "active_users",
                "title": "Active Users",
                "value": f"{active_users:,}",
                "icon": "users",
                "color": "text-green-600",
                "bg": "bg-green-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_chats",
                "title": "Total Chats",
                "value": f"{total_chats:,}",
                "icon": "message-square",
                "color": "text-indigo-600",
                "bg": "bg-indigo-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_messages",
                "title": "Total Messages",
                "value": f"{total_messages:,}",
                "icon": "zap",
                "color": "text-amber-600",
                "bg": "bg-amber-50"
            })
            volume_widgets.append({
                "type": "card",
                "id": "total_soap_reports",
                "title": "SOAP Reports",
                "value": f"{total_soap_reports:,}",
                "icon": "file-text",
                "color": "text-emerald-600",
                "bg": "bg-emerald-50"
            })

        dynamic_sections.append({
            "title": "Volume Metrics",
            "type": "grid",
            "widgets": volume_widgets
        })

        # 2. Token Efficiency Section (Included for Super Admin, omitted for org_admin)
        if not is_org_admin:
            efficiency_widgets = [
                {
                    "type": "card",
                    "id": "avg_tokens_per_message",
                    "title": "Avg Tokens / Message",
                    "value": f"{round(avg_tokens_per_msg):,}",
                    "icon": "zap",
                    "color": "text-purple-600",
                    "bg": "bg-purple-50"
                },
                {
                    "type": "card",
                    "id": "avg_tokens_per_chat",
                    "title": "Avg Tokens / Chat",
                    "value": f"{round(avg_tokens_per_chat):,}",
                    "icon": "message-square",
                    "color": "text-indigo-600",
                    "bg": "bg-indigo-50"
                },
                {
                    "type": "card",
                    "id": "avg_tokens_per_user",
                    "title": "Avg Tokens / User",
                    "value": f"{round(avg_tokens_per_user):,}",
                    "icon": "users",
                    "color": "text-cyan-600",
                    "bg": "bg-cyan-50"
                }
            ]
            dynamic_sections.append({
                "title": "Token Efficiency",
                "type": "grid",
                "widgets": efficiency_widgets
            })

        # 3. Super Admin Premium & Subscription Insights Section (only for super admin / non-org filtered)
        if not org_id:
            subscription_widgets = [
                {
                    "type": "card",
                    "id": "active_trial_subscriptions",
                    "title": "Active Trials",
                    "value": f"{active_trial_subscriptions:,}",
                    "icon": "users",
                    "color": "text-orange-600",
                    "bg": "bg-orange-50"
                },
                {
                    "type": "card",
                    "id": "active_paid_subscriptions",
                    "title": "Active Premium Users",
                    "value": f"{active_paid_subscriptions:,}",
                    "icon": "zap",
                    "color": "text-violet-600",
                    "bg": "bg-violet-50"
                },
                {
                    "type": "card",
                    "id": "estimated_mrr",
                    "title": "Estimated MRR",
                    "value": f"${estimated_mrr:,.2f}",
                    "icon": "dollar-sign",
                    "color": "text-green-600",
                    "bg": "bg-green-50"
                },
                {
                    "type": "card",
                    "id": "trial_to_paid_conversion_rate",
                    "title": "Trial Conversion",
                    "value": f"{trial_to_paid_conversion_rate:.1f}%",
                    "icon": "trending-up",
                    "color": "text-blue-600",
                    "bg": "bg-blue-50"
                },
                {
                    "type": "card",
                    "id": "total_voice_streaming_sessions",
                    "title": "Voice Sessions",
                    "value": f"{total_voice_streaming_sessions:,}",
                    "icon": "mic",
                    "color": "text-pink-600",
                    "bg": "bg-pink-50"
                },
                {
                    "type": "card",
                    "id": "past_due_stripe_subscriptions",
                    "title": "Past Due Subscriptions",
                    "value": f"{past_due_stripe_subscriptions:,}",
                    "icon": "alert-circle",
                    "color": "text-red-600",
                    "bg": "bg-red-50"
                }
            ]
            dynamic_sections.append({
                "title": "Premium & Subscription Insights",
                "type": "grid",
                "widgets": subscription_widgets
            })

            # Organization & Member Insights
            org_widgets = [
                {
                    "type": "card",
                    "id": "total_orgs_count",
                    "title": "Total Organizations",
                    "value": f"{total_orgs_count:,}",
                    "icon": "building",
                    "color": "text-blue-600",
                    "bg": "bg-blue-50"
                },
                {
                    "type": "card",
                    "id": "active_org_subscriptions",
                    "title": "Active Org Subscriptions",
                    "value": f"{active_org_subscriptions:,}",
                    "icon": "shield-check",
                    "color": "text-indigo-600",
                    "bg": "bg-indigo-50"
                },
                {
                    "type": "card",
                    "id": "organization_users_count",
                    "title": "Organization Member Users",
                    "value": f"{organization_users_count:,}",
                    "icon": "users",
                    "color": "text-emerald-600",
                    "bg": "bg-emerald-50"
                },
                {
                    "type": "card",
                    "id": "active_individual_subscriptions",
                    "title": "Individual Subscribers",
                    "value": f"{active_individual_subscriptions:,}",
                    "icon": "user-check",
                    "color": "text-amber-600",
                    "bg": "bg-amber-50"
                },
                {
                    "type": "card",
                    "id": "individual_users_count",
                    "title": "Total Individual Users",
                    "value": f"{individual_users_count:,}",
                    "icon": "user",
                    "color": "text-slate-600",
                    "bg": "bg-slate-50"
                },
                {
                    "type": "card",
                    "id": "active_org_trials",
                    "title": "Active Org Trials",
                    "value": f"{active_org_trials:,}",
                    "icon": "clock",
                    "color": "text-orange-600",
                    "bg": "bg-orange-50"
                }
            ]
            dynamic_sections.append({
                "title": "Organization & Member Insights",
                "type": "grid",
                "widgets": org_widgets
            })

        # 4. Charts Section (Time Series) (Included for Super Admin, omitted for org_admin)
        if not is_org_admin:
            chart_labels = []
            msg_data = []
            user_data = []
            token_data = []
            revenue_data_points = []
            for point in time_series:
                chart_labels.append(point.date)
                msg_data.append(point.messages)
                user_data.append(point.users)
                token_data.append(point.tokens)
                revenue_data_points.append(point.revenue)

            charts_widgets = [
                {
                    "type": "chart",
                    "id": "messages_chart",
                    "title": "Daily Message Activity",
                    "chart_type": "line",
                    "grid_width": 6,
                    "labels": chart_labels,
                    "datasets": [
                        {"label": "Messages", "data": msg_data, "color": "#818cf8"}
                    ]
                }
            ]

            if not org_id:
                charts_widgets.append({
                    "type": "chart",
                    "id": "users_chart",
                    "title": "New User Registrations",
                    "chart_type": "line",
                    "grid_width": 6,
                    "labels": chart_labels,
                    "datasets": [
                        {"label": "Users", "data": user_data, "color": "#34d399"}
                    ]
                })

            charts_widgets.append({
                "type": "chart",
                "id": "tokens_chart",
                "title": "Token Consumption",
                "chart_type": "line",
                "grid_width": 6,
                "labels": chart_labels,
                "datasets": [
                    {"label": "Tokens", "data": token_data, "color": "#a78bfa"}
                ]
            })

            if not org_id:
                charts_widgets.append({
                    "type": "chart",
                    "id": "revenue_chart",
                    "title": "Sales Revenue",
                    "chart_type": "line",
                    "grid_width": 6,
                    "labels": chart_labels,
                    "datasets": [
                        {"label": "Revenue ($)", "data": revenue_data_points, "color": "#fb7185"}
                    ]
                })

            dynamic_sections.append({
                "title": "Visual Analytics",
                "type": "charts_grid",
                "widgets": charts_widgets
            })

        if is_org_admin:
            # Org admin dashboard: only active users and its dynamic section are returned
            return {
                "active_users": active_users,
                "dynamic_sections": dynamic_sections
            }

        response_data = {
            "total_users": total_users,
            "active_pro_users": active_pro_users,
            "total_chats": total_chats,
            "total_messages": total_messages,
            "stripe_status_distribution": stripe_status_distribution,
            "total_revenue": round(total_revenue, 2),
            "failed_transactions": failed_transactions,
            "time_series": time_series,
            "avg_tokens_per_message": round(avg_tokens_per_msg, 2),
            "avg_tokens_per_chat": round(avg_tokens_per_chat, 2),
            "avg_tokens_per_user": round(avg_tokens_per_user, 2),
            "active_users": active_users,
            "role_counts": role_counts,
            "total_soap_reports": total_soap_reports,
            
            # New Super Admin Premium/Subscription Insights
            "active_trial_subscriptions": active_trial_subscriptions,
            "active_paid_subscriptions": active_paid_subscriptions,
            "active_subscriptions_by_plan": active_subscriptions_by_plan,
            "total_users_had_trial": total_users_had_trial,
            "trial_to_paid_conversion_count": trial_to_paid_conversion_count,
            "trial_to_paid_conversion_rate": round(trial_to_paid_conversion_rate, 2),
            "estimated_mrr": round(estimated_mrr, 2),
            "canceled_stripe_subscriptions": canceled_stripe_subscriptions,
            "pending_cancellation_stripe_subscriptions": pending_cancellation_stripe_subscriptions,
            "past_due_stripe_subscriptions": past_due_stripe_subscriptions,
            "average_messages_per_chat": round(average_messages_per_chat, 2),
            "average_chats_per_user": round(average_chats_per_user, 2),
            "total_voice_streaming_sessions": total_voice_streaming_sessions,
            
            # Org vs Individual breakdowns
            "individual_users_count": individual_users_count,
            "organization_users_count": organization_users_count,
            "active_org_subscriptions": active_org_subscriptions,
            "active_individual_subscriptions": active_individual_subscriptions,
            "active_org_trials": active_org_trials,

            # Dynamic sections list
            "dynamic_sections": dynamic_sections,
            "total_documents": total_documents,
            "document_types": document_types,
            "total_super_admins": total_super_admins,
            "total_organizations": total_organizations
        }

        return response_data

