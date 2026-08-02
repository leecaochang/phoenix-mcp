"""mesa-core: reference implementation of the MESA specification."""

from custom_components.phoenix_mcp.mesa_core.audit import MesaAuditEvent, emit_audit_event
from custom_components.phoenix_mcp.mesa_core.conflict import ConflictResolver
from custom_components.phoenix_mcp.mesa_core.enforcer import ConfirmationManager, EnforcementResult, MesaEnforcer
from custom_components.phoenix_mcp.mesa_core.exceptions import (
    InvalidCursorError,
    LeaseNotFoundError,
    MesaEnforcementError,
    MesaError,
    MesaValidationError,
)
from custom_components.phoenix_mcp.mesa_core.inheritance import InheritanceResolver, ProfileExplanation
from custom_components.phoenix_mcp.mesa_core.integration_import import import_from_integration
from custom_components.phoenix_mcp.mesa_core.lease import Lease, LeaseManager, LeaseResponse
from custom_components.phoenix_mcp.mesa_core.migration import migrate_profile
from custom_components.phoenix_mcp.mesa_core.portability import (
    ImportResult,
    aexport_profiles,
    aimport_profiles,
    export_profiles,
    import_profiles,
)
from custom_components.phoenix_mcp.mesa_core.privacy import AccessDecision, CallerContext, PrivacyEnforcer
from custom_components.phoenix_mcp.mesa_core.profile import (
    DOMAIN_SAFETY_BASELINE,
    HA_TARGET_SELECTOR_KEYS,
    HELPER_DOMAINS,
    ControlMode,
    FreshnessReport,
    MetadataOrigin,
    OperationalBoundaries,
    PersonTraits,
    PrivacyClassification,
    PrivacyLevel,
    ProfileMetadata,
    SemanticProfile,
    TriggersAutomations,
)
from custom_components.phoenix_mcp.mesa_core.store import DeploymentDefaults, ProfileQueryResult, ProfileStore, QueryRow
from custom_components.phoenix_mcp.mesa_core.temporal import TemporalEvaluator, TemporalResult
from custom_components.phoenix_mcp.mesa_core.trigger_validator import (
    TriggerValidator,
    ValidationIssue,
    entities_by_role,
)
from custom_components.phoenix_mcp.mesa_core.validation import ValidationReport, validate_document, validate_or_raise

__version__ = "1.3.0"

__all__ = [
    "DOMAIN_SAFETY_BASELINE",
    "HA_TARGET_SELECTOR_KEYS",
    "HELPER_DOMAINS",
    "AccessDecision",
    "CallerContext",
    "ConfirmationManager",
    "ConflictResolver",
    "ControlMode",
    "DeploymentDefaults",
    "EnforcementResult",
    "FreshnessReport",
    "ImportResult",
    "InheritanceResolver",
    "InvalidCursorError",
    "Lease",
    "LeaseManager",
    "LeaseNotFoundError",
    "LeaseResponse",
    "MesaAuditEvent",
    "MesaEnforcementError",
    "MesaEnforcer",
    "MesaError",
    "MesaValidationError",
    "MetadataOrigin",
    "OperationalBoundaries",
    "PersonTraits",
    "PrivacyClassification",
    "PrivacyEnforcer",
    "PrivacyLevel",
    "ProfileExplanation",
    "ProfileMetadata",
    "ProfileQueryResult",
    "ProfileStore",
    "QueryRow",
    "SemanticProfile",
    "TemporalEvaluator",
    "TemporalResult",
    "TriggerValidator",
    "TriggersAutomations",
    "ValidationIssue",
    "ValidationReport",
    "aexport_profiles",
    "aimport_profiles",
    "emit_audit_event",
    "entities_by_role",
    "export_profiles",
    "import_from_integration",
    "import_profiles",
    "migrate_profile",
    "validate_document",
    "validate_or_raise",
]
