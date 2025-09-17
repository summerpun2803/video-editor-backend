from sqlalchemy import Column, Integer, String, Float, DateTime, func
from .database import Base 

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    original_filename = Column(String)
    duration = Column(Float)
    size = Column(Integer)
    upload_time = Column(DateTime(timezone=True), server_default=func.now())
    