"""
tests._evidence_test_helpers
================================
Helper COMPARTILHADO para os testes que precisam de evidência
genuinamente VERIFIED — CORREÇÃO (rodadas anteriores + item 1 desta
rodada): `VERIFIED` agora exige um `VerifierRegistry`/`SourceRegistry`
com registros feitos através de um `AuthorityGrant` REAL, emitido por
um `AuthorityRegistry` genuinamente bootstrapado — nunca mais uma
string `authority_level` autodeclarada. Este helper bootstrapa uma
autoridade raiz de teste, emite um grant com escopo apropriado, e só
então registra o verificador/fontes de teste — nunca usado em
produção (o padrão de produção continua sendo registries VAZIOS, sem
bootstrap)."""

from core.evidence.authority_registry import (
    SCOPE_REGISTER_SOURCE, SCOPE_REGISTER_VERIFIER, AuthorityRegistry,
)
from core.evidence.verifier_registry import TrustLevel, VerifierRegistration, VerifierRegistry
from core.evidence.source_registry import SourceRegistry, SourceRegistration, SourceType, TrustClass
from core.evidence.registry import VerificationStatus
from core.capabilities.registry import CapabilityHealth


def make_authenticated_test_owner_session():
    """CORREÇÃO (itens 1/2/9/10 rodada anterior + itens 1/2/3 desta
    rodada — Owner não pode ser autodeclarado, auth_service precisa
    ser canônico): monta um `OwnerAuthService` de teste REAL via
    `create_canonical_auth_service()` e faz login de verdade
    (e-mail+senha verificados via PBKDF2) — devolve `(auth_service,
    session_token)`, a ÚNICA base aceita para bootstrapar autoridade
    raiz. Nunca usado em produção (fixture de teste — credenciais
    fixas de teste, não segredo real)."""
    from core.security.auth import create_canonical_auth_service
    from tests._auth_test_helpers import fast_hash_password

    auth_service = create_canonical_auth_service(
        owner_email="owner_teste@fixture.local", stored_password_hash=fast_hash_password("SenhaDeTesteForte123"),
    )
    session_token = auth_service.login("owner_teste@fixture.local", "SenhaDeTesteForte123")
    return auth_service, session_token


def _bootstrap_test_authority(*, scope: str):
    """Bootstrapa uma autoridade raiz de teste (OWNER) a partir de uma
    sessão Owner REAL, e emite, a partir dela, um grant GOVERNANCE com
    o escopo pedido — nunca aceita `authority_level`/`owner_id` como
    string solta; sempre passa pelo fluxo real (sessão autenticada ->
    `issue_owner_principal` -> `bootstrap_root_authority` ->
    `issue_grant`)."""
    from core.evidence.owner_principal import issue_owner_principal

    auth_service, session_token = make_authenticated_test_owner_session()
    principal = issue_owner_principal(auth_service=auth_service, session_token=session_token)
    authority_registry = AuthorityRegistry()
    root_grant = authority_registry.bootstrap_root_authority(
        owner_principal=principal, auth_service=auth_service, bootstrap_reason="fixture de teste automatizado",
    )
    grant = authority_registry.issue_grant(
        issuer_grant_id=root_grant.grant_id, subject_actor_id="governance_teste", authority_level="GOVERNANCE",
        scopes=[scope], issuance_reason="fixture de teste automatizado",
    )
    return authority_registry, grant.grant_id


def make_test_verifier_registry() -> VerifierRegistry:
    authority_registry, grant_id = _bootstrap_test_authority(scope=SCOPE_REGISTER_VERIFIER)
    registry = VerifierRegistry(authority_registry=authority_registry)
    registry.register(
        VerifierRegistration(
            verifier_id="teste_automatizado", verifier_type="automated_test", authorized=True,
            capabilities=["evidence_verification"], verification_methods=[],  # lista vazia = qualquer metodo aceito
            health=CapabilityHealth.HEALTHY, trust_level=TrustLevel.STANDARD, scope="TEST",
        ),
        authority_grant_id=grant_id, registration_reason="fixture de teste automatizado",
    )
    return registry


