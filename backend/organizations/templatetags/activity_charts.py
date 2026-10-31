from django import template

register = template.Library()


@register.inclusion_tag("admin/activity_charts.html")
def activity_charts():
    from organizations.activity import ranked_users

    by_tokens = ranked_users(days="30", sort="tokens", limit=8)
    by_logins = ranked_users(days="30", sort="logins", limit=8)
    top_tokens = by_tokens[0]["tokens"] if by_tokens else 0
    top_logins = by_logins[0]["logins"] if by_logins else 0
    for row in by_tokens:
        row["width"] = round(100 * row["tokens"] / top_tokens) if top_tokens else 0
    for row in by_logins:
        row["width"] = round(100 * row["logins"] / top_logins) if top_logins else 0
    return {
        "by_tokens": by_tokens,
        "by_logins": by_logins,
        "has_data": bool(top_tokens or top_logins),
    }
