from sqlalchemy import Column, Integer, String
from source.database import Base

class Entry(Base):
    __tablename__ = "entries"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(String, index=True)
    author = Column(String, index=True)
