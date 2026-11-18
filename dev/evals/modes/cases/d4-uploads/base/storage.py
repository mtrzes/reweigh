UPLOAD_DIR = "/srv/uploads"


def save_upload(name, data):
    with open(UPLOAD_DIR + "/" + name, "wb") as f:
        f.write(data)
