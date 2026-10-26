from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

Version = Annotated[int, Field(strict=True, ge=1)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Short = Annotated[str, Field(min_length=1, max_length=160)]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenRequest(Command):
    login: Short
    password: Annotated[str, Field(min_length=1, max_length=256)]


class AssessmentCommand(Command):
    identity_verified: bool
    risk_score: Annotated[int, Field(strict=True, ge=0, le=100)]
    evidence_reference: Short


class ActionCommand(Command):
    kind: Literal[
        "DISPUTE_REWORK",
        "DISPUTE_CLOSE",
        "CONTROL_CLEAR",
        "PAYOUT_RETURN",
        "EXCEPTION_CLOSE",
        "DELIVERY_REPLAY",
        "CANCEL_UNRELEASED",
        "PAYOUT_RETRY",
    ]
    target_id: UUID
    reason: Text
    evidence_reference: Short


class DecisionCommand(Command):
    expected_version: Version
    approve: bool


class ReturnCommand(Command):
    reference: UUID
    original_reference: UUID


class SubmissionCommand(Command):
    expected_version: Version
    description: Text
    evidence_reference: Short


class ApprovalCommand(Command):
    submission_id: UUID
    review_window_id: UUID
    expected_version: Version


class RevisionCommand(ApprovalCommand):
    reason: Text


class DisputeCommand(Command):
    expected_version: Version
    reason: Text


class ReconciliationCommand(Command):
    from_inclusive: datetime
    to_exclusive: datetime

    @model_validator(mode="after")
    def valid_interval(self):
        if not self.from_inclusive.tzinfo or not self.to_exclusive.tzinfo:
            raise ValueError("Timezone-aware timestamps required")
        seconds = (self.to_exclusive - self.from_inclusive).total_seconds()
        if not 0 < seconds <= 31 * 86400:
            raise ValueError("Interval must be positive and at most 31 days")
        return self


class TransferCommand(Command):
    reference: UUID
    source_account_ref: Short
    beneficiary_account_ref: Short
    amount_minor: Annotated[int, Field(strict=True, ge=1, le=9000000000000)]
    currency: Literal["PKR"]

    @model_validator(mode="after")
    def different_accounts(self):
        if self.source_account_ref == self.beneficiary_account_ref:
            raise ValueError("Source and beneficiary must differ")
        return self
