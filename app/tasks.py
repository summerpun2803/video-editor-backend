# app/tasks.py
import subprocess
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from . import models
from .database import DATABASE_URL
import logging

# Create DB session
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Import celery_app AFTER db setup
from .celery_app import celery_app

@celery_app.task(bind=True, name="app.tasks.trim_video_task")
def trim_video_task(self, job_id: str, input_path: str, output_path: str, start_time: float, duration: float):
    logger.info("in the funciton")
    db = next(get_db())
    
    try:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise Exception(f"Job {job_id} not found")

        job.status = "processing"
        db.commit()

        # FFmpeg trim
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-ss', str(start_time),
            '-t', str(duration),
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-strict', 'experimental',
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        if not os.path.exists(output_path):
            raise Exception("Output file not created")

        # Get metadata
        import ffmpeg
        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        # Save new video
        new_video = models.Video(
            filename=os.path.basename(output_path),
            original_filename=f"Trimmed video (job {job_id})",
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        # Update job
        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        db.commit()

        return {
            "status": "completed",
            "job_id": job_id,
            "video_id": new_video.id,
            "filename": job.result_filename
        }

    except Exception as e:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if job:
            job.status = "failed"
            job.result_filename = None
            db.commit()
        raise Exception(f"Trim failed: {str(e)}")
    finally:
        db.close()