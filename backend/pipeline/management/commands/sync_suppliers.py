"""Backfill supplier records from already-imported offers."""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Create supplier records for names found on existing offers. "
        "Imports do this automatically; run once per database that "
        "imported files before suppliers existed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--organization-id",
            type=int,
            default=None,
            help="Only backfill one organization.",
        )

    def handle(self, *args, organization_id=None, **options):
        from organizations.models import Organization
        from pipeline.models import NormalizedOffer, Supplier
        from pipeline.suppliers import ensure_suppliers

        orgs = Organization.objects.all().order_by("id")
        if organization_id is not None:
            orgs = orgs.filter(id=organization_id)
            if not orgs.exists():
                raise CommandError(
                    f"No organization with id {organization_id}."
                )
        created_total = 0
        for org in orgs:
            before = Supplier.objects.filter(organization=org).count()
            names = sorted(
                set(
                    NormalizedOffer.objects.filter(organization=org)
                    .exclude(supplier="")
                    .values_list("supplier", flat=True)
                )
            )
            ensure_suppliers(org.id, names)
            created = (
                Supplier.objects.filter(organization=org).count() - before
            )
            created_total += created
            self.stdout.write(
                f"{org.name}: {len(names)} names seen, "
                f"{created} suppliers created."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {created_total} suppliers created."
            )
        )
