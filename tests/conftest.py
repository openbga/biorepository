import pytest
from django.contrib.auth.models import Group

from accounts.models import User
from core.models import Repository


@pytest.fixture
def staff(db):
    def make(name, role):
        user = User.objects.create_user(name, f"{name}@example.invalid", "test-password-729!")
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    return make


@pytest.fixture
def repository(db):
    repository, _ = Repository.objects.get_or_create(
        pk=1, defaults={"name": "Example Biorepository"}
    )
    return repository
