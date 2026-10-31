"""
core.models.entities
=====================
Entidades de domínio (Seções 6, 47, 48, 55, 56 do Comando Mestre).
Dataclasses puras — persistência fica em core.models.repositories.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Channel (Seção 6/7)
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    channel_id: str
    platform: str
    niche: str = "UNKNOWN"
    subniche: str = "UNKNOWN"
    audience: str = "UNKNOWN"
    language: str = "pt-BR"
    region: str = "UNKNOWN"
    content_style: str = "UNKNOWN"
    voice_profile: str = "DOCUMENTAL"
    visual_identity: str = "UNKNOWN"
    strategy: str = "UNKNOWN"
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(platform: str, **kwargs) -> "Channel":
        return Channel(channel_id=new_id("chan"), platform=platform, **kwargs)


# ---------------------------------------------------------------------------
# Project (Seção 55) - Project Manager
# ---------------------------------------------------------------------------

class ProjectStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


@dataclass
class Project:
    project_id: str
    channel_id: str
    goal: str
    status: ProjectStatus = ProjectStatus.PENDING
    budget: float | None = None
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(channel_id: str, goal: str, budget: float | None = None) -> "Project":
        return Project(project_id=new_id("proj"), channel_id=channel_id, goal=goal, budget=budget)


# ---------------------------------------------------------------------------
# Job (Seção 56) - Job Queue
# ---------------------------------------------------------------------------

class JobState(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Job:
    job_id: str
    job_type: str
    idempotency_key: str
    project_id: str | None = None
    channel_id: str | None = None
    state: JobState = JobState.PENDING
    attempts: int = 0
    max_attempts: int = 3
    payload: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    # MICROCORREÇÃO 5 (item 3): ver nota equivalente em `Lead.version`.
    version: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def create(job_type: str, idempotency_key: str, **kwargs) -> "Job":
        return Job(job_id=new_id("job"), job_type=job_type, idempotency_key=idempotency_key, **kwargs)


# ---------------------------------------------------------------------------
# Product / Offer / Funnel (Seções 34-43)
# ---------------------------------------------------------------------------

@dataclass
class Product:
    product_id: str
    platform: str
    name: str
    niche: str = "UNKNOWN"
    price: float | None = None
    commission_pct: float | None = None
    reputation_score: float | None = None  # 0-1, None = UNKNOWN
    refund_rate: float | None = None       # 0-1, None = UNKNOWN
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(platform: str, name: str, **kwargs) -> "Product":
        return Product(product_id=new_id("prod"), platform=platform, name=name, **kwargs)


class OfferStatus(str, Enum):
    DRAFT = "DRAFT"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"


@dataclass
class Offer:
    offer_id: str
    product_id: str
    channel_id: str
    funnel_id: str | None = None
    status: OfferStatus = OfferStatus.DRAFT
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(product_id: str, channel_id: str, **kwargs) -> "Offer":
        return Offer(offer_id=new_id("offer"), product_id=product_id, channel_id=channel_id, **kwargs)


class FunnelType(str, Enum):
    DIRECT_TO_OFFER = "direct-to-offer"
    LEAD_MAGNET = "lead-magnet"
    EMAIL_FUNNEL = "email-funnel"
    CONTENT_FUNNEL = "content-funnel"
    VSL = "vsl"
    WEBINAR = "webinar"
    QUIZ = "quiz"
    TRIPWIRE = "tripwire"
    UPSELL = "upsell"
    CROSS_SELL = "cross-sell"
    COMMUNITY = "community-funnel"
    DM_ASSISTED = "dm-assisted"
    ORGANIC_REMARKETING = "organic-remarketing"


@dataclass
class Funnel:
    funnel_id: str
    channel_id: str
    funnel_type: FunnelType
    config: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(channel_id: str, funnel_type: FunnelType, **kwargs) -> "Funnel":
        return Funnel(funnel_id=new_id("funnel"), channel_id=channel_id, funnel_type=funnel_type, **kwargs)


# ---------------------------------------------------------------------------
# Lead / CRM (Seção 47)
# ---------------------------------------------------------------------------

class LeadStatus(str, Enum):
    NEW = "NEW"
    ENGAGED = "ENGAGED"
    QUALIFIED = "QUALIFIED"
    OFFERED = "OFFERED"
    CUSTOMER = "CUSTOMER"
    LOST = "LOST"
    UNSUBSCRIBED = "UNSUBSCRIBED"


@dataclass
class Lead:
    lead_id: str
    channel_id: str
    source: str = "UNKNOWN"
    campaign: str | None = None
    content_id: str | None = None
    offer_id: str | None = None
    product_id: str | None = None
    status: LeadStatus = LeadStatus.NEW
    consent_state: str = "UNKNOWN"
    conversion_state: str = "UNKNOWN"
    # MICROCORREÇÃO 5 (item 3): versão para controle de concorrência
    # otimista — cada UPDATE condiciona ao valor lido, detectando
    # atualizações baseadas em estado obsoleto entre instâncias.
    version: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def create(channel_id: str, **kwargs) -> "Lead":
        return Lead(lead_id=new_id("lead"), channel_id=channel_id, **kwargs)


# ---------------------------------------------------------------------------
# Sales tracking (Seção 48)
# ---------------------------------------------------------------------------

class ValueType(str, Enum):
    REAL = "REAL"
    ESTIMATED = "ESTIMATED"
    FORECAST = "FORECAST"
    UNKNOWN = "UNKNOWN"


@dataclass
class SalesEvent:
    event_id: str
    channel_id: str
    event_type: str  # impression|view|click|lead|checkout|sale|refund
    value_type: ValueType = ValueType.UNKNOWN
    amount: float | None = None
    currency: str = "BRL"
    lead_id: str | None = None
    offer_id: str | None = None
    # ATLAS+RADAR (item 2 — desempenho multidimensional): dimensões
    # opcionais além de canal. Nunca preenchidas automaticamente com
    # valor inventado — "" (equivalente a None/ausente) até que o
    # chamador informe um dado real.
    product_id: str | None = None
    campaign_id: str | None = None
    funnel_id: str | None = None
    platform: str | None = None
    traffic_source: str | None = None
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def create(channel_id: str, event_type: str, **kwargs) -> "SalesEvent":
        return SalesEvent(event_id=new_id("sevt"), channel_id=channel_id, event_type=event_type, **kwargs)
