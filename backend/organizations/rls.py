"""Canonical row-level-security inventory.

Maps every tenant-owned table to its expected policy so provisioning and
verification share one definition. Tables below are enforced with FORCE
ROW LEVEL SECURITY: no context means no rows, even for table owners.

Deliberately unpolicied (see the operator runbook for why):
- organizations_organization: identity directory, not tenant data; every
  access path rechecks live membership first.
- organizations_userpreference / organizations_userlogin /
  organizations_aiproviderconfig: per-user or global records the runtime
  and the admin console must read across tenants.
- pipeline_rulesetversion / pipeline_operatorspend: global reference and
  operator singleton, never tenant rows.
- auth, sessions, admin, contenttypes, and migration tables.
"""

from __future__ import annotations

TENANT_POLICIES = {
    # policy name follows {table}_org_isolation except where noted.
    "pipeline_sourcefile": "pipeline_sourcefile_org_isolation",
    "pipeline_importrun": "pipeline_importrun_org_isolation",
    "pipeline_sourcerow": "pipeline_sourcerow_org_isolation",
    "pipeline_normalizedoffer": "pipeline_normalizedoffer_org_isolation",
    "pipeline_validationresult": "pipeline_validationresult_org_isolation",
    "pipeline_reviewrequest": "pipeline_reviewrequest_org_isolation",
    "pipeline_reviewdecision": "pipeline_reviewdecision_org_isolation",
    "pipeline_exportartifact": "pipeline_exportartifact_org_isolation",
    "pipeline_usageevent": "pipeline_usageevent_org_isolation",
    "pipeline_auditevent": "pipeline_auditevent_org_isolation",
    "pipeline_spendallowance": "pipeline_spendallowance_org_isolation",
    "pipeline_columnmappingmemory": "pipeline_columnmappingmemory_org_isolation",
    "pipeline_pricingrule": "pipeline_pricingrule_org_isolation",
    "pipeline_category": "pipeline_category_org_isolation",
    "pipeline_supplier": "pipeline_supplier_org_isolation",
    "pipeline_suppliernote": "pipeline_suppliernote_org_isolation",
    "pipeline_productalias": "pipeline_productalias_org_isolation",
    "pipeline_customerquote": "pipeline_customerquote_org_isolation",
    "pipeline_spendreservation": "pipeline_spendreservation_org_isolation",
    "pipeline_assistantflag": "pipeline_assistantflag_org_isolation",
    "pipeline_assistidempotency": "pipeline_assistidempotency_org_isolation",
    "organizations_invitation": "invitation_org_isolation",
    "organizations_membership": "membership_user_or_org_isolation",
}

TENANT_TABLES = tuple(TENANT_POLICIES)

BOOTSTRAP_FUNCTIONS = (
    "offerflow_run_org(integer)",
    "offerflow_invitation_org(text)",
)

BOOTSTRAP_OWNER = "offerflow_bootstrap"
