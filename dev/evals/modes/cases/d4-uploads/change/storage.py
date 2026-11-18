import os

UPLOAD_DIR = "/srv/uploads"


def save_upload(name, data):
    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path
