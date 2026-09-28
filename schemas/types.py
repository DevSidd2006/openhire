from typing import Annotated

from pydantic import StringConstraints

# Strips leading/trailing whitespace, then rejects empty results.
# "   " and "\t\n" raise ValidationError; "  hi  " becomes "hi".
NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
