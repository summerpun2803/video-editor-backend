from sqlalchemy import Column, Integer, String, Float, DateTime, func, ForeignKey
from .database import Base 

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, index=True)
    original_video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    duration = Column(Float)
    size = Column(Integer)
    upload_time = Column(DateTime(timezone=True), server_default=func.now())


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, index=True)  
    original_video_id = Column(Integer, ForeignKey("videos.id"))
    updated_video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    status = Column(String, default="pending")  # pending, completed, failed
    result_filename = Column(String, nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    type = Column(String)

class Overlay(Base):
    __tablename__ = "overlays"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    overlay_type = Column(String)  # "text", "image", "video"
    content = Column(String)       # text content or image/video filename
    position_x = Column(Integer, default=10)
    position_y = Column(Integer, default=10)
    start_time = Column(Float, default=0.0)
    end_time = Column(Float, default=0.0)
    font_size = Column(Integer, nullable=True)
    font_color = Column(String, nullable=True)
    opacity = Column(Float, default=1.0)

    scale_width = Column(Integer, nullable=True) 
    scale_height = Column(Integer, nullable=True)  

# Add this class before Video
class VideoQuality(Base):
    __tablename__ = "video_qualities"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    quality = Column(String)  # "1080p", "720p", "480p"
    file_path = Column(String)
    file_size = Column(Integer)
    width = Column(Integer)
    height = Column(Integer)
    bitrate = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
