"""Bindings explícitos para os formulários do demo local."""

from lastro.config.policy import Binding, BrowserPolicy, RequestBinding, Target

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
                request=RequestBinding(method="POST", path="/employees", success_status=200),
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
