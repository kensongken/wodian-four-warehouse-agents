from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from photo_organizer.models.base import Base, UUIDTimestampMixin


class User(UUIDTimestampMixin, Base):
    __tablename__ = "users"

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
