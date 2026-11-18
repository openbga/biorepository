from django.apps import AppConfig


class DistributionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "distribution"
    verbose_name = "Distribution"

    def ready(self):
        from specimens.record_links import DISTRIBUTION_SOURCES

        from .record_rows import specimen_rows

        if specimen_rows not in DISTRIBUTION_SOURCES:
            DISTRIBUTION_SOURCES.append(specimen_rows)
