import os


def job_upload_dir(config, job_id):
    return os.path.join(config.upload_dir, job_id)


def job_output_dir(config, job_id):
    return os.path.join(config.output_dir, job_id)


def chunks_dir(config, job_id):
    return os.path.join(job_output_dir(config, job_id), "chunks")


def segment_dir(config, job_id, index):
    return os.path.join(job_output_dir(config, job_id), "segments", str(index))


def music_dir(config, job_id):
    return os.path.join(job_output_dir(config, job_id), "music")
