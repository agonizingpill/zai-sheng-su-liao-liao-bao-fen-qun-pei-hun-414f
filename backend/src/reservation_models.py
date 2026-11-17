"""秤台整单预留：推荐不占料，确认后按料包锁定余量，退回草稿即释放。"""
from decimal import Decimal
from sqlalchemy import String,Numeric,ForeignKey,UniqueConstraint,CheckConstraint,BigInteger
from sqlalchemy.orm import Mapped,mapped_column
from .database import Base
class BlendReservation(Base):
    __tablename__='blend_reservations'
    # 同配方同料包仅一条预留；整单预留与释放按整组行处理
    __table_args__=(UniqueConstraint('blend_id','bale_id',name='uq_reservation_blend_bale'),CheckConstraint('kg > 0'))
    id:Mapped[int]=mapped_column(primary_key=True)
    blend_id:Mapped[int]=mapped_column(ForeignKey('blends.id'))
    bale_id:Mapped[int]=mapped_column(ForeignKey('bales.id'))
    kg:Mapped[Decimal]=mapped_column(Numeric(12,3))
    cluster_snapshot:Mapped[int]
    version_snapshot:Mapped[int]=mapped_column(BigInteger)
    pvc_snapshot:Mapped[Decimal]=mapped_column(Numeric(9,3))
    reserved_by:Mapped[str]=mapped_column(String(120))
    reserved_at:Mapped[str]=mapped_column(String(40))
