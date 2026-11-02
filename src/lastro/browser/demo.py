"""Bindings explícitos para os formulários do demo local."""

from lastro.config.policy import Binding, BrowserPolicy, RequestBinding, Target
from lastro.config.testdata import EntityTestData, TestDataSettings

FIELDS = {
    name: name for name in ("full_name", "cpf", "email", "phone", "role", "hire_date", "status")
}


def demo_policy() -> BrowserPolicy:
    return BrowserPolicy(
        bindings=[
            Binding(
                id="employee-create",
                entity="employee",
                operation="create",
                route="/employees/new",
                target=Target(role="button", name="Salvar"),
                form="employee-form",
                fields=FIELDS,
                field_values={"status": ["active"]},
                request=RequestBinding(
                    method="POST", path="/employees", success_status=200, location_id=True
                ),
            ),
            Binding(
                id="employee-update",
                entity="employee",
                operation="update",
                route="/employees/{id}/edit",
                target=Target(role="button", name="Salvar"),
                form="employee-form",
                fields=FIELDS,
                field_values={"status": ["active"]},
                request=RequestBinding(method="POST", path="/employees/{id}", success_status=200),
            ),
        ]
    )


def demo_test_data() -> TestDataSettings:
    return TestDataSettings(
        entities={
            "employee": EntityTestData(
                detail_path="/employees/{id}",
                cleanup_instructions=(
                    "Abra o funcionário pelo link de detalhe e remova-o manualmente; "
                    "o Lastro não executa a limpeza."
                ),
            )
        }
    )
