from validator import Validator


class ValidatorAgent:
    """Checks the plan before it can become an output row. Reports problems; never repairs them."""

    name = "validate"

    def __init__(self, validator: Validator):
        self._validator = validator

    def __call__(self, state) -> dict:
        request, plan = state["request"], state["plan"]
        return {"validation": tuple(self._validator.validate(request.case_id, plan.status, plan.steps, request.audit_at))}
