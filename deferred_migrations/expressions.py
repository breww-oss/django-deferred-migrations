from django.db.models import F
from django.db.models import Q
from django.db.models.expressions import BaseExpression


def expression_field_names(expression: object) -> set[str]:
    if isinstance(expression, F):
        return {expression.name.split("__")[0]}

    if isinstance(expression, Q):
        names: set[str] = set()

        for child in expression.children:
            # Q children are (lookup, value) pairs, except when a bare expression is passed positionally, as in Q(Exists(...)).
            if isinstance(child, (tuple, list)) and len(child) == 2:
                lookup, value = child
                names.add(str(lookup).split("__")[0])
                names |= expression_field_names(value)
            else:
                names |= expression_field_names(child)

        return names

    if not isinstance(expression, BaseExpression):
        return set()

    names: set[str] = set()

    for source in expression.get_source_expressions():
        if source is not None:
            names |= expression_field_names(source)

    return names
