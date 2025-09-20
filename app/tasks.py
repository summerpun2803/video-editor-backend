import subprocess
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from . import models
from .database import DATABASE_URL
import logging
import ffmpeg

# Create DB session
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger().handlers[0].flush = lambda: None 

QUALITY_PRESETS = {
    "1080p": {"width": 1920, "height": 1080, "bitrate": "8000k"},
    "720p": {"width": 1280, "height": 720, "bitrate": "4000k"},
    "480p": {"width": 854, "height": 480, "bitrate": "2000k"},
    "360p": {"width": 640, "height": 360, "bitrate": "1000k"},
}   

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
        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        # Save new video
        new_video = models.Video(
            filename=os.path.basename(output_path),
            original_video_id=f"{job.original_video_id}",
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        # Update job
        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        job.updated_video_id = new_video.id
        db.commit()

        return {
            "status": "completed",
            "job_id": job_id,
            "video_id": new_video.id,
            "filename": job.result_filename
        }

    except Exception as e:
        db.rollback()
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if job:
            job.status = "failed"
            job.result_filename = None
            job.updated_video_id = None
            db.commit()
        raise Exception(f"Trim failed: {str(e)}")
    finally:
        db.close()

@celery_app.task(bind=True, name="app.tasks.add_text_overlay_task")
def add_text_overlay_task(self, job_id: str, input_path: str, output_path: str, overlay_id: int):
    db = next(get_db())

    try:
        # Get job and overlay
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise Exception(f"Job {job_id} not found")
        
        overlay = db.query(models.Overlay).filter(models.Overlay.id == overlay_id).first()
        if not overlay:
            raise Exception(f"Overlay {overlay_id} not found")

        job.status = "processing"
        db.commit()

        text = overlay.content.replace("'", r"'\''")  # Escape single quotes
        fontsize = overlay.font_size or 24
        fontcolor = overlay.font_color or "white"
        
        # Position
        x = overlay.position_x
        y = overlay.position_y

        enable_param = ""
        if overlay.end_time > 0:
            enable_param = f":enable='between(t,{overlay.start_time},{overlay.end_time})'"

        filter_string = f"drawtext=text='{text}':x={x}:y={y}:fontsize={fontsize}:fontcolor={fontcolor}{enable_param}"

        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-vf', filter_string,
            '-c:a', 'copy',
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        if not os.path.exists(output_path):
            raise Exception("Output file not created")

        # Get metadata
        
        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        new_video = models.Video(
            filename=os.path.basename(output_path),
            original_video_id=job.original_video_id,
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        job.updated_video_id = new_video.id
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
        raise Exception(f"Text overlay failed: {str(e)}")
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.add_image_overlay_task")
def add_image_overlay_task(self, job_id: str, input_path: str, output_path: str, img_path: str,
                            x: int, y: int, width: int, height: int,
                            start_time: float, end_time: float, opacity: float):
    """Add image overlay to video"""
    db = next(get_db())
    
    try:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise Exception(f"Job {job_id} not found")

        job.status = "processing"
        db.commit()

        # Build FFmpeg filter
        # Scale image
        scale_filter = f"[1:v]scale={width}:{height}[overlay_scaled]"
        
        # Position and timing
        overlay_filter = f"[0:v][overlay_scaled]overlay=x={x}:y={y}"
        
        # Add opacity
        # if opacity < 1.0:
        #     overlay_filter += f",format=rgba,colorchannelmixer=aa={opacity}"
        
        # Add timing
        if end_time > 0:
            overlay_filter += f":enable='between(t,{start_time},{end_time})'"
        
        filter_complex = f"{scale_filter};{overlay_filter}"

        # Run FFmpeg
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-i', img_path,
            '-filter_complex', filter_complex,
            '-c:a', 'copy',
            output_path
        ]
        logger.info(f"filter command {filter_complex}")
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        if not os.path.exists(output_path):
            raise Exception("Output file not created")


        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        # Save new video
        new_video = models.Video(
            filename=os.path.basename(output_path),
            original_video_id=job.original_video_id,
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        # Update job
        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        job.updated_video_id = new_video.id
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
        raise Exception(f"Image overlay failed: {str(e)}")
    finally:
        db.close()

@celery_app.task(bind=True, name="app.tasks.add_video_overlay_task")
def add_video_overlay_task(self, job_id: str, input_path: str, output_path: str, overlay_path: str,
                          x: int, y: int, width: int, height: int,
                          start_time: float, end_time: float, opacity: float):
    """Add video overlay to video"""
    db = next(get_db())
    
    try:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise Exception(f"Job {job_id} not found")

        job.status = "processing"
        db.commit()

        # Build FFmpeg filter
        # Scale overlay video
        scale_filter = f"[1:v]scale={width}:{height}[overlay_scaled]"
        
        # Position and timing
        overlay_filter = f"[0:v][overlay_scaled]overlay=x={x}:y={y}"
        
        # Add opacity
        # if opacity < 1.0:
        #     overlay_filter += f",format=rgba,colorchannelmixer=aa={opacity}"
        
        # Add timing
        if end_time > 0:
            overlay_filter += f":enable='between(t,{start_time},{end_time})'"
        
        filter_complex = f"{scale_filter};{overlay_filter}"
        logger.info(f"filter command {filter_complex}")
        # Run FFmpeg
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-i', overlay_path,
            '-filter_complex', filter_complex,
            '-c:a', 'copy',
            output_path
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info(f"FFmpeg filter_complex: {filter_complex}")

        if not os.path.exists(output_path):
            raise Exception("Output file not created")

        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_duration = float(format_info.get('duration', 0.0))
        new_size = int(format_info.get('size', 0))

        # Save new video
        new_video = models.Video(
            filename=os.path.basename(output_path),
            original_video_id=job.original_video_id,
            duration=new_duration,
            size=new_size
        )
        db.add(new_video)
        db.commit()

        # Update job
        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        job.updated_video_id = new_video.id
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
        raise Exception(f"Video overlay failed: {str(e)}")
    finally:
        db.close()


@celery_app.task(bind=True, name="app.tasks.convert_quality_task")
def convert_quality_task(self, job_id: str, input_path: str, output_path: str, quality: str):
    db = next(get_db())
    
    try:
        # Get job
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if not job:
            raise Exception(f"Job {job_id} not found")

        job.status = "processing"
        db.commit()

        # Get quality preset
        if quality not in QUALITY_PRESETS:
            raise Exception(f"Unknown quality: {quality}")
        
        preset = QUALITY_PRESETS[quality]

        # FFmpeg command for quality conversion
        cmd = [
            'ffmpeg',
            '-i', input_path,
            '-vf', f'scale={preset["width"]}:{preset["height"]}',
            '-c:v', 'libx264',
            '-b:v', preset["bitrate"],
            '-c:a', 'aac',
            '-b:a', '192k',
            '-strict', 'experimental',
            output_path
        ]
        
        # Execute
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        # Verify output
        if not os.path.exists(output_path):
            raise Exception("Output file not created")

        probe = ffmpeg.probe(output_path)
        format_info = probe.get('format', {})
        new_size = int(format_info.get('size', 0))

        # Save quality record
        quality_record = models.VideoQuality(
            video_id=job.original_video_id,
            quality=quality,
            file_path=os.path.basename(output_path),
            file_size=new_size,
            width=preset['width'],
            height=preset['height'],
            bitrate=preset['bitrate']
        )
        db.add(quality_record)
        db.commit()

        # Update job
        job.status = "completed"
        job.result_filename = os.path.basename(output_path)
        job.updated_video_id = job.original_video_id
        db.commit()

        return {
            "status": "completed",
            "job_id": job_id,
            "quality": quality,
            "file_size": new_size
        }

    except Exception as e:
        job = db.query(models.Job).filter(models.Job.id == job_id).first()
        if job:
            job.status = "failed"
            job.result_filename = None
            db.commit()
        raise Exception(f"Quality conversion failed: {str(e)}")
    finally:
        db.close()