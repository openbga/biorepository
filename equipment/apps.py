from django.apps import AppConfig


def open_excursion_units():
    """Storage units with an open temperature excursion, for the path bar's census."""
    from .models import Excursion

    return Excursion.objects.filter(
        status=Excursion.Status.OPEN, storage_unit__isnull=False
    ).values_list("storage_unit_id", flat=True)


class EquipmentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "equipment"
    verbose_name = "Equipment"

    def ready(self):
        from storage.pathbar import EXCURSION_SOURCES

        if open_excursion_units not in EXCURSION_SOURCES:
            EXCURSION_SOURCES.append(open_excursion_units)
