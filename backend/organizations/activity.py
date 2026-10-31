"""Staff-only activity dashboard: who logs in and who spends tokens."""

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone


def ranked_users(*, days="30", sort="tokens", limit=None):
    """Users with sign-in and token totals, most active first."""
    from organizations.models import UserLogin
    from pipeline.models import UsageEvent

    if sort not in ("tokens", "logins"):
        sort = "tokens"
    if days not in ("7", "30", "all"):
        days = "30"
    since = None if days == "all" else timezone.now() - timedelta(days=int(days))
    users = list(
        get_user_model()
        .objects.filter(is_active=True)
        .prefetch_related("org_memberships__organization")
        .order_by("username")
    )
    rows = []
    for user in users:
        logins = UserLogin.objects.filter(user=user)
        if since is not None:
            logins = logins.filter(created_at__gte=since)
        login_count = logins.count()
        last_login = logins.values_list("created_at", flat=True).first()
        spend = UsageEvent.objects.filter(
            Q(user=user)
            | Q(
                user__isnull=True,
                run__source_file__uploaded_by=user,
            ),
            kind="provider_call",
        )
        if since is not None:
            spend = spend.filter(created_at__gte=since)
        tokens = spend.aggregate(total=Sum("quantity"))["total"] or 0
        rows.append(
            {
                "username": user.username,
                "email": user.email,
                "full_name": user.get_full_name(),
                "companies": ", ".join(
                    sorted(
                        {
                            membership.organization.name
                            for membership in user.org_memberships.all()
                        }
                    )
                ),
                "logins": login_count,
                "tokens": tokens,
                "last_login": last_login,
            }
        )
    if sort == "logins":
        rows.sort(key=lambda row: (-row["logins"], -row["tokens"], row["username"]))
    else:
        rows.sort(key=lambda row: (-row["tokens"], -row["logins"], row["username"]))
    if limit is not None:
        rows = rows[:limit]
    return rows


@staff_member_required
def activity_dashboard(request):
    sort = request.GET.get("sort", "tokens")
    if sort not in ("tokens", "logins"):
        sort = "tokens"
    days = request.GET.get("days", "30")
    if days not in ("7", "30", "all"):
        days = "30"
    rows = ranked_users(days=days, sort=sort)
    return render(
        request,
        "admin/activity_dashboard.html",
        {
            "title": "User activity",
            "rows": rows[:100],
            "truncated": len(rows) > 100,
            "sort": sort,
            "days": days,
            "login_total": sum(row["logins"] for row in rows),
            "token_total": sum(row["tokens"] for row in rows),
        },
    )
