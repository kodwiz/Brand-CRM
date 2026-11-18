from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db import Base

PRODUCT_STATUSES = ("active", "inactive", "discontinued")
PRICING_STATUSES = ("unpriced", "active", "suspended")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("status in ('active','inactive','discontinued')", name="status_valid"),
        CheckConstraint("code = upper(code)", name="code_upper"),
        Index("ix_products_name", "name"),
        Index("ix_products_brand", "brand"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(80))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(14), default="active", nullable=False)
    tracks_batches: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tracks_expiry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discount_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    category: Mapped[Category] = relationship(lazy="joined")
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.id", lazy="selectin")


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint("status in ('active','inactive','discontinued')", name="status_valid"),
        CheckConstraint("pricing_status in ('unpriced','active','suspended')", name="pricing_status_valid"),
        CheckConstraint("sku = upper(sku)", name="sku_upper"),
        CheckConstraint("retail_price >= 0 and employee_price >= 0 and distributor_price >= 0 and purchase_cost >= 0",
                        name="prices_non_negative"),
        CheckConstraint("reorder_threshold >= 0", name="reorder_non_negative"),
        CheckConstraint("pricing_status <> 'active' or retail_price is not null", name="active_needs_retail"),
        Index("ix_product_variants_product_id", "product_id"),
        Index("ix_product_variants_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    sku: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)  # label shown in pickers, e.g. "Rosewood 3.5g"
    shade_name: Mapped[str | None] = mapped_column(String(80))
    shade_hex: Mapped[str | None] = mapped_column(String(7))
    size: Mapped[str | None] = mapped_column(String(40))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Reference purchase cost only. Stock value comes from posted transactions, never from this field.
    purchase_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    retail_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    employee_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    distributor_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    pricing_status: Mapped[str] = mapped_column(String(12), default="unpriced", nullable=False)
    reorder_threshold: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(14), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    product: Mapped[Product] = relationship(back_populates="variants")


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("variant_id", "batch_no", name="uq_batches_variant_batch"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("product_variants.id"), nullable=False, index=True)
    batch_no: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ImportJob(Base):
    """A validated (not yet committed) bulk import. Nothing here touches the catalogue until commit."""

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="products", nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(12), default="validated", nullable=False)  # validated|committed|failed
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    records: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    errors: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
