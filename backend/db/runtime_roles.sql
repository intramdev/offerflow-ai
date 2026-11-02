-- OfferFlow runtime-role provisioning. Single source of truth, executed by
-- `manage.py provision_runtime` on the privileged admin connection after
-- variable substitution. Also readable as DBA documentation: with psql,
-- pass -v APP_ROLE=... -v SCHEMA_OWNER=... and replace the {{TOKENS}}.
--
-- {{APP_ROLE}}: the restricted runtime role (web + worker). Never a
--   superuser, never BYPASSRLS, owns nothing.
-- {{SCHEMA_OWNER}}: the role that owns the tables and runs migrations.
-- {{DB_NAME}}: the database being provisioned.
--
-- The bootstrap role owns only the two narrow SECURITY DEFINER resolvers.
-- It cannot log in, and its BYPASSRLS attribute takes effect solely inside
-- those functions, which each return a single organization id.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'offerflow_bootstrap') THEN
        CREATE ROLE offerflow_bootstrap WITH NOLOGIN NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
END
$$;

-- Least privilege for the runtime role.
REVOKE CREATE ON SCHEMA public FROM {{APP_ROLE}};
GRANT CONNECT ON DATABASE "{{DB_NAME}}" TO {{APP_ROLE}};
GRANT USAGE ON SCHEMA public TO {{APP_ROLE}};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {{APP_ROLE}};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {{APP_ROLE}};
ALTER DEFAULT PRIVILEGES FOR ROLE {{SCHEMA_OWNER}} IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {{APP_ROLE}};
ALTER DEFAULT PRIVILEGES FOR ROLE {{SCHEMA_OWNER}} IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO {{APP_ROLE}};

-- Tenant bootstrap resolvers. Each reveals one organization id and nothing
-- else; callers still verify membership, tokens, and email matches.
CREATE OR REPLACE FUNCTION offerflow_run_org(p_run_id integer)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
    SELECT organization_id FROM public.pipeline_importrun WHERE id = p_run_id
$$;
ALTER FUNCTION offerflow_run_org(integer) OWNER TO offerflow_bootstrap;
REVOKE ALL ON FUNCTION offerflow_run_org(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION offerflow_run_org(integer) TO {{APP_ROLE}};

CREATE OR REPLACE FUNCTION offerflow_invitation_org(p_token text)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
    SELECT organization_id FROM public.organizations_invitation
    WHERE token = p_token AND accepted_at IS NULL
$$;
ALTER FUNCTION offerflow_invitation_org(text) OWNER TO offerflow_bootstrap;
REVOKE ALL ON FUNCTION offerflow_invitation_org(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION offerflow_invitation_org(text) TO {{APP_ROLE}};

-- The bootstrap role can read only the columns its two resolvers need.
-- Ordinary table privileges still apply inside SECURITY DEFINER code;
-- BYPASSRLS alone only exempts the row policies.
GRANT SELECT (id, organization_id) ON public.pipeline_importrun
    TO offerflow_bootstrap;
GRANT SELECT (token, organization_id, accepted_at)
    ON public.organizations_invitation TO offerflow_bootstrap;