def make_test_source_registry() -> SourceRegistry:
    """Fontes de teste EXPLICITAMENTE registradas através de um grant
    GOVERNANCE genuíno — 'fonte_real'/'fonte'/'planilha' resolvem para
    uma `SourceRegistration` real (`TRUSTED_FIRST_PARTY`, `VERIFIED`,
    `HEALTHY`). Qualquer OUTRA string de source (ex:
    'totally-fake-source') NUNCA resolve."""
    authority_registry, grant_id = _bootstrap_test_authority(scope=SCOPE_REGISTER_SOURCE)
    registry = SourceRegistry(authority_registry=authority_registry)
    for source_id in ("fonte_real", "fonte", "planilha"):
        registry.register(
            SourceRegistration(
                source_id=source_id, source_type=SourceType.FIRST_PARTY, source_reference=f"test://{source_id}",
                requested_trust_class=TrustClass.TRUSTED_FIRST_PARTY, health=CapabilityHealth.HEALTHY,
                requested_verification_status=VerificationStatus.VERIFIED, verification_method="validacao_manual",
            ),
            authority_grant_id=grant_id, registration_reason="fixture de teste automatizado",
        )
    return registry


def make_valid_provenance_record(*, evidence_ids):
    """`ProvenanceRecord` de teste genuinamente válida —
    `source_reference='planilha'` (registrada em
    `make_test_source_registry()`), `related_evidence_ids` apontando
    para evidência real (o CHAMADOR precisa ter registrado essas
    evidências como VERIFIED antes)."""
    from core.evidence.provenance import ProvenanceRecord

    return ProvenanceRecord(
        source_type="OWN_OPERATION_DATA", source_reference="planilha", collection_method="export",
        collector="agent", verifier="teste_automatizado", verification_method="validacao_manual",
        integrity_reference="sha256:teste", related_evidence_ids=list(evidence_ids),
    )


def make_authority_grant(*, scope, authority_level="GOVERNANCE", actor_id="governance_teste"):
    """Helper de baixo nível para testes que precisam controlar
    diretamente o `AuthorityRegistry`/grant (ex: testar cenários de
    autoridade insuficiente, nível errado, etc.). Bootstrapa uma
    autoridade raiz de teste a partir de uma sessão Owner REAL, e
    emite, a partir dela, um grant com o escopo/nível pedidos — nunca
    aceita `authority_level`/`owner_id` como string solta. Devolve
    `(authority_registry, grant_id)`."""
    from core.evidence.authority_registry import AuthorityRegistry
    from core.evidence.owner_principal import issue_owner_principal

    auth_service, session_token = make_authenticated_test_owner_session()
    principal = issue_owner_principal(auth_service=auth_service, session_token=session_token)
    authority_registry = AuthorityRegistry()
    root_grant = authority_registry.bootstrap_root_authority(
        owner_principal=principal, auth_service=auth_service, bootstrap_reason="fixture de teste automatizado",
    )
    scopes = [scope] if isinstance(scope, str) else list(scope)
    grant = authority_registry.issue_grant(
        issuer_grant_id=root_grant.grant_id, subject_actor_id=actor_id, authority_level=authority_level,
        scopes=scopes, issuance_reason="fixture de teste automatizado",
    )
    return authority_registry, grant.grant_id


def register_test_claim(evidence_registry, *, claim_id, strategy_id, metric_names=None):
    """`claim_type=FACT` exige `primary_claim_id` resolvendo no
    `ClaimRegistry` e pertencendo à MESMA estratégia — este helper
    registra uma claim de teste válida para isso."""
    from core.evidence.claim_registry import ClaimRecord

    evidence_registry.claim_registry.register(ClaimRecord(
        claim_id=claim_id, strategy_id=strategy_id, claim_type="FACT", statement="claim de teste",
        related_metric_names=list(metric_names or []),
    ))
