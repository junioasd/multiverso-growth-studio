"""
core.production.copyright_gate
================================
Copyright Gate (Seção 20). Direito/licença UNKNOWN, AMBIGUOUS ou
INCOMPATIBLE => BLOCK. Prioriza ativos próprios, música própria,
material com direitos comerciais claros, bibliotecas licenciadas,
conteúdo autorizado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LicenseStatus(str, Enum):
    OWN_ASSET = "OWN_ASSET"
    LICENSED_LIBRARY = "LICENSED_LIBRARY"
    CLEARED_COMMERCIAL = "CLEARED_COMMERCIAL"
    UNKNOWN = "UNKNOWN"
    AMBIGUOUS = "AMBIGUOUS"
    INCOMPATIBLE = "INCOMPATIBLE"


_BLOCKING_STATUSES = {LicenseStatus.UNKNOWN, LicenseStatus.AMBIGUOUS, LicenseStatus.INCOMPATIBLE}
_CLEAR_STATUSES = {LicenseStatus.OWN_ASSET, LicenseStatus.LICENSED_LIBRARY, LicenseStatus.CLEARED_COMMERCIAL}


@dataclass
class AssetRights:
    asset_ref: str
    source: str
    license_status: LicenseStatus
    commercial_use: bool | None = None
    monetization_compatible: bool | None = None
    evidence: str | None = None
    restrictions: list[str] = field(default_factory=list)


@dataclass
class CopyrightGateResult:
    approved: bool
    blocked_assets: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class CopyrightGate:
    def evaluate(self, assets: list[AssetRights]) -> CopyrightGateResult:
        blocked: list[str] = []
        reasons: list[str] = []

        for asset in assets:
            if asset.license_status in _BLOCKING_STATUSES:
                blocked.append(asset.asset_ref)
                reasons.append(
                    f"{asset.asset_ref}: status de licença '{asset.license_status.value}' => BLOCK"
                )
                continue

            if asset.commercial_use is False:
                blocked.append(asset.asset_ref)
                reasons.append(f"{asset.asset_ref}: uso comercial não autorizado")
                continue

            if asset.monetization_compatible is False:
                blocked.append(asset.asset_ref)
                reasons.append(f"{asset.asset_ref}: incompatível com monetização")
                continue

        return CopyrightGateResult(approved=len(blocked) == 0, blocked_assets=blocked, reasons=reasons)
