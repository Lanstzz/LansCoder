from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class UserInputOption:

    id: str
    label: str
    description: str = ""


@dataclass(slots=True)
class UserInputRequest:

    id: str
    kind: Literal["ask_user", "permission_confirmation"]
    question: str
    options: list[UserInputOption] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
