from __future__ import annotations


class SessionError(Exception):
    pass


class SessionNotFoundError(SessionError):
    pass


class SessionInvalidIdError(SessionError):
    pass


class SessionEmptyError(SessionError):
    pass


class SessionCorruptError(SessionError):
    pass


class SessionUnsupportedSchemaError(SessionError):

    def __init__(self, *, session_id: str, actual_version: str, expected_version: str) -> None:
        self.session_id = session_id
        self.actual_version = actual_version
        self.expected_version = expected_version
        super().__init__(f"session {session_id} uses context event schema {actual_version}; " f"expected {expected_version}")
