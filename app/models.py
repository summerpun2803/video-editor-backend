from sqlalchemy import Column, Integer, String, Float, DateTime, func, ForeignKey
from .database import Base 

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    original_filename = Column(String)
    duration = Column(Float)
    size = Column(Integer)
    upload_time = Column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, index=True)  
    video_id = Column(Integer, ForeignKey("videos.id"))
    status = Column(String, default="pending")  # pending, completed, failed
    result_filename = Column(String, nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    