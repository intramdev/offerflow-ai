from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "organizations"

    def ready(self):
        # Imported for signal side effects.
        from django.core.checks import register

        from organizations import checks, signals

        assert signals is not None
        register(checks.check_runtime_role_is_restricted)
