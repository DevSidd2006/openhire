"""
Profile page backend: account-level profile fields on `users`, and the
GET/PATCH /auth/me + POST /auth/me/password endpoints.

Candidate resume data (skills, experience, education) is deliberately NOT
duplicated here - it stays owned by `CandidateRecord`/`ParsedResume` and is
only ever read, never written, through this module's endpoints. See
`repositories/interfaces.py`'s `CandidateRecord` docstring for why.

BYOK key storage is out of scope for this module - see the `byok` branch.
"""
from repositories.interfaces import UserRecord


class TestUserRecordProfileFields:
    def test_profile_fields_default_to_none_and_round_trip(self):
        """A UserRecord built the way signup builds it today (no profile
        fields supplied) must still validate - these columns are additive,
        not required."""
        record = UserRecord(
            user_id="user_1",
            email="a@example.com",
            password_hash="hash",
            user_type="candidate",
        )
        assert record.full_name is None
        assert record.company_name is None

        updated = record.model_copy(update={"full_name": "Jane Doe", "phone": "555-0100"})
        assert updated.full_name == "Jane Doe"
        assert updated.phone == "555-0100"
        # Untouched fields still round-trip.
        assert updated.email == "a@example.com"
